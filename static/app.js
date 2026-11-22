/* Invoice desk front end. Vanilla JS, hash routing, SSE for the live run view. */
const $ = (s, el = document) => el.querySelector(s);
const view = $("#view");
const S = { meta: null, samples: [], es: null };

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const inr = (n) => (n == null ? "-" : new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 2 }).format(n));
const inr0 = (n) => (n == null ? "-" : new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 }).format(n));
const pct = (n) => (n == null ? "-" : `${Math.round(n * 100)}%`);
const fmtDate = (iso) => (iso ? new Date(iso.length === 10 ? iso + "T00:00:00" : iso).toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" }) : "-");
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false }) : "-");
const LABEL = { approved: "Approved", needs_review: "Needs review", rejected: "Rejected", error: "Error", running: "Running" };
const STAMP = { approved: "APPROVED", needs_review: "NEEDS REVIEW", rejected: "REJECTED", error: "ERROR" };
const ICON = { pass: "✓", info: "i", warn: "!", fail: "!", block: "✕" };

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
}
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("show"), 3200);
}
function chip(decision, review) {
  if (!decision) return `<span class="chip running">Running</span>`;
  let h = `<span class="chip ${esc(decision)}">${esc(LABEL[decision] || decision)}</span>`;
  if (review) h += ` <span class="chip ${review === "approved" ? "ok" : "bad"}">Reviewer ${esc(review)}</span>`;
  return h;
}

/* ------------------------------------------------------------- router */
async function route() {
  if (S.es) { S.es.close(); S.es = null; }
  const [, page = "process", id] = location.hash.split("/");
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === page));
  refreshBadge();
  try {
    if (page === "process") await viewProcess(id ? Number(id) : null);
    else if (page === "runs") await viewRuns();
    else if (page === "review") await viewReview();
    else if (page === "reference") await viewReference();
    else if (page === "how") viewHow();
    else location.hash = "#/process";
  } catch (e) {
    view.innerHTML = `<div class="panel empty"><strong>Couldn't load this page</strong>${esc(e.message)}</div>`;
  }
  view.focus({ preventScroll: true });
}
async function refreshBadge() {
  try {
    const q = await api("/api/review-queue");
    const b = $("#review-badge");
    b.hidden = !q.length;
    b.textContent = q.length;
  } catch (_) {}
}

/* ------------------------------------------------------------- process */
async function viewProcess(runId) {
  view.innerHTML = `
  <div class="page-head"><div><h1>Process an invoice</h1>
    <p>Drop in a vendor invoice or pick a prepared one. Each step runs live on the right; the decision and its reasons appear at the end.</p></div></div>
  <div class="process">
    <section class="panel inbox" aria-label="Invoice inbox">
      <h2>Inbox</h2>
      <label class="drop" id="drop" tabindex="0">
        <strong>Upload an invoice</strong><span class="small">PDF, PNG or JPG. Drag it here or click to choose.</span>
        <input type="file" id="file" accept=".pdf,.png,.jpg,.jpeg" hidden>
      </label>
      <h3 style="margin-top:16px">Prepared test invoices</h3>
      <ul class="samples" id="samples"></ul>
    </section>
    <section id="run-area"></section>
  </div>`;
  const list = $("#samples");
  list.innerHTML = S.samples.map((s) => `
    <li><button class="sample" data-sample="${esc(s.file)}" title="${esc(s.scenario)}">
      <span class="t">${esc(s.title)}</span><span class="expect ${esc(s.expect)}">${esc(LABEL[s.expect])}</span>
      <span class="d">${esc(s.scenario)}</span></button></li>`).join("");
  list.onclick = (e) => { const b = e.target.closest("[data-sample]"); if (b) startRun({ sample: b.dataset.sample }); };
  const drop = $("#drop"), file = $("#file");
  file.onchange = () => file.files[0] && startRun({ file: file.files[0] });
  drop.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); file.click(); } };
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => e.dataTransfer.files[0] && startRun({ file: e.dataTransfer.files[0] }));

  if (runId) await showRun(runId);
  else $("#run-area").innerHTML = `<div class="panel empty"><strong>No invoice running</strong>Pick a test invoice from the inbox, or upload one, to watch it go through the nine checks.</div>`;
}

