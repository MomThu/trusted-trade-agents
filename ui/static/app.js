// Approval UI. Talks only to the human-only backend (/api/ui), never to agent tools.
const STAFF = { "X-Staff-Key": "demo-staff", "Content-Type": "application/json" };
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const eur = (x) => "€" + Number(x || 0).toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const qty = (x) => Number(x).toLocaleString("en-GB", { maximumFractionDigits: 2 });
const when = (iso) => iso ? new Date(iso).toLocaleString("en-GB", { weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "–";
let buyers = [];

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: STAFF, ...opts });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || r.statusText);
  return body;
}
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); setTimeout(() => t.classList.remove("show"), 2600); }
const buyerName = (id) => (buyers.find((b) => b.buyer_id === id) || {}).name || id;

const FLAG_TEXT = {
  supplier_stock_unknown: ["Supplier stock unknown", "bad"], later_than_requested: ["Later than requested", "bad"],
  low_reliability: ["Low delivery reliability", "bad"], unusual_quantity: ["Unusual quantity", "bad"],
  price_changed: ["Price rose since offer", "bad"], order_cutoff_passed: ["Cut-off passed", "bad"],
  partial_fulfilment: ["Partial", "bad"], possible_misunderstanding: ["AI: possible misunderstanding", ""],
  demo_scenario_override: ["Demo overlay data", "info"], reliability_unknown: ["Reliability unknown", "info"],
  allergen_policy_unconfirmed: ["Allergen policy not confirmed", "info"],
  agent_unresolved: ["Agent unsure", ""],
};
function flagChip(f) {
  if (f.startsWith("allergen_unknown:")) return `<span class="chip bad">Allergen data missing: ${esc(f.split(":")[1])}</span>`;
  const [t, c] = FLAG_TEXT[f] || [f, ""];
  return `<span class="chip ${c}">${esc(t)}</span>`;
}

// "Buyer said | Agent understood | Agent unsure about" in one row of matching boxes.
function saidUnderstood(o) {
  const doubt = o.need.unresolved;
  return `<div class="said-understood${doubt ? " three" : ""}">
    <div class="said"><h4>Buyer said</h4>“${esc(o.need.text)}”</div>
    <div class="understood"><h4>Agent understood</h4>${esc(o.summary)}</div>
    ${doubt ? `<div class="doubt"><h4>Agent unsure about</h4>${esc(doubt)}</div>` : ""}
  </div>`;
}

// Flags that belong to the whole order (not shown on any line): agent doubt, AI cross-check, partial, ...
function orderChips(o) {
  const onLines = new Set((o.lines || []).flatMap((l) => l.flags || []));
  const own = (o.flags || []).filter((f) => !onLines.has(f));
  return own.length ? `<div class="chips">${own.map(flagChip).join("")}</div>` : "";
}

function linesTable(o, removable) {
  return `<div class="table-wrap"><table>
    <thead><tr>${removable ? "<th>Keep</th>" : ""}<th>Product</th><th class="num">Qty</th><th class="num">Unit price</th><th class="num">Line</th>
    <th>Delivery</th><th class="num">Reliability</th><th class="num">CO₂e kg/kg*</th></tr></thead><tbody>
    ${o.lines.map((l) => `<tr>
      ${removable ? `<td><input type="checkbox" checked data-line="${l.line}"></td>` : ""}
      <td>${esc(l.name)} <span class="muted">${esc(l.product_code)}</span>
        <div class="chips">${l.is_substitution ? `<span class="chip sub">Official substitute${l.substitute_for ? " for " + esc([].concat(l.substitute_for).join(", ")) : ""}</span>` : ""}
        ${(l.flags || []).map(flagChip).join("")}
        ${l.offered_unit_price_eur !== l.unit_price_eur ? `<span class="chip bad">Offered ${eur(l.offered_unit_price_eur)} → now ${eur(l.unit_price_eur)}</span>` : ""}</div></td>
      <td class="num">${qty(l.quantity)} ${esc(l.sales_unit)}<div class="muted">${qty(l.covers)} ${esc(o.need.unit === "SALES_UNIT" ? "units" : o.need.unit)}</div></td>
      <td class="num">${eur(l.unit_price_eur)}</td><td class="num">${eur(l.line_total_eur)}</td>
      <td>${esc(l.available_date || "–")}<div class="muted">order by ${when(l.closing_time)}</div></td>
      <td class="num">${l.delivery_reliability == null ? "<span class='muted'>n/a</span>" : Math.round(l.delivery_reliability * 100) + "%"}</td>
      <td class="num">${l.carbon_kg_co2e_per_kg == null ? "<span class='muted'>n/a</span>" : l.carbon_kg_co2e_per_kg}</td>
    </tr>`).join("")}</tbody></table></div>
    <p class="muted" style="font-size:12px">Prices excl. VAT, fixed (no negotiation). *Carbon is a product-group average, not per product.</p>`;
}

