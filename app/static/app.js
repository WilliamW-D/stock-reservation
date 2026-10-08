// Cellar dashboard — a thin client. Every rule is enforced by the backend;
// this page only makes the guarantees visible.

const $ = (id) => document.getElementById(id);
const state = { token: null, user: null, products: [], productId: null, key: null, lastBody: null };

// ------------------------------------------------------------------ http
async function api(method, path, { body, headers = {}, quiet = false } = {}) {
  const h = { ...headers };
  if (state.token) h.Authorization = `Bearer ${state.token}`;
  if (body !== undefined && !(body instanceof URLSearchParams)) h["Content-Type"] = "application/json";
  const started = performance.now();
  const res = await fetch(path, {
    method,
    headers: h,
    body: body === undefined ? undefined : body instanceof URLSearchParams ? body : JSON.stringify(body),
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  const replayed = res.headers.get("Idempotent-Replayed") === "true";
  if (!quiet) log(method, path, res.status, data, replayed, performance.now() - started);
  return { ok: res.ok, status: res.status, data, replayed };
}

const errMsg = (r) => r.data?.error?.message || r.data?.detail?.[0]?.msg || `HTTP ${r.status}`;
const errCode = (r) => r.data?.error?.code || (r.status === 422 ? "validation_error" : `http_${r.status}`);

// ------------------------------------------------------------------ ui helpers
function toast(title, text = "", kind = "info") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `<b></b><span></span>`;
  el.querySelector("b").textContent = title;
  el.querySelector("span").textContent = text;
  $("toasts").appendChild(el);
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 300); }, 4200);
}

function log(method, path, status, data, replayed, ms) {
  const li = document.createElement("li");
  const cls = status >= 400 ? "c4" : replayed ? "c3" : "c2";
  li.innerHTML = `<span class="code ${cls}"></span><span></span><span class="detail"></span>`;
  li.children[0].textContent = status;
  li.children[1].textContent = `${method} ${path}${replayed ? "  ↺ replayed" : ""}  · ${ms.toFixed(0)}ms`;
  li.children[2].textContent = data?.error ? `${data.error.code}: ${data.error.message}` : "";
  const list = $("log");
  list.prepend(li);
  while (list.children.length > 60) list.lastChild.remove();
}

const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`);
function newKey() { state.key = uuid(); state.lastBody = null; $("idem-key").textContent = state.key; }

function setMetric(id, value) {
  const el = $(id);
  if (el.textContent !== String(value)) {
    el.textContent = value;
    el.classList.remove("bump"); void el.offsetWidth; el.classList.add("bump");
  }
}

function timeLeft(iso) {
  const s = Math.round((new Date(iso) - Date.now()) / 1000);
  if (s <= 0) return "due";
  return s >= 3600 ? `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

// ------------------------------------------------------------------ rendering
function renderInventory(inv) {
  $("product-name").textContent = inv.name;
  $("product-sku").textContent = inv.sku;
  $("m-unit-1").textContent = `${inv.unit}s on hand`;
  setMetric("m-onhand", inv.on_hand_quantity);
  setMetric("m-reserved", inv.reserved_quantity);
  setMetric("m-available", inv.available_quantity);

  const bar = $("stockbar");
  const total = inv.on_hand_quantity;
  const shown = Math.min(total, 60);
  while (bar.children.length < shown) { const u = document.createElement("div"); u.className = "unit pop"; bar.appendChild(u); }
  while (bar.children.length > shown) bar.lastChild.remove();
  [...bar.children].forEach((u, i) => {
    const reserved = i < Math.min(inv.reserved_quantity, shown);
    if (u.classList.contains("reserved") !== reserved) { u.classList.toggle("reserved", reserved); u.classList.remove("pop"); void u.offsetWidth; u.classList.add("pop"); }
  });

  const ok = inv.reserved_quantity >= 0 && inv.reserved_quantity <= inv.on_hand_quantity
    && inv.available_quantity === inv.on_hand_quantity - inv.reserved_quantity;
  const line = $("invariant-line");
  line.className = `invariant ${ok ? "ok" : ""}`;
  line.textContent = `0 ≤ reserved (${inv.reserved_quantity}) ≤ physical (${inv.on_hand_quantity}) · available = ${inv.on_hand_quantity} − ${inv.reserved_quantity} = ${inv.available_quantity}`;
}