async function startRun({ sample, file }) {
  const fd = new FormData();
  if (sample) fd.append("sample", sample);
  if (file) fd.append("file", file);
  try {
    const { run_id } = await api("/api/runs", { method: "POST", body: fd });
    location.hash = `#/process/${run_id}`;
  } catch (e) { toast(e.message); }
}

function runSkeleton(run) {
  const stages = S.meta.stages;
  return `
  <div class="run-head">
    <div><div class="muted small">Run #${run.id} received ${fmtTime(run.created_at)}</div><div class="file">${esc(run.filename)}</div></div>
    <div id="run-status">${run.status === "running" ? `<span class="chip running">Running</span>` : chip(run.decision, run.review_decision)}</div>
  </div>
  <div class="run-grid">
    <section class="panel"><h2>Steps</h2>
      <ol class="timeline" id="timeline">
        ${stages.map((s) => `<li class="step" data-stage="${s.key}">
          <button class="step-btn" aria-expanded="false"><span class="step-name">${esc(s.name)}</span><span class="step-ms"></span><span class="step-what">${esc(s.what)}</span></button>
          <div class="step-findings"></div></li>`).join("")}
      </ol>
    </section>
    <section id="decision-area"><div class="panel empty"><strong>Working through the checks</strong>The decision appears here when the last step finishes.</div></section>
  </div>`;
}

function findingHTML(f) {
  return `<div class="finding ${esc(f.severity)}"><span class="ic" aria-label="${esc(f.severity)}">${ICON[f.severity] || "·"}</span>
    <div><div class="ft">${esc(f.title)}</div>${f.detail ? `<div class="fd">${esc(f.detail)}</div>` : ""}</div></div>`;
}

function applyEvent(e) {
  const li = document.querySelector(`.step[data-stage="${e.stage}"]`);
  if (e.kind === "error") {
    $("#decision-area").innerHTML = `<div class="panel"><h2>The process stopped with an error</h2><pre class="raw">${esc(e.data.error)}</pre></div>`;
    return;
  }
  if (!li) return;
  if (e.kind === "start") { li.className = "step running"; return; }
  if (e.kind === "end") {
    li.className = `step ${e.status}`;
    li.querySelector(".step-ms").textContent = e.data.ms != null ? `${(e.data.ms / 1000).toFixed(1)}s` : "";
    const fs = e.data.findings || [];
    li.querySelector(".step-findings").innerHTML = fs.map(findingHTML).join("");
    if (["warn", "fail", "block"].includes(e.status) || e.stage === "decide") {
      li.classList.add("open");
      li.querySelector(".step-btn").setAttribute("aria-expanded", "true");
    }
  }
}

function wireTimeline() {
  $("#timeline").onclick = (e) => {
    const b = e.target.closest(".step-btn");
    if (!b) return;
    const li = b.parentElement;
    li.classList.toggle("open");
    b.setAttribute("aria-expanded", li.classList.contains("open"));
  };
}

async function showRun(id) {
  const run = await api(`/api/runs/${id}`);
  $("#run-area").innerHTML = runSkeleton(run);
  wireTimeline();
  if (run.status !== "running") {
    run.events.forEach(applyEvent);
    markSkipped();
    renderDecision(run);
    return;
  }
  S.es = new EventSource(`/api/runs/${id}/stream`);
  S.es.onmessage = (m) => applyEvent(JSON.parse(m.data));
  S.es.addEventListener("done", async () => {
    S.es.close(); S.es = null;
    const done = await api(`/api/runs/${id}`);
    markSkipped();
    $("#run-status").innerHTML = chip(done.decision, done.review_decision);
    renderDecision(done);
    refreshBadge();
  });
  S.es.onerror = () => { /* browser retries automatically; stream ends on done */ };
}
function markSkipped() {
  document.querySelectorAll(".step").forEach((li) => { if (li.className === "step") li.classList.add("skipped"); });
}

