"""High-concurrency async load testing script for Cellar stock reservation service.

Simulates hundreds of concurrent operations across multiple workers:
- Concurrent single and multi-product reservations
- Idempotent request replays
- Order fulfillments and partial line fulfillments
- Order cancellations
- Manager stock deliveries / receipts
- Verification of 100% database invariants at the end.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import connect
from tests.conftest import assert_invariants


async def run_load_test(
    base_url: str,
    database_url: str,
    *,
    total_operations: int = 250,
    concurrency: int = 15,
) -> None:
    print(f"=== Starting Load Test against {base_url} ===")
    print(f"Concurrency: {concurrency} workers | Total operations: {total_operations}\n")

    latencies: list[float] = []
    status_counts: dict[int, int] = {}
    replayed_count = 0
    errors: list[str] = []

    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        # 1. Login actors
        manager_token_resp = await client.post("/auth/token", data={"username": "maria", "password": "manager-pass"})
        if manager_token_resp.status_code != 200:
            print(f"Failed to authenticate manager: {manager_token_resp.text}")
            sys.exit(1)
        manager_token = manager_token_resp.json()["access_token"]

        eli_token_resp = await client.post("/auth/token", data={"username": "eli", "password": "employee-pass"})
        if eli_token_resp.status_code != 200:
            print(f"Failed to authenticate employee: {eli_token_resp.text}")
            sys.exit(1)
        eli_token = eli_token_resp.json()["access_token"]

        # 2. Seed stock
        products_resp = await client.get("/products", headers={"Authorization": f"Bearer {manager_token}"})
        products = products_resp.json()
        if not products:
            p_cheese = await client.post(
                "/products",
                json={"sku": "CHEDDAR-LOAD", "name": "Cheddar Cheese", "unit": "case"},
                headers={"Authorization": f"Bearer {manager_token}"},
            )
            p_butter = await client.post(
                "/products",
                json={"sku": "BUTTER-LOAD", "name": "Butter", "unit": "case"},
                headers={"Authorization": f"Bearer {manager_token}"},
            )
            cheese_id = p_cheese.json()["product_id"]
            butter_id = p_butter.json()["product_id"]
        else:
            cheese_id = products[0]["product_id"]
            butter_id = products[1]["product_id"] if len(products) > 1 else cheese_id

        # Receive opening stock
        await client.post(
            f"/inventory/{cheese_id}/receive",
            json={"quantity": 100, "reason": "Load test opening stock"},
            headers={"Authorization": f"Bearer {manager_token}", "Idempotency-Key": f"seed-cheese-{uuid.uuid4()}"},
        )
        await client.post(
            f"/inventory/{butter_id}/receive",
            json={"quantity": 100, "reason": "Load test opening stock"},
            headers={"Authorization": f"Bearer {manager_token}", "Idempotency-Key": f"seed-butter-{uuid.uuid4()}"},
        )

        active_reservations: list[dict[str, Any]] = []
        lock = asyncio.Lock()
        sem = asyncio.Semaphore(concurrency)

        start_time = time.perf_counter()

        async def worker_task(op_id: int):
            async with sem:
                op_type = random.choices(
                    ["reserve_single", "reserve_multi", "replay", "fulfill", "cancel", "receive"],
                    weights=[35, 25, 15, 12, 8, 5],
                    k=1,
                )[0]

                t0 = time.perf_counter()
                status = 0
                is_replay = False

                try:
                    if op_type == "reserve_single":
                        key = str(uuid.uuid4())
                        resp = await client.post(
                            "/reservations",
                            json={
                                "product_id": cheese_id,
                                "quantity": random.randint(1, 3),
                                "order_reference": f"load-single-{op_id}",
                            },
                            headers={"Authorization": f"Bearer {eli_token}", "Idempotency-Key": key},
                        )
                        status = resp.status_code
                        if status == 201:
                            res_data = resp.json()["reservation"]
                            res_data["_key"] = key
                            async with lock:
                                active_reservations.append(res_data)

                    elif op_type == "reserve_multi":
                        key = str(uuid.uuid4())
                        resp = await client.post(
                            "/reservations",
                            json={
                                "order_reference": f"load-multi-{op_id}",
                                "items": [
                                    {"product_id": cheese_id, "quantity": random.randint(1, 2)},
                                    {"product_id": butter_id, "quantity": random.randint(1, 2)},
                                ],
                            },
                            headers={"Authorization": f"Bearer {eli_token}", "Idempotency-Key": key},
                        )
                        status = resp.status_code
                        if status == 201:
                            res_data = resp.json()["reservation"]
                            res_data["_key"] = key
                            async with lock:
                                active_reservations.append(res_data)

                    elif op_type == "replay":
                        target = None
                        async with lock:
                            if active_reservations:
                                target = random.choice(active_reservations)
                        if target and "_key" in target:
                            resp = await client.post(
                                "/reservations",
                                json={
                                    "product_id": cheese_id,
                                    "quantity": 1,
                                    "order_reference": target["order_reference"],
                                },
                                headers={"Authorization": f"Bearer {eli_token}", "Idempotency-Key": target["_key"]},
                            )
                            status = resp.status_code
                            if resp.headers.get("Idempotent-Replayed") == "true":
                                is_replay = True
                        else:
                            status = 200

                    elif op_type == "fulfill":
                        target = None
                        async with lock:
                            if active_reservations:
                                target = active_reservations.pop()
                        if target:
                            resp = await client.post(
                                f"/reservations/{target['id']}/fulfill",
                                json={"reason": "Load test customer picked up"},
                                headers={"Authorization": f"Bearer {eli_token}"},
                            )
                            status = resp.status_code
                        else:
                            status = 200

                    elif op_type == "cancel":
                        target = None
                        async with lock:
                            if active_reservations:
                                target = active_reservations.pop()
                        if target:
                            resp = await client.post(
                                f"/reservations/{target['id']}/cancel",
                                json={"reason": "Load test cancellation"},
                                headers={"Authorization": f"Bearer {eli_token}"},
                            )
                            status = resp.status_code
                        else:
                            status = 200

                    elif op_type == "receive":
                        resp = await client.post(
                            f"/inventory/{cheese_id}/receive",
                            json={"quantity": 5, "reason": "Mid-test replenishment"},
                            headers={"Authorization": f"Bearer {manager_token}", "Idempotency-Key": str(uuid.uuid4())},
                        )
                        status = resp.status_code

                except Exception as ex:
                    errors.append(str(ex))
                finally:
                    duration = time.perf_counter() - t0
                    latencies.append(duration)
                    if status:
                        status_counts[status] = status_counts.get(status, 0) + 1
                    if is_replay:
                        nonlocal replayed_count
                        replayed_count += 1

        tasks = [worker_task(i) for i in range(total_operations)]
        await asyncio.gather(*tasks)

        total_time = time.perf_counter() - start_time

    # Calculate percentiles
    latencies.sort()
    n = len(latencies)
    p50 = latencies[int(n * 0.50)] * 1000 if n else 0
    p95 = latencies[int(n * 0.95)] * 1000 if n else 0
    p99 = latencies[int(n * 0.99)] * 1000 if n else 0
    rps = n / total_time if total_time > 0 else 0

    print("=================== LOAD TEST RESULTS ===================")
    print(f"Total Completed Operations: {n}")
    print(f"Total Duration:             {total_time:.2f} seconds")
    print(f"Throughput:                 {rps:.1f} req/s")
    print(f"Latency p50:                {p50:.1f} ms")
    print(f"Latency p95:                {p95:.1f} ms")
    print(f"Latency p99:                {p99:.1f} ms")
    print(f"Idempotent Replays:         {replayed_count}")
    print("\nStatus Code Distribution:")
    for code, cnt in sorted(status_counts.items()):
        print(f"  HTTP {code}: {cnt}")
    if errors:
        print(f"\nErrors encountered: {len(errors)}")

    print("\n=== Running Database Invariant Check ===")
    with connect(database_url) as conn:
        assert_invariants(conn)
    print(">>> 100% DATABASE INVARIANTS SATISFIED! Zero overselling, zero audit discrepancies.\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Cellar Load Tester")
    parser.add_argument("--url", default="http://localhost:8000", help="Base URL of server")
    parser.add_argument(
        "--db",
        default=os.getenv("DATABASE_URL", "postgresql://stock:stock@localhost:5433/stock"),
        help="DB connection URL",
    )
    parser.add_argument("-n", "--total", type=int, default=200, help="Total operations to run")
    parser.add_argument("-c", "--concurrency", type=int, default=10, help="Concurrent workers")
    args = parser.parse_args()

    asyncio.run(
        run_load_test(
            base_url=args.url,
            database_url=args.db,
            total_operations=args.total,
            concurrency=args.concurrency,
        )
    )


if __name__ == "__main__":
    main()