let seenReservations = new Set();
function renderReservations(rows) {
  const body = $("res-body");
  $("res-scope").textContent = state.user?.role === "manager" ? "All employees" : "Only yours — enforced server-side";
  if (!rows.length) { body.innerHTML = `<tr><td colspan="7" class="empty">No reservations yet</td></tr>`; return; }
  body.innerHTML = "";
  for (const r of rows) {
    const tr = document.createElement("tr");
    if (!seenReservations.has(`${r.id}:${r.status}`) && seenReservations.size) tr.classList.add("fresh");
    tr.innerHTML = `<td class="num"></td><td></td><td></td><td class="num"></td>
      <td><span class="pill ${r.status}"></span></td><td class="num"></td><td><div class="actions"></div></td>`;
    const c = tr.children;
    c[0].textContent = r.id; c[1].textContent = r.order_reference; c[2].textContent = r.username;
    c[3].textContent = r.quantity; c[4].firstChild.textContent = r.status;
    c[5].textContent = r.status === "active" ? timeLeft(r.expires_at) : "—";
    if (r.status === "active") {
      const actions = c[6].firstChild;
      for (const [label, verb, cls] of [["Fulfill", "fulfill", "primary"], ["Cancel", "cancel", "ghost"]]) {
        const b = document.createElement("button");
        b.className = `${cls} small`; b.id = `res-${r.id}-${verb}`; b.textContent = label;
        b.onclick = () => transition(r.id, verb);
        actions.appendChild(b);
      }
    }
    body.appendChild(tr);
  }
  seenReservations = new Set(rows.map((r) => `${r.id}:${r.status}`));
}

let seenAudit = 0;
function renderAudit(rows) {
  const body = $("audit-body");
  if (!rows) { body.innerHTML = `<tr><td colspan="9" class="empty">Sign in as Maria (manager) to view the audit trail</td></tr>`; return; }
  if (!rows.length) { body.innerHTML = `<tr><td colspan="9" class="empty">No events</td></tr>`; return; }
  const maxId = rows[0].id;
  body.innerHTML = "";
  for (const e of rows) {
    const tr = document.createElement("tr");
    if (seenAudit && e.id > seenAudit) tr.classList.add("fresh");
    const delta = (n) => `<span class="${n > 0 ? "pos" : n < 0 ? "neg" : ""}">${n > 0 ? "+" : ""}${n}</span>`;
    tr.innerHTML = `<td class="num"></td><td class="num"></td><td></td><td><span class="pill action"></span></td>
      <td class="num"></td><td class="num">${delta(e.on_hand_delta)}</td><td class="num">${delta(e.reserved_delta)}</td>
      <td class="num"></td><td></td>`;
    const c = tr.children;
    c[0].textContent = e.id;
    c[1].textContent = new Date(e.created_at).toLocaleTimeString();
    c[2].textContent = e.actor_username || "system";
    c[3].firstChild.textContent = e.action;
    c[4].textContent = e.reservation_id ?? "—";
    c[7].textContent = `${e.on_hand_after} / ${e.reserved_after}`;
    c[8].textContent = e.reason || "";
    body.appendChild(tr);
  }
  seenAudit = maxId;
}

// ------------------------------------------------------------------ data
async function refresh() {
  if (!state.token) return;
  const products = await api("GET", "/products", { quiet: true });
  if (!products.ok) return;
  state.products = products.data;
  const select = $("product-select");
  if (select.options.length !== state.products.length) {
    select.innerHTML = state.products.map((p) => `<option value="${p.product_id}">${p.name} (${p.sku})</option>`).join("");
  }
  if (!state.productId && state.products.length) state.productId = state.products[0].product_id;
  select.value = state.productId;
  const inv = state.products.find((p) => p.product_id === state.productId);
  if (inv) renderInventory(inv);

  const res = await api("GET", "/reservations", { quiet: true });
  if (res.ok) renderReservations(res.data);

  if (state.user.role === "manager") {
    const audit = await api("GET", `/audit-events?limit=100`, { quiet: true });
    if (audit.ok) renderAudit(audit.data);
  } else renderAudit(null);
}

async function login(button) {
  const form = new URLSearchParams({ username: button.dataset.user, password: button.dataset.pass });
  state.token = null;
  const r = await api("POST", "/auth/token", { body: form });
  if (!r.ok) return toast("Sign-in failed", errMsg(r) + " — did you run `python -m app.cli seed`?", "err");
  state.token = r.data.access_token;
  state.user = r.data.user;
  localStorage.setItem("cellar-user", button.dataset.user);
  document.querySelectorAll(".user-chip").forEach((c) => c.classList.toggle("active", c === button));
  seenReservations = new Set(); seenAudit = 0;
  toast(`Signed in as ${state.user.username}`, `Role: ${state.user.role}`, "ok");
  newKey();
  refresh();
}

// ------------------------------------------------------------------ actions
function reservationBody() {
  return {
    product_id: state.productId,
    quantity: Number($("reserve-qty").value),
    order_reference: $("reserve-order").value,
    ttl_minutes: Number($("reserve-ttl").value),
  };
}

async function reserve(retry) {
  if (!requireLogin()) return;
  const body = retry && state.lastBody ? state.lastBody : reservationBody();
  state.lastBody = body;
  const r = await api("POST", "/reservations", { body, headers: { "Idempotency-Key": state.key } });
  if (r.ok && r.replayed) toast("Replayed original result", `Same key → reservation #${r.data.reservation.id}, no new stock change`, "info");
  else if (r.ok) toast(`Reserved ${r.data.reservation.quantity}`, `Reservation #${r.data.reservation.id} · key kept for retries`, "ok");
  else toast(`Rejected: ${errCode(r)}`, errMsg(r), "err");
  refresh();
}