function renderDecision(run) {
  const res = run.result;
  const area = $("#decision-area");
  if (!res) { area.innerHTML = `<div class="panel"><h2>No result</h2><p>${esc(run.summary || "")}</p></div>`; return; }
  const d = res.stages.decide?.data || {};
  const exc = d.exceptions || [];
  const reviewed = run.review_decision
    ? `<p class="small" style="margin-top:10px"><span class="chip ${run.review_decision === "approved" ? "ok" : "bad"}">Reviewer ${esc(run.review_decision)}</span> by ${esc(run.reviewed_by)} on ${fmtTime(run.reviewed_at)}: "${esc(run.review_comment)}"</p>` : "";
  const reviewLink = run.decision === "needs_review" && !run.review_decision
    ? `<p style="margin-top:10px"><a class="btn" href="#/review">Open in review queue</a></p>` : "";
  area.innerHTML = `
  <div class="panel decision">
    <div class="decision-top">
      <div class="stamp ${esc(res.decision)}">${STAMP[res.decision] || esc(res.decision)}</div>
      <div><div class="decision-summary">${esc(res.summary)}</div>
      <div class="queue">Routed to <b>${esc(res.queue)}</b>${res.invoice?.total != null ? ` for ${inr(res.invoice.total)}` : ""}</div></div>
    </div>
    ${reviewed}${reviewLink}
    <div class="decision-cols">
      <div><h3>${exc.length ? `Why (${exc.length})` : "Why"}</h3>
        ${exc.length ? `<ul class="exc">${exc.map((f) => `<li class="${f.severity}"><b>${esc(f.title)}</b>${esc(f.detail)}</li>`).join("")}</ul>`
                     : `<p class="muted">Every check passed. Nothing for a person to look at.</p>`}</div>
      <div><h3>What happens next</h3><ul class="actions">${(res.next_actions || []).map((a) => `<li>${esc(a)}</li>`).join("")}</ul></div>
    </div>
  </div>
  <div class="panel">
    <div class="tabs" role="tablist">
      ${["Fields", "PO match", `Messages${res.communications?.length ? ` (${res.communications.length})` : ""}`, "Document", "Audit trail", "Raw text"]
        .map((t, i) => `<button class="tab" role="tab" data-tab="${i}" aria-selected="${i === 0}">${t}</button>`).join("")}
    </div>
    <div id="tab-body"></div>
  </div>`;
  const tabs = [() => tabExtracted(res), () => tabPO(res), () => tabMessages(res), () => tabDoc(run), () => tabAudit(run), () => tabRaw(res)];
  const body = $("#tab-body");
  body.innerHTML = tabs[0]();
  area.querySelector(".tabs").onclick = (e) => {
    const b = e.target.closest(".tab"); if (!b) return;
    area.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-selected", t === b));
    body.innerHTML = tabs[Number(b.dataset.tab)]();
  };
}

