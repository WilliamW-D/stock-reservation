# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

COPY pyproject.toml uv.lock .python-version ./

ENV UV_COMPILE_BYTECODE=1
RUN uv sync --frozen --no-dev --no-install-project

# ------------------------------------------------------------------ runner
FROM python:3.12-slim AS runner

WORKDIR /app

RUN groupadd -r appgroup && useradd -r -g appgroup -u 1000 appuser

COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

COPY app/ ./app/
COPY migrations/ ./migrations/
COPY alembic.ini ./

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