async function transition(id, verb) {
  const r = await api("POST", `/reservations/${id}/${verb}`);
  if (r.ok) toast(`Reservation #${id} ${r.data.reservation.status}`, "", "ok");
  else toast(`Rejected: ${errCode(r)}`, errMsg(r), "err");
  refresh();
}

async function managerAction(method, path, body, success) {
  if (!requireLogin()) return;
  const r = await api(method, path, { body, headers: { "Idempotency-Key": uuid() } });
  if (r.ok) toast(success(r.data), "", "ok");
  else toast(`Rejected: ${errCode(r)}`, errMsg(r), "err");
  refresh();
}

async function race(duplicate) {
  if (!requireLogin()) return;
  const n = Number($("race-n").value);
  const grid = $("race-grid");
  grid.innerHTML = "";
  const dots = Array.from({ length: n }, (_, i) => {
    const d = document.createElement("div"); d.className = "dot pending"; d.textContent = i + 1; grid.appendChild(d); return d;
  });
  const before = state.products.find((p) => p.product_id === state.productId);
  const sharedKey = uuid();
  const body = { product_id: state.productId, quantity: 1, order_reference: duplicate ? "Double-clicked order" : "Race lab", ttl_minutes: 30 };

  // Fire everything at once; the browser sends them concurrently.
  const results = await Promise.all(
    dots.map(() => api("POST", "/reservations", { body, headers: { "Idempotency-Key": duplicate ? sharedKey : uuid() }, quiet: true })),
  );

  let wins = 0, replays = 0, losses = 0;
  results.forEach((r, i) => {
    const d = dots[i];
    d.classList.remove("pending");
    if (r.ok && r.replayed) { d.classList.add("replay"); d.textContent = "↺"; replays++; }
    else if (r.ok) { d.classList.add("win"); d.textContent = "✓"; wins++; }
    else { d.classList.add("lose"); d.textContent = "✕"; losses++; }
    d.title = r.ok ? `reservation #${r.data.reservation.id}` : errCode(r);
  });
  log("POST", `/reservations ×${n}`, wins ? 201 : 409, null, duplicate, 0);

  const ids = new Set(results.filter((r) => r.ok).map((r) => r.data.reservation.id));
  $("race-summary").innerHTML = duplicate
    ? `Same key sent <b>${n}×</b> → <b>${ids.size}</b> reservation created, <b>${replays}</b> replays of the original, <b>${losses}</b> rejected.`
    : `Available before: <b>${before?.available_quantity ?? "?"}</b> · <b>${wins}</b> succeeded, <b>${losses}</b> rejected (insufficient_stock). Never oversold.`;
  refresh();
}

function requireLogin() {
  if (state.token) return true;
  toast("Pick a user first", "Sign in as Maria, Eli or Erin at the top right", "info");
  return false;
}

// ------------------------------------------------------------------ wiring
document.querySelectorAll(".user-chip").forEach((b) => b.addEventListener("click", () => login(b)));
$("product-select").addEventListener("change", (e) => { state.productId = Number(e.target.value); refresh(); });
$("reserve-form").addEventListener("submit", (e) => { e.preventDefault(); reserve(false); });
$("reserve-retry").addEventListener("click", () => reserve(true));
$("new-key").addEventListener("click", newKey);
["reserve-qty", "reserve-order", "reserve-ttl"].forEach((id) => $(id).addEventListener("input", newKey));
$("qty-dec").addEventListener("click", () => { $("reserve-qty").value = Math.max(1, Number($("reserve-qty").value) - 1); newKey(); });
$("qty-inc").addEventListener("click", () => { $("reserve-qty").value = Number($("reserve-qty").value) + 1; newKey(); });
$("race-n").addEventListener("input", (e) => ($("race-n-label").textContent = e.target.value));
$("race-distinct").addEventListener("click", () => race(false));
$("race-duplicate").addEventListener("click", () => race(true));
$("receive-form").addEventListener("submit", (e) => {
  e.preventDefault();
  managerAction("POST", `/inventory/${state.productId}/receive`, { quantity: Number($("receive-qty").value), reason: "Delivery received" },
    (d) => `Delivery received · ${d.on_hand_quantity} on hand`);
});
$("adjust-form").addEventListener("submit", (e) => {
  e.preventDefault();
  managerAction("POST", `/inventory/${state.productId}/adjust`, { delta: -Number($("adjust-qty").value), reason: $("adjust-reason").value },
    (d) => `Spoilage recorded · ${d.on_hand_quantity} on hand`);
});
$("expire-now").addEventListener("click", () => managerAction("POST", "/reservations/expire", undefined, (d) => `Expired ${d.expired} reservation(s)`));

newKey();
renderAudit(null);
const remembered = document.querySelector(`.user-chip[data-user="${localStorage.getItem("cellar-user") || "maria"}"]`);
if (remembered) login(remembered);
setInterval(refresh, 3000);