function tabExtracted(res) {
  const inv = res.invoice || {};
  const c = inv._confidence || {};
  const row = (k, label, v) => {
    const conf = c[k];
    const missing = v == null || v === "";
    return `<div class="k">${label}</div><div class="${missing ? "missing" : ""}">${missing ? "not found" : esc(v)}</div>
      <div class="conf ${conf != null && conf < 0.8 ? "low" : ""}">${conf != null && !missing ? pct(conf) : ""}</div>`;
  };
  const ex = res.stages.extract?.data || {};
  const bank = inv.bank || {};
  return `
  <p class="small muted">Read by <b>${esc(ex.method)}</b> from the ${res.doc?.method === "ocr" ? `scanned image (OCR ${pct(res.doc.ocr_confidence)})` : "PDF text layer"}. Overall confidence ${pct(ex.confidence)}. Right-hand column is per-field confidence.</p>
  <div class="kv">
    ${row("vendor_name", "Vendor", inv.vendor_name)}
    ${row("vendor_gstin", "Vendor GSTIN", inv.vendor_gstin)}
    ${row("invoice_number", "Invoice number", inv.invoice_number)}
    ${row("invoice_date", "Invoice date", inv.invoice_date && fmtDate(inv.invoice_date))}
    ${row("due_date", "Due date", inv.due_date && fmtDate(inv.due_date))}
    ${row("po_number", "PO reference", inv.po_number)}
    ${row("subtotal", "Taxable value", inv.subtotal != null ? inr(inv.subtotal) : null)}
    ${row("tax_total", "GST", (inv.tax_lines || []).length ? inv.tax_lines.map((t) => `${t.type} ${t.rate}% ${inr(t.amount)}`).join(", ") : null)}
    ${row("total", "Total", inv.total != null ? inr(inv.total) : null)}
    ${row("bank", "Remit-to bank", bank.account ? `${bank.bank_name || ""} a/c ${bank.account} ${bank.ifsc || ""}` : null)}
  </div>
  <h3 style="margin-top:16px">Line items</h3>
  <table><thead><tr><th>Description</th><th class="num">Qty</th><th class="num">Rate</th><th class="num">Amount</th></tr></thead>
  <tbody>${(inv.line_items || []).map((l) => `<tr><td>${esc(l.description)}</td><td class="num">${esc(l.qty)}</td><td class="num">${inr(l.unit_price)}</td><td class="num">${inr(l.amount)}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">No line items read</td></tr>`}</tbody></table>
  ${(inv.notes || []).length ? `<h3 style="margin-top:16px">Remarks on the invoice</h3>${inv.notes.map((n) => `<p>"${esc(n)}"</p>`).join("")}` : ""}`;
}

function tabPO(res) {
  const data = res.stages.po?.data;
  if (!data) return `<p class="muted">PO matching didn't run for this invoice.</p>`;
  const po = data.po;
  const inferred = (res.stages.po.findings || []).find((f) => f.code === "PO_INFERRED" || f.code === "PO_UNMATCHED");
  const cands = inferred?.evidence?.candidates || [];
  const lines = data.lines || [];
  return `
  ${po ? `<div class="kv" style="grid-template-columns:150px 1fr">
     <div class="k">PO</div><div><b>${esc(po.po_number)}</b> ${data.inferred ? `<span class="chip warn">inferred</span>` : ""} ${esc(po.description)}</div>
     <div class="k">Raised</div><div>${fmtDate(po.po_date)} by ${esc(po.buyer_contact)}</div>
     <div class="k">PO value</div><div>${inr(po.total)}</div>
     <div class="k">Billed before this</div><div>${inr(po.billed_value)} (remaining ${inr(po.remaining_value)})</div></div>` : `<p>No PO matched.</p>`}
  ${cands.length ? `<h3 style="margin-top:16px">How the PO was chosen</h3>
    <table><thead><tr><th>Open PO for this vendor</th><th class="num">Match score</th></tr></thead><tbody>
    ${cands.map((c) => `<tr><td><b>${esc(c.po)}</b> ${esc(c.description)}</td><td class="num">${pct(c.score)}</td></tr>`).join("")}</tbody></table>
    <p class="small muted">Score = 60% line-description similarity + 40% how closely the invoice value fits the PO prices.</p>` : ""}
  ${lines.length ? `<h3 style="margin-top:16px">Three-way match (invoice vs PO vs goods receipt)</h3>
  <div style="overflow-x:auto"><table><thead><tr><th>Invoice line</th><th class="num">Billed qty</th><th class="num">Rate</th><th class="num">PO rate</th><th class="num">Ordered</th><th class="num">Received</th><th class="num">Billed before</th><th class="num">Still billable</th><th>Result</th></tr></thead>
  <tbody>${lines.map((l) => `<tr><td>${esc(l.invoice_line)}${l.po_line ? `<div class="small muted">PO line ${l.po_line}, ${Math.round(l.similarity)}% text match</div>` : ""}</td>
    <td class="num">${l.qty}</td><td class="num">${inr(l.unit_price)}</td><td class="num">${l.po_price != null ? inr(l.po_price) : "-"}${l.price_var_pct ? `<div class="small ${l.price_var_pct > 1 ? "" : "muted"}">${l.price_var_pct > 0 ? "+" : ""}${l.price_var_pct}%</div>` : ""}</td>
    <td class="num">${l.ordered ?? "-"}</td><td class="num">${l.received ?? "-"}</td><td class="num">${l.billed_before ?? "-"}</td><td class="num">${l.billable ?? "-"}</td>
    <td>${l.status === "ok" ? `<span class="chip ok">OK</span>` : `<span class="chip warn">${esc(l.status)}</span>`}</td></tr>`).join("")}</tbody></table></div>` : ""}`;
}