async function loadApprovals() {
  const list = await api("/api/ui/orders?status=pending_approval");
  $("#pending-count").textContent = list.length;
  const el = $("#approvals");
  if (!list.length) { el.innerHTML = `<div class="empty">No orders waiting for approval.</div>`; return; }
  el.innerHTML = list.map((o) => `<article class="card" data-id="${o.order_id}">
    <div class="approval-head">
      <div><strong>${esc(buyerName(o.buyer_id))}</strong> <span class="muted">· ${esc(o.order_id)} · ${when(o.created_at)}</span></div>
      <div class="total">${eur(o.total_eur)} <span class="muted" style="font-size:12px;font-weight:400">excl. VAT</span></div>
    </div>
    ${orderChips(o)}
    ${saidUnderstood(o)}
    <strong>Why it needs you</strong>
    <ul class="reasons">${(o.policy_reasons || []).map((r) => `<li>${esc(r.message)}</li>`).join("")}</ul>
    ${linesTable(o, o.lines.length > 1)}
    ${(o.check_notes?.notes || []).length ? `<p class="muted">${o.check_notes.notes.map(esc).join("<br>")}</p>` : ""}
    <div class="actions">
      <input placeholder="Note for the audit log (optional)" class="note">
      <button class="approve">Approve and place</button>
      <button class="danger reject">Reject</button>
      <button class="ghost detail">History</button>
    </div>
  </article>`).join("");
  el.querySelectorAll("article").forEach((card) => {
    const id = card.dataset.id;
    const note = () => $(".note", card).value || null;
    $(".approve", card).onclick = async () => {
      const remove = [...card.querySelectorAll("input[data-line]")].filter((c) => !c.checked).map((c) => +c.dataset.line);
      try { await api(`/api/ui/orders/${id}/approve`, { method: "POST", body: JSON.stringify({ note: note(), remove_lines: remove.length ? remove : null }) }); toast("Approved and placed"); refresh(); }
      catch (e) { toast(e.message); }
    };
    $(".reject", card).onclick = async () => {
      try { await api(`/api/ui/orders/${id}/reject`, { method: "POST", body: JSON.stringify({ note: note() }) }); toast("Rejected"); refresh(); }
      catch (e) { toast(e.message); }
    };
    $(".detail", card).onclick = () => showOrder(id);
  });
}