function tabMessages(res) {
  const m = res.communications || [];
  if (!m.length) return `<p class="muted">No messages needed. Approved invoices go straight to the payment run.</p>`;
  return `<p class="small muted">Drafted for a person to send. Nothing is sent automatically.</p>` + m.map((x, i) => `
    <div class="msg"><div class="msg-h"><div><b>${esc(x.subject)}</b>To ${esc(x.audience)}: ${esc(x.to)}</div>
    <button class="btn" onclick="navigator.clipboard.writeText(document.getElementById('msg${i}').innerText).then(()=>toast('Copied'))">Copy</button></div>
    <pre id="msg${i}">${esc(x.body)}</pre></div>`).join("");
}
function tabDoc(run) {
  return `<iframe class="docframe" src="/api/runs/${run.id}/file" title="Original invoice"></iframe>`;
}
function tabAudit(run) {
  return `<table><thead><tr><th>Time</th><th>Step</th><th>Event</th><th>Result</th></tr></thead><tbody>
  ${run.events.map((e) => `<tr><td class="small">${fmtTime(e.ts)}</td><td>${esc(e.stage)}</td><td>${esc(e.kind === "start" ? "started" : e.kind === "end" ? "finished" : e.kind)}: ${esc(e.title)}${e.data?.comment ? `<div class="small muted">"${esc(e.data.comment)}"</div>` : ""}</td>
  <td>${e.kind === "start" ? "" : `<span class="chip ${({ pass: "ok", info: "ok", warn: "warn", fail: "warn", block: "bad" })[e.status] || "neutral"}">${esc(e.status)}</span>`}</td></tr>`).join("")}</tbody></table>`;
}
function tabRaw(res) {
  return `<p class="small muted">What the reader got out of the document before extraction.</p><pre class="raw">${esc(res.stages.read?.data?.text || "")}</pre>`;
}

/* ------------------------------------------------------------- dashboard */
async function viewRuns() {
  const [d, runs] = await Promise.all([api("/api/dashboard"), api("/api/runs")]);
  const maxExc = Math.max(1, ...d.exceptions.map((e) => e[1]));
  view.innerHTML = `
  <div class="page-head"><div><h1>Dashboard</h1><p>Every invoice the desk has processed, what it decided, and where the money is.</p></div>
    <a class="btn primary" href="#/process">Process an invoice</a></div>
  <div class="kpis">
    <div class="kpi"><div class="v">${d.total}</div><div class="l">Invoices processed</div><div class="s">${d.avg_seconds}s average per invoice</div></div>
    <div class="kpi"><div class="v">${pct(d.touchless_rate)}</div><div class="l">Approved with no human touch</div><div class="s">${d.approved} of ${d.total}, ${inr0(d.value_approved)} cleared in total</div></div>
    <div class="kpi"><div class="v">${d.open_reviews}</div><div class="l">Waiting for review</div><div class="s">${inr0(d.value_held)} on hold</div></div>
    <div class="kpi"><div class="v">${inr0(d.value_stopped)}</div><div class="l">Stopped from being paid</div><div class="s">${d.rejected} invoice${d.rejected === 1 ? "" : "s"} rejected</div></div>
  </div>
  <div class="dash-grid">
    <section class="panel">
      <div class="filters">
        <input id="q" type="search" placeholder="Search vendor, invoice, PO or file" aria-label="Search runs">
        <div class="seg" role="group" aria-label="Filter by decision">
          ${[["", "All"], ["approved", "Approved"], ["needs_review", "Needs review"], ["rejected", "Rejected"]].map(([v, l]) => `<button data-f="${v}" aria-pressed="${v === ""}">${l}</button>`).join("")}
        </div>
      </div>
      <div style="overflow-x:auto"><table><thead><tr><th>Run</th><th>Received</th><th>Vendor</th><th>Invoice</th><th>PO</th><th class="num">Total</th><th>Read by</th><th>Decision</th><th>Routed to</th></tr></thead>
      <tbody id="runs-body"></tbody></table></div>
    </section>
    <section class="panel"><h2>Most common exceptions</h2>
      ${d.exceptions.length ? `<ul class="bars">${d.exceptions.map(([code, n]) => `<li title="${n} invoice(s)"><span>${esc(code.replaceAll("_", " ").toLowerCase())}</span><span class="n">${n}</span><span class="track"><span class="fill" style="width:${(n / maxExc) * 100}%"></span></span></li>`).join("")}</ul>`
        : `<p class="muted">No exceptions yet.</p>`}
    </section>
  </div>`;
  let filter = "", q = "";
  const draw = () => {
    const rows = runs.filter((r) => (!filter || r.decision === filter) &&
      (!q || [r.vendor_name, r.invoice_number, r.po_number, r.filename].join(" ").toLowerCase().includes(q)));
    $("#runs-body").innerHTML = rows.map((r) => `<tr class="click" data-id="${r.id}" tabindex="0">
      <td>#${r.id}</td><td class="small nw">${fmtTime(r.created_at)}</td><td>${esc(r.vendor_name || "-")}</td><td class="nw">${esc(r.invoice_number || "-")}</td>
      <td class="nw">${esc(r.po_number || "-")}</td><td class="num nw">${inr(r.total)}</td><td class="small">${esc(r.extraction_method || "-")}</td>
      <td>${chip(r.status === "running" ? null : r.decision, r.review_decision)}</td><td class="small">${esc(r.queue || "")}</td></tr>`).join("")
      || `<tr><td colspan="9" class="empty"><strong>No runs yet</strong><a href="#/process">Process an invoice</a> to see it here.</td></tr>`;
  };
  draw();
  $("#q").oninput = (e) => { q = e.target.value.toLowerCase(); draw(); };
  view.querySelector(".seg").onclick = (e) => {
    const b = e.target.closest("button"); if (!b) return;
    filter = b.dataset.f;
    view.querySelectorAll(".seg button").forEach((x) => x.setAttribute("aria-pressed", x === b));
    draw();
  };
  const open = (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) location.hash = `#/process/${tr.dataset.id}`; };
  $("#runs-body").onclick = open;
  $("#runs-body").onkeydown = (e) => e.key === "Enter" && open(e);
}