async function loadOrders() {
  const b = $("#orders-buyer").value, s = $("#orders-status").value;
  const list = await api(`/api/ui/orders?${new URLSearchParams({ ...(b && { buyer_id: b }), ...(s && { status: s }) })}`);
  const t = $("#orders-table");
  if (!list.length) { t.innerHTML = `<tr><td class="muted" style="padding:24px">No orders yet. Run a preset in the Agent console.</td></tr>`; return; }
  t.innerHTML = `<thead><tr><th>Order</th><th>Buyer</th><th>Status</th><th>Agent understood</th><th class="num">Total</th><th>Cancel until</th><th></th></tr></thead><tbody>
  ${list.map((o) => `<tr class="clickable" data-id="${o.order_id}">
    <td>${esc(o.order_id)}<div class="muted">${when(o.created_at)}</div></td><td>${esc(buyerName(o.buyer_id))}</td>
    <td><span class="status ${o.status}">${esc(o.status.replace("_", " "))}</span></td>
    <td>${esc(o.summary)}${orderChips(o)}</td><td class="num">${eur(o.total_eur)}</td>
    <td>${o.cancellable && o.status !== "pending_approval" ? when(o.cancel_until) : "<span class='muted'>–</span>"}</td>
    <td>${o.cancellable ? `<button class="ghost cancel" data-id="${o.order_id}">Cancel</button>` : ""}</td></tr>`).join("")}</tbody>`;
  t.querySelectorAll("tr.clickable").forEach((tr) => tr.onclick = (e) => { if (!e.target.classList.contains("cancel")) showOrder(tr.dataset.id); });
  t.querySelectorAll("button.cancel").forEach((btn) => btn.onclick = async () => {
    try { await api(`/api/ui/orders/${btn.dataset.id}/cancel`, { method: "POST", body: JSON.stringify({ approver: "Buyer" }) }); toast("Cancelled"); refresh(); }
    catch (e) { toast(e.message); }
  });
}

async function showOrder(id) {
  const o = await api(`/api/ui/orders/${id}`);
  $("#order-detail").innerHTML = `<h3>${esc(o.order_id)} <span class="status ${o.status}">${esc(o.status.replace("_", " "))}</span></h3>
    ${orderChips(o)}
    ${saidUnderstood(o)}
    <p><b>Policy:</b> ${(o.policy_reasons || []).map((r) => esc(r.message)).join("; ")}</p>
    ${o.decided_by ? `<p><b>Decision:</b> ${esc(o.status)} by ${esc(o.decided_by)} ${when(o.decided_at)}${o.decision_note ? " – " + esc(o.decision_note) : ""}</p>` : ""}
    ${linesTable(o, false)}
    <h4>Declared need</h4><pre style="white-space:pre-wrap;font-size:12px">${esc(JSON.stringify(o.need, null, 2))}</pre>
    <h4>Audit trail</h4><ol class="audit">${o.audit.map(auditItem).join("")}</ol>`;
  $("#order-dialog").showModal();
}

const auditItem = (a) => `<li><span class="ts">${when(a.ts)}</span><span class="actor"><span class="ev">${esc(a.event)}</span><br><span class="muted">${esc(a.actor)}${a.buyer_id ? " · " + esc(a.buyer_id) : ""}</span></span><span>${esc(a.message)}${a.order_id ? ` <a href="#" data-order="${esc(a.order_id)}">${esc(a.order_id)}</a>` : ""}</span></li>`;

async function loadAudit() {
  const b = $("#audit-buyer").value;
  const list = await api(`/api/ui/audit${b ? "?buyer_id=" + encodeURIComponent(b) : ""}`);
  $("#audit").innerHTML = list.length ? list.map(auditItem).join("") : `<li class="muted">Nothing logged yet.</li>`;
  $("#audit").querySelectorAll("a[data-order]").forEach((a) => a.onclick = (e) => { e.preventDefault(); showOrder(a.dataset.order); });
}