/* ------------------------------------------------------------- review */
async function viewReview() {
  const items = await api("/api/review-queue");
  view.innerHTML = `
  <div class="page-head"><div><h1>Review queue</h1><p>Invoices the desk couldn't clear on its own. Each one says what's wrong and what to do about it. Approving an exception needs a reason, and it's kept in the audit trail.</p></div></div>
  <div id="rq-list">${items.length ? "" : `<div class="panel empty"><strong>Nothing waiting</strong>Held invoices will show up here.</div>`}</div>`;
  $("#rq-list").innerHTML += items.map((r) => `
    <section class="panel rq" data-id="${r.id}">
      <div>
        <div class="muted small">Run #${r.id}, ${fmtTime(r.created_at)}, routed to <b>${esc(r.queue)}</b></div>
        <h2 style="margin:4px 0 6px">${esc(r.vendor_name || "Unknown vendor")}, ${esc(r.invoice_number || "no number")}, ${inr(r.total)}</h2>
        <p class="small">${esc(r.po_number ? `Against ${r.po_number}. ` : "")}${esc(r.summary)}</p>
        <ul class="exc">${r.exceptions.map((f) => `<li class="${f.severity}"><b>${esc(f.title)}</b>${esc(f.detail)}</li>`).join("")}</ul>
        <a href="#/process/${r.id}">Open full run</a>
      </div>
      <div>
        <h3>Suggested next steps</h3>
        <ul class="actions small">${r.next_actions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>
        <label class="small" for="c${r.id}" style="display:block;margin-top:10px">Reviewer comment</label>
        <textarea id="c${r.id}" placeholder="What did you check, and with whom?"></textarea>
        <div class="btns"><button class="btn ok" data-act="approve">Approve for payment</button><button class="btn bad" data-act="reject">Reject</button></div>
      </div>
    </section>`).join("");
  $("#rq-list").onclick = async (e) => {
    const b = e.target.closest("[data-act]"); if (!b) return;
    const card = b.closest("[data-id]"), id = card.dataset.id;
    try {
      await api(`/api/runs/${id}/review`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: b.dataset.act, comment: $(`#c${id}`).value, reviewer: "AP reviewer" }) });
      toast(b.dataset.act === "approve" ? `Run #${id} approved for payment` : `Run #${id} rejected`);
      viewReview(); refreshBadge();
    } catch (err) { toast(err.message); }
  };
}

/* ------------------------------------------------------------- reference */
async function viewReference() {
  const d = await api("/api/reference");
  view.innerHTML = `
  <div class="page-head"><div><h1>POs and vendors</h1><p>The source of truth the desk checks against. Balances update as invoices are approved.</p></div>
    <button class="btn" id="reset">Reset demo data</button></div>
  <section class="panel"><h2>Purchase orders</h2><div style="overflow-x:auto"><table>
    <thead><tr><th>PO</th><th>Vendor</th><th>Line</th><th class="num">Unit price</th><th class="num">Ordered</th><th class="num">Received</th><th class="num">Billed</th><th class="num">Still billable</th></tr></thead><tbody>
    ${d.pos.map((p) => p.lines.map((l, i) => `<tr>${i === 0 ? `<td rowspan="${p.lines.length}"><b>${esc(p.po_number)}</b><div class="small muted">${esc(p.description)}</div><div class="small">${inr(p.total)}, remaining ${inr(p.remaining_value)}</div></td>
      <td rowspan="${p.lines.length}">${esc(d.vendors.find((v) => v.vendor_id === p.vendor_id)?.name || p.vendor_id)}</td>` : ""}
      <td>${esc(l.description)} <span class="small muted">GST ${l.tax_rate}%</span></td><td class="num">${inr(l.unit_price)}</td><td class="num">${l.qty_ordered}</td><td class="num">${l.qty_received}</td><td class="num">${l.qty_billed}</td>
      <td class="num">${l.qty_billable === 0 ? `<span class="chip neutral">0</span>` : l.qty_billable}</td></tr>`).join("")).join("")}
  </tbody></table></div></section>
  <section class="panel"><h2>Vendor master</h2><div style="overflow-x:auto"><table>
    <thead><tr><th>Vendor</th><th>GSTIN</th><th>Status</th><th>Bank on file</th><th class="num">Terms</th><th>Phone on file</th></tr></thead><tbody>
    ${d.vendors.map((v) => `<tr><td><b>${esc(v.name)}</b><div class="small muted">${esc(v.vendor_id)}, ${esc(v.city)}</div></td><td>${esc(v.gstin)}</td>
      <td><span class="chip ${v.status === "approved" ? "ok" : "bad"}">${esc(v.status)}</span>${v.status_reason ? `<div class="small muted">${esc(v.status_reason)}</div>` : ""}</td>
      <td>${esc(v.bank_name)} ${esc(v.bank_account)}<div class="small muted">${esc(v.ifsc)}</div></td><td class="num">${v.payment_terms_days} days</td><td class="small">${esc(v.contact_phone)}</td></tr>`).join("")}
  </tbody></table></div></section>
  <section class="panel"><h2>AP ledger</h2><table>
    <thead><tr><th>Vendor</th><th>Invoice</th><th>Date</th><th>PO</th><th class="num">Total</th><th>Status</th><th>Source</th></tr></thead><tbody>
    ${d.ledger.map((l) => `<tr><td>${esc(d.vendors.find((v) => v.vendor_id === l.vendor_id)?.name || l.vendor_id)}</td><td>${esc(l.invoice_number)}</td><td>${fmtDate(l.invoice_date)}</td><td>${esc(l.po_number)}</td>
      <td class="num">${inr(l.total)}</td><td><span class="chip ok">${esc(l.status)}</span></td><td class="small">${l.run_id ? `<a href="#/process/${l.run_id}">run #${l.run_id}</a>` : "history"}</td></tr>`).join("")}
  </tbody></table></section>`;
  $("#reset").onclick = async () => {
    if (!confirm("Clear all runs and restore the starting ledger?")) return;
    await api("/api/reset", { method: "POST" });
    toast("Demo data reset");
    viewReference(); refreshBadge();
  };
}

/* ------------------------------------------------------------- how */
function viewHow() {
  const p = S.meta.policy;
  view.innerHTML = `
  <div class="page-head"><div><h1>How it decides</h1><p>The LLM only reads the document. Every decision comes from the rules below, so the same invoice always gets the same answer and every answer has a reason.</p></div></div>
  <div class="dash-grid">
  <section class="panel howto"><h2>The nine steps</h2><ol>
    ${S.meta.stages.map((s) => `<li><b>${esc(s.name)}.</b> ${esc(s.what)}</li>`).join("")}</ol>
    <h2 style="margin-top:18px">Outcomes</h2>
    <p><span class="chip approved">Approved</span> every check passed and the invoice is under the auto-approval limit. Posted to the ledger and scheduled for payment.</p>
    <p><span class="chip needs_review">Needs review</span> something a person should look at (price or quantity off, PO had to be guessed, bank details changed, tax looks wrong). Routed to the team that can fix it, with a drafted message.</p>
    <p><span class="chip rejected">Rejected</span> the invoice can't be paid as sent (duplicate, blocked or unknown vendor, missing invoice number or date). A message to the vendor is drafted.</p>
  </section>
  <section class="panel"><h2>Policy in force</h2><table class="policy"><tbody>
    <tr><td>Price tolerance per line</td><td>${p.unit_price_tolerance_pct}%</td></tr>
    <tr><td>Invoice value tolerance</td><td>lesser of ${p.amount_tolerance_pct}% or ${inr0(p.amount_tolerance_abs)}</td></tr>
    <tr><td>Auto-approval limit</td><td>${inr0(p.auto_approve_limit)}</td></tr>
    <tr><td>Oldest acceptable invoice</td><td>${p.max_invoice_age_days} days</td></tr>
    <tr><td>Near-duplicate window</td><td>${p.fuzzy_duplicate_window_days} days</td></tr>
    <tr><td>Auto-approve when PO is inferred</td><td>${p.auto_approve_inferred_po ? "Yes" : "No, hold for review"}</td></tr>
    <tr><td>Minimum extraction confidence</td><td>${pct(p.min_extraction_confidence)}</td></tr>
  </tbody></table>
  <p class="small muted" style="margin-top:10px">Buyer: ${esc(S.meta.buyer.name)}, GSTIN ${esc(S.meta.buyer.gstin)}</p></section>
  </div>`;
}

/* ------------------------------------------------------------- boot */
(async function boot() {
  [S.meta, S.samples] = await Promise.all([api("/api/meta"), api("/api/samples")]);
  $("#llm-status").innerHTML = S.meta.llm.enabled
    ? `Extraction: <b>${esc(S.meta.llm.provider)} ${esc(S.meta.llm.model)}</b> cross-checked by rules`
    : `Extraction: <b>rules-based parser</b>. Set an LLM key to add model extraction.`;
  window.addEventListener("hashchange", route);
  if (!location.hash) location.hash = "#/process";
  else route();
})();