const MANDATE_FIELDS = [
  ["spend_limit_eur", "Spend limit per order (€)", "number"],
  ["substitutions_need_approval", "Substitutions need approval", "bool"],
  ["min_delivery_reliability", "Min. delivery reliability (0–1)", "number"], ["unusual_quantity_factor", "Unusual quantity = × history avg", "number"],
  ["price_increase_tolerance_pct", "Tolerated price rise (%)", "number"],
];
let ALLERGEN_GROUPS = [];
function allergenPolicyHtml(b) {
  const p = b.mandate.allergen_policy || {}, ex = new Set(p.exclude || []);
  return `<fieldset class="allergens"><legend>Allergen policy: never deliver these (checked on every order)</legend>
    <div class="allergen-grid">${ALLERGEN_GROUPS.map((g) => `<label><input type="checkbox" name="allergen" value="${g}" ${ex.has(g) ? "checked" : ""}>${g}</label>`).join("")}</div>
    <p class="muted" style="font-size:12px">${p.confirmed_at
      ? `${ex.size ? "" : "None. "}Confirmed by ${esc(p.confirmed_by)} · ${esc(String(p.confirmed_at).slice(0, 10))}`
      : `<span class="chip bad">Not confirmed yet</span>`} · Missing manufacturer data for these always goes to a human.</p></fieldset>`;
}
async function loadMandates() {
  if (!ALLERGEN_GROUPS.length) ALLERGEN_GROUPS = (await api("/api/ui/vocab")).allergen_groups;
  $("#mandates").innerHTML = buyers.map((b) => `<form class="card" data-id="${b.buyer_id}">
    <h3>${esc(b.name)}</h3><p class="muted">buyer_id ${esc(b.buyer_id)} · key ${esc(b.api_key_hint)}</p>
    ${allergenPolicyHtml(b)}
    ${MANDATE_FIELDS.map(([k, label, type]) => `<label>${label}${type === "bool"
      ? `<input type="checkbox" name="${k}" ${b.mandate[k] ? "checked" : ""}>`
      : `<input type="number" step="any" name="${k}" value="${b.mandate[k] ?? ""}">`}</label>`).join("")}
    <button>Save mandate</button></form>`).join("");
  $("#mandates").querySelectorAll("form").forEach((f) => f.onsubmit = async (e) => {
    e.preventDefault();
    const body = {};
    MANDATE_FIELDS.forEach(([k, , type]) => { const i = f.elements[k]; body[k] = type === "bool" ? i.checked : (i.value === "" ? null : +i.value); });
    body.allergen_policy_exclude = [...f.querySelectorAll("input[name=allergen]:checked")].map((c) => c.value);
    try { await api(`/api/ui/buyers/${f.dataset.id}/mandate`, { method: "PUT", body: JSON.stringify(body) }); toast("Mandate saved and logged"); await loadBuyers(); }
    catch (err) { toast(err.message); }
  });
}

function renderEvents(events) {
  $("#transcript").innerHTML = events.map((e) => {
    if (e.kind === "say") return `<li class="${e.who === "buyer" ? "buyer" : "agent"}"><span class="who">${e.who === "buyer" ? "Buyer (person)" : "Buyer agent"}</span>${esc(e.text)}</li>`;
    if (e.kind === "tool_call") return `<li class="tool_call"><span class="who">Agent → Valio MCP</span>${esc(e.tool)}(${esc(JSON.stringify(e.args).slice(0, 700))}${JSON.stringify(e.args).length > 700 ? "…" : ""})</li>`;
    if (e.kind === "tool_result") return `<li class="tool_result ${/^(ERROR|REQUEST CHECK FAILED)/.test(e.brief) ? "err" : ""}"><span class="who">Valio → agent</span>${esc(e.brief)}</li>`;
    return `<li class="error"><span class="who">${esc(e.kind)}</span>${esc(e.text || JSON.stringify(e))}</li>`;
  }).join("");
}

async function runAgent(body, btn) {
  document.querySelectorAll("#tab-agent button").forEach((b) => b.disabled = true);
  $("#run-status").textContent = "· running…";
  $("#transcript").innerHTML = `<li class="muted">Agent is working: find_offers → place_order…</li>`;
  try {
    const r = await api("/api/ui/agent/run", { method: "POST", body: JSON.stringify(body) });
    renderEvents(r.events);
    $("#run-status").textContent = `· ${r.mode} agent · outcome: ${r.outcome || "none"}${r.order_id ? " · " + r.order_id : ""}`;
    if (!r.order_id) $("#transcript").insertAdjacentHTML("afterbegin", `<li class="error"><span class="who">No order was created</span>The agent stopped without ordering. Read its last message: it may be waiting for the buyer's answer. Nothing appears in Approvals.</li>`);
    else if (r.outcome === "pending_approval") $("#transcript").insertAdjacentHTML("afterbegin", `<li class="tool_result"><span class="who">Waiting for a human</span>Order ${esc(r.order_id)} is in the Approvals tab.</li>`);
    refresh();
  } catch (e) { $("#transcript").innerHTML = `<li class="error">${esc(e.message)}</li>`; $("#run-status").textContent = ""; }
  finally { document.querySelectorAll("#tab-agent button").forEach((b) => b.disabled = false); }
}

async function loadBuyers() {
  buyers = await api("/api/ui/buyers");
  for (const sel of ["#orders-buyer", "#audit-buyer"]) {
    const s = $(sel), keep = s.value, first = s.options[0].outerHTML;
    s.innerHTML = first + buyers.map((b) => `<option value="${b.buyer_id}">${esc(b.name)}</option>`).join(""); s.value = keep;
  }
  $("#llm-buyer").innerHTML = buyers.map((b) => `<option value="${b.buyer_id}">${esc(b.name)}</option>`).join("");
  loadMandates();
}

async function loadTop() {
  const [h, sc, presets] = await Promise.all([fetch("/health").then((r) => r.json()), api("/api/ui/scenario"), api("/api/ui/presets")]);
  $("#health").textContent = `Valio API ${h.valio_api_key ? "✓" : "✗ (no key)"} · Gemini ${h.gemini ? "✓" : "✗"}`;
  $("#llm-note").textContent = h.gemini ? "" : "GEMINI_API_KEY is not set in .env: use the scripted runs, or add a key and restart.";
  $("#llm-run").disabled = !h.gemini;
  const s = $("#scenario");
  s.innerHTML = `<option value="">Off (live data only)</option>` + Object.entries(sc.available).map(([k, d]) => `<option value="${k}" title="${esc(d)}">${k}</option>`).join("");
  s.value = sc.active || "";
  $("#presets").innerHTML = presets.map((p) => `<button class="preset" data-id="${p.id}"><b>${esc(p.title)}</b><span>${esc(buyerName(p.buyer_id))}: “${esc(p.text)}”</span></button>`).join("");
  $("#presets").querySelectorAll("button").forEach((b) => b.onclick = () => runAgent({ preset: b.dataset.id }, b));
}

async function refresh() {
  const active = document.querySelector(".tabs button.active").dataset.tab;
  await loadApprovals();
  if (active === "orders") await loadOrders();
  if (active === "audit") await loadAudit();
}

document.querySelectorAll(".tabs button").forEach((b) => b.onclick = () => {
  document.querySelectorAll(".tabs button, .tab").forEach((x) => x.classList.remove("active"));
  b.classList.add("active"); $("#tab-" + b.dataset.tab).classList.add("active");
  refresh();
});
$("#orders-buyer").onchange = $("#orders-status").onchange = loadOrders;
$("#audit-buyer").onchange = loadAudit;
$("#scenario").onchange = async (e) => { await api("/api/ui/scenario", { method: "POST", body: JSON.stringify({ name: e.target.value || null }) }); toast(e.target.value ? `Overlay '${e.target.value}' on` : "Overlay off: live data only"); };
$("#reset").onclick = async () => { if (!confirm("Delete all demo orders and audit entries?")) return; await api("/api/ui/reset", { method: "POST" }); toast("Demo reset"); loadTop(); refresh(); };
$("#llm-run").onclick = () => runAgent({ text: $("#llm-text").value, buyer_id: $("#llm-buyer").value });

(async () => { await loadBuyers(); await loadTop(); await refresh(); setInterval(() => loadApprovals().catch(() => {}), 5000); })();
