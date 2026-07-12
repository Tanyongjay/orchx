"""Dashboard HTML for the OrchX control plane.

This module owns the single-page dashboard that's served
at GET /. The HTML is a self-contained string literal that
expands ``{token}`` placeholders at module load time via
a tiny JS ``init()`` function.

We split it out of app.py so the FastAPI app definition
isn't 500 lines of embedded HTML. The single public
symbol is ``INDEX_HTML``; the rest of the dashboard code
(JS, CSS) lives inside the string.
"""

#: The full HTML+JS+CSS for the dashboard. The string
#: is processed at import time by the init() call
#: at the bottom of this module, which substitutes
#: ``{token}`` placeholders like ``{ORCHX_AUTH_MODE}``.
INDEX_HTML = """
<!doctype html>
<html><head>
<meta charset="utf-8">
<title>OrchX control plane</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body {
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    margin: 0; padding: 0;
    background: #0d1117; color: #e6edf3;
  }
  header {
    background: #161b22; border-bottom: 1px solid #30363d;
    padding: 12px 24px; display: flex; align-items: baseline; gap: 12px;
  }
  header h1 { font-size: 16px; margin: 0; font-weight: 600; }
  header .tagline { color: #8b949e; font-size: 12px; }
  main { max-width: 1100px; margin: 0 auto; padding: 24px; display: grid; grid-template-columns: 320px 1fr; gap: 24px; }
  .panel { background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 16px; }
  .panel h2 { margin: 0 0 12px; font-size: 13px; text-transform: uppercase; color: #8b949e; letter-spacing: 0.05em; }
  label { display: block; font-size: 12px; color: #8b949e; margin-bottom: 4px; }
  input, select {
    width: 100%; background: #0d1117; color: #e6edf3; border: 1px solid #30363d;
    padding: 6px 8px; border-radius: 4px; font: inherit;
  }
  button {
    background: #238636; color: white; border: none; padding: 7px 14px;
    border-radius: 4px; cursor: pointer; font: inherit; font-weight: 500;
  }
  button:disabled { background: #30363d; cursor: not-allowed; opacity: 0.6; }
  button.danger { background: #da3633; }
  button.secondary { background: #21262d; border: 1px solid #30363d; }
  .row { display: flex; gap: 8px; margin-top: 12px; }
  .runs-list { list-style: none; padding: 0; margin: 0; max-height: 70vh; overflow-y: auto; }
  .runs-list li {
    padding: 10px; border: 1px solid #30363d; border-radius: 4px;
    margin-bottom: 6px; cursor: pointer;
  }
  .runs-list li:hover { background: #1f242c; }
  .runs-list li.selected { border-color: #58a6ff; background: #1f242c; }
  .runs-list .row { display: flex; justify-content: space-between; align-items: center; margin: 0; }
  .runs-list .id { font-family: ui-monospace, SFMono-Regular, monospace; font-size: 11px; color: #8b949e; }
  .badge { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 11px; font-weight: 500; }
  .badge.ok { background: rgba(46, 160, 67, 0.15); color: #3fb950; }
  .badge.failed { background: rgba(248, 81, 73, 0.15); color: #f85149; }
  .badge.running { background: rgba(88, 166, 255, 0.15); color: #58a6ff; }
  .badge.pending { background: rgba(187, 128, 9, 0.15); color: #d29922; }
  .badge.aborted { background: rgba(139, 148, 158, 0.15); color: #8b949e; }
  .events { font-family: ui-monospace, SFMono-Regular, monospace; font-size: 12px; }
  .event { padding: 4px 8px; border-bottom: 1px solid #21262d; display: flex; gap: 8px; align-items: center; }
  .event .status { width: 70px; flex-shrink: 0; }
  .event .step { color: #d2a8ff; flex-shrink: 0; }
  .event .host { color: #8b949e; font-size: 11px; }
  .event .msg { color: #e6edf3; }
  .event.status-ok .status { color: #3fb950; }
  .event.status-failed .status { color: #f85149; }
  .event.status-rolled_back .status { color: #d29922; }
  .event.status-running .status { color: #58a6ff; }
  .event.status-pending .status { color: #d29922; }
  .empty { color: #8b949e; font-style: italic; padding: 20px; text-align: center; }
  code { font-family: ui-monospace, SFMono-Regular, monospace; font-size: 12px; background: #0d1117; padding: 1px 4px; border-radius: 3px; }
</style>
</head>
<body>
<header>
  <h1>OrchX control plane</h1>
  <span class="tagline">Multi-system deploy orchestrator</span>
  <span class="tagline" style="margin-left:auto"><a href="/api/runs" style="color:#58a6ff">JSON API</a> · <a href="/healthz" style="color:#58a6ff">healthz</a> · <a href="#" id="signout" style="color:#58a6ff;display:none" onclick="event.preventDefault(); logout()">Sign out</a></span>
</header>
<main>
  <section class="panel" id="new-run">
    <h2>New run</h2>
    <label for="descriptor">Descriptor</label>
    <select id="descriptor"></select>
    <label for="target" style="margin-top:10px">Target URI</label>
    <input id="target" type="text" value="mock://local" placeholder="mock://local, ssh://user@host, winrm://user:pwd@host">
    <div class="row">
      <button id="submit" onclick="submitRun()">Deploy</button>
      <button class="secondary" onclick="refreshRuns()">Refresh</button>
    </div>
  </section>

  <section class="panel" id="runs-panel">
    <h2>Runs</h2>
    <div class="row" style="margin-bottom:8px">
      <label style="font-size:12px;color:#8b949e">
        State:
        <select id="state-filter" onchange="onFilterChange()" style="margin-left:4px">
          <option value="">all</option>
          <option value="pending">pending</option>
          <option value="running">running</option>
          <option value="ok">ok</option>
          <option value="failed">failed</option>
          <option value="aborted">aborted</option>
        </select>
      </label>
      <span id="runs-summary" style="margin-left:auto;font-size:12px;color:#8b949e"></span>
    </div>
    <ul class="runs-list" id="runs"></ul>
    <div class="row" style="margin-top:8px;align-items:center">
      <button class="secondary" id="prev-page" onclick="prevPage()" disabled>&laquo; Prev</button>
      <span id="page-info" style="margin:0 12px;font-size:12px;color:#8b949e">Page 1 of 1</span>
      <button class="secondary" id="next-page" onclick="nextPage()" disabled>Next &raquo;</button>
    </div>
  </section>

  <section class="panel" id="detail" style="grid-column: 1 / -1;">
    <h2 id="detail-title">Select a run</h2>
    <div id="detail-body" class="empty">No run selected.</div>
  </section>
  <div id="login-modal" class="modal" style="display:none">
    <div class="modal-body">
      <h2>Sign in to OrchX</h2>
      <p id="login-help" style="color:#8b949e;font-size:13px"></p>
      <label id="login-user-label" for="login-user" style="font-size:12px;color:#8b949e">Username</label>
      <input id="login-user" type="text" autocomplete="username" style="display:block;width:100%;margin:4px 0 10px;padding:6px;background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:3px">
      <label id="login-pass-label" for="login-pass" style="font-size:12px;color:#8b949e">Password</label>
      <input id="login-pass" type="password" autocomplete="current-password" style="display:block;width:100%;margin:4px 0 10px;padding:6px;background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:3px">
      <label id="login-token-label" for="login-token" style="font-size:12px;color:#8b949e;display:none">API token</label>
      <input id="login-token" type="password" autocomplete="off" style="display:none;width:100%;margin:4px 0 10px;padding:6px;background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:3px">
      <div id="login-error" style="color:#f85149;font-size:12px;min-height:18px;margin-bottom:6px"></div>
      <div class="row" style="justify-content:flex-end">
        <button id="login-submit">Sign in</button>
      </div>
    </div>
  </div>
</main>

<style>
.modal {
  position: fixed; top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(0,0,0,0.6);
  display: flex; align-items: center; justify-content: center;
  z-index: 1000;
}
.modal-body {
  background: #161b22;
  border: 1px solid #30363d;
  border-radius: 6px;
  padding: 24px;
  width: 360px;
  max-width: 90vw;
}
.modal-body h2 { margin-top: 0; }
</style>

<script>
const $ = (id) => document.getElementById(id);
let selectedRunId = null;
let ws = null;

// ---- auth state ----
// We store the credential in localStorage keyed by
// the current origin so a refresh keeps the user signed
// in. The credential is sent on every fetch() via
// applyAuth() below; it is never sent to a third party
// because /api/auth is a same-origin request.
const STORAGE_KEY = "orchx.cred";
let authMode = "none";

function getCred() {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || "null"); }
  catch { return null; }
}
function setCred(c) {
  if (c) localStorage.setItem(STORAGE_KEY, JSON.stringify(c));
  else localStorage.removeItem(STORAGE_KEY);
}
function applyAuth(headers) {
  const c = getCred();
  if (!c) return headers;
  if (c.kind === "basic") {
    headers["Authorization"] = "Basic " + btoa(c.user + ":" + c.pass);
  } else if (c.kind === "api_key") {
    headers["Authorization"] = "Bearer " + c.token;
  }
  return headers;
}
// Wrap fetch so every API call (including the live
// stream's URL params) carries the credential. The
// WebSocket code path uses buildWsUrl() below.
const _origFetch = window.fetch;
window.fetch = function (url, init) {
  init = init || {};
  init.headers = init.headers || {};
  applyAuth(init.headers);
  return _origFetch.call(this, url, init);
};

function buildWsUrl(runId) {
  const c = getCred();
  let url = `/api/runs/${runId}/stream`;
  if (c && c.kind === "api_key") {
    url += "?token=" + encodeURIComponent(c.token);
  } else if (c && c.kind === "basic") {
    // WebSocket can't carry Authorization, so base64 the
    // user:pass into ?basic=... The server reads and
    // validates it the same way it would for an HTTP
    // Basic header.
    url += "?basic=" + encodeURIComponent(btoa(c.user + ":" + c.pass));
  }
  return url;
}

function showLoginModal(mode) {
  const m = $("login-modal");
  m.style.display = "flex";
  $("login-error").textContent = "";
  if (mode === "basic") {
    $("login-user-label").style.display = "";
    $("login-user").style.display = "block";
    $("login-pass-label").style.display = "";
    $("login-pass").style.display = "block";
    $("login-token-label").style.display = "none";
    $("login-token").style.display = "none";
    $("login-help").textContent =
      "Enter the username and password configured via " +
      "ORCHX_AUTH_BASIC_USER and ORCHX_AUTH_BASIC_PASSWORD.";
  } else {
    $("login-user-label").style.display = "none";
    $("login-user").style.display = "none";
    $("login-pass-label").style.display = "none";
    $("login-pass").style.display = "none";
    $("login-token-label").style.display = "";
    $("login-token").style.display = "block";
    $("login-help").textContent =
      "Enter the API token configured via ORCHX_AUTH_API_KEY.";
  }
}
function hideLoginModal() {
  $("login-modal").style.display = "none";
}

async function submitLogin() {
  const status = await (await fetch("/api/auth")).json();
  const c = status.mode === "basic"
    ? { kind: "basic", user: $("login-user").value, pass: $("login-pass").value }
    : { kind: "api_key", token: $("login-token").value };
  setCred(c);
  // Verify the credential works. If the server returns
  // 401, surface the error and stay on the modal.
  const probe = await fetch("/api/runs?limit=1");
  if (probe.status === 401) {
    $("login-error").textContent =
      "Sign-in failed: the server rejected the credential.";
    setCred(null);
    return;
  }
  hideLoginModal();
  // Surface "Sign out" once a credential is stored. The
  // link is hidden in mode=none and shown whenever a
  // credential exists.
  if (authMode !== "none") {
    $("signout").style.display = "";
  }
  await refreshRuns();
}

async function logout() {
  setCred(null);
  location.reload();
}

async function init() {
  // Probe /api/auth. If the response says credentials are
  // required but the user has none in localStorage, show
  // the login modal. We do this BEFORE the first
  // refreshRuns() so the dashboard never flashes a 401
  // to the operator.
  const status = await (await fetch("/api/auth")).json();
  authMode = status.mode;
  if (status.requires_credentials && !getCred()) {
    showLoginModal(status.mode);
  }
  // Wire the login-submit button. We do this here so the
  // element is in the DOM by the time we attach.
  $("login-submit").onclick = submitLogin;
  // Allow Enter in the password field to submit.
  ["login-user", "login-pass", "login-token"].forEach((id) => {
    $(id).addEventListener("keydown", (e) => {
      if (e.key === "Enter") submitLogin();
    });
  });
  await loadDescriptorOptions();
  await refreshRuns();
  setInterval(refreshRuns, 2000);
  // The "Sign out" link is hidden when the server is in
  // mode=none (no credentials to clear) or when the user
  // isn't signed in. It surfaces the moment a credential
  // is stored in localStorage.
  if (authMode !== "none" && getCred()) {
    $("signout").style.display = "";
  }
}

async function loadDescriptorOptions() {
  // The orchx CLI bundles a couple of sample descriptors; we list the
  // local descriptors/ directory so the user can pick without
  // typing paths. (Server could expose /api/descriptors; the MVP
  // hardcodes the local list.)
  const samples = [
    "descriptors/sample_webapp_erp.yaml",
    "descriptors/sample_oauth_service.yaml",
    "descriptors/sample_containerized_saas.yaml",
    "descriptors/sample_hr_service.yaml",
    "descriptors/sample_settle_eod.yaml",
  ];
  const sel = $("descriptor");
  sel.innerHTML = "";
  for (const path of samples) {
    const opt = document.createElement("option");
    opt.value = path;
    opt.textContent = path;
    sel.appendChild(opt);
  }
  // Custom path input: just include a "custom" option
  const custom = document.createElement("option");
  custom.value = "__custom__";
  custom.textContent = "(custom path...)";
  sel.appendChild(custom);
  sel.addEventListener("change", () => {
    if (sel.value === "__custom__") {
      const p = prompt("Path to descriptor (absolute or relative to project root):");
      if (p) {
        const o = document.createElement("option");
        o.value = p; o.textContent = p; o.selected = true;
        sel.insertBefore(o, custom);
      }
    }
  });
}

// Pagination + filter state.
let currentPage = 0;
const PAGE_SIZE = 25;
let currentStateFilter = "";

async function refreshRuns() {
  let data = { runs: [], total: 0, limit: PAGE_SIZE, offset: 0 };
  try {
    const params = new URLSearchParams();
    params.set("limit", String(PAGE_SIZE));
    params.set("offset", String(currentPage * PAGE_SIZE));
    if (currentStateFilter) params.set("state_filter", currentStateFilter);
    const r = await fetch("/api/runs?" + params.toString());
    data = await r.json();
  } catch (e) {
    $("runs").innerHTML = '<li class="empty">API unreachable.</li>';
    return;
  }
  const ul = $("runs");
  const prev = selectedRunId;
  ul.innerHTML = "";
  const runs = data.runs || [];
  if (!runs.length) {
    ul.innerHTML = '<li class="empty">No runs yet — kick one off above.</li>';
  } else {
    for (const r of runs) {
      const li = document.createElement("li");
      li.dataset.id = r.id;
      if (r.id === prev) li.classList.add("selected");
      li.onclick = () => selectRun(r.id);
      const target = r.target || "";
      const truncated = r.id.length > 12 ? r.id.slice(0, 8) + "…" : r.id;
      li.innerHTML = `
        <div class="row">
          <div>
            <div><code>${truncated}</code> <span class="badge ${r.state}">${r.state}</span></div>
            <div class="id">${escapeHtml(target)}</div>
          </div>
          <button class="secondary" onclick="event.stopPropagation(); selectRun('${r.id}')">view</button>
        </div>
      `;
      ul.appendChild(li);
    }
  }
  // Pagination chrome.
  const total = data.total || 0;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const page = currentPage + 1;
  $("page-info").textContent = `Page ${page} of ${pageCount} · ${total} total`;
  $("prev-page").disabled = currentPage === 0;
  $("next-page").disabled = currentPage >= pageCount - 1;
  $("runs-summary").textContent = currentStateFilter
    ? `filtered: ${currentStateFilter}`
    : "";
}

function onFilterChange() {
  currentStateFilter = $("state-filter").value;
  currentPage = 0;
  refreshRuns();
}

function prevPage() {
  if (currentPage > 0) {
    currentPage--;
    refreshRuns();
  }
}

function nextPage() {
  currentPage++;
  refreshRuns();
}

async function submitRun() {
  const descriptor = $("descriptor").value;
  const target = $("target").value.trim();
  if (!descriptor || descriptor === "__custom__") return alert("Pick a descriptor.");
  if (!target) return alert("Target URI required.");
  const btn = $("submit");
  btn.disabled = true; btn.textContent = "Submitting…";
  try {
    const r = await fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ descriptor, target }),
    });
    const body = await r.json();
    if (!r.ok) throw new Error(body.detail || "submit failed");
    await refreshRuns();
    selectRun(body.id);
  } catch (e) {
    alert("Failed: " + e.message);
  } finally {
    btn.disabled = false; btn.textContent = "Deploy";
  }
}

async function selectRun(runId) {
  selectedRunId = runId;
  // Highlight the list item.
  for (const li of document.querySelectorAll(".runs-list li")) {
    li.classList.toggle("selected", li.dataset.id === runId);
  }
  $("detail-title").textContent = "Run " + runId;
  $("detail-body").innerHTML = '<div class="empty">Loading…</div>';
  // Fetch the run detail (state + events).
  let data;
  try {
    const r = await fetch("/api/runs/" + runId);
    data = await r.json();
  } catch (e) {
    $("detail-body").innerHTML = '<div class="empty">Failed to load.</div>';
    return;
  }
  renderDetail(data);
  // Open a WebSocket for live updates.
  if (ws) { try { ws.close(); } catch(e){} }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${location.host}${buildWsUrl(runId)}`);
  ws.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    // De-dup by seq: server replays history then streams live; the
    // events we already got from GET /api/runs/{id} would otherwise
    // be re-appended here and the timeline would double.
    if (typeof ev.seq === "number" && data.events.some(x => x.seq === ev.seq)) {
      return;
    }
    data.events.push(ev);
    // Update state badge from the latest event.
    if (ev.status === "ok" || ev.status === "failed" || ev.status === "aborted") {
      data.state = ev.status;
    }
    renderDetail(data);
  };
  ws.onclose = () => { ws = null; };
  // renderDetail() also paints the cancel button if the run is still
  // in flight, so we don't need to do it here.
}

function renderDetail(data) {
  const body = $("detail-body");
  // Build a step-id -> "uses secret" map from the plan. The
  // dashboard surfaces a small 🔐 indicator on events whose
  // step_id touches the vault. The plan itself never contains
  // resolved secret values — the indicator is computed from the
  // step's source descriptor on the server, not from the live
  // transport.
  const secretMap = {};
  for (const node of (data.plan || [])) {
    if (node.uses_secret) secretMap[node.id] = true;
  }
  // Re-render the body each time — small N, fine.
  const header = `
    <div style="margin-bottom:12px">
      <span class="badge ${data.state}">${data.state}</span>
      <code>${escapeHtml(data.target || "")}</code>
      <span style="color:#8b949e;font-size:12px;margin-left:8px">
        exit=${data.exit_code === null ? "-" : data.exit_code}
      </span>
    </div>
  `;
  const events = (data.events || []).map(ev => {
    const indicator = secretMap[ev.step_id]
      ? ' <span class="lock" title="this step uses a secret">\U0001f512</span>'
      : '';
    return [
      '<div class="event status-' + ev.status + '">',
      '<span class="status">' + ev.status + '</span>',
      '<span class="step">' + (ev.step_id || '-') + indicator + '</span>',
      '<span class="host">' + (ev.host || '') + '</span>',
      '<span class="msg">' + escapeHtml(ev.message || '') + '</span>',
      '</div>',
    ].join('');
  }).join('');
  body.innerHTML = header + '<div class="events">' + events + "</div>";
  // Re-append cancel button if still in flight.
  if (data.state === "pending" || data.state === "running") {
    const row = document.createElement("div");
    row.className = "row";
    const btn = document.createElement("button");
    btn.textContent = "Cancel run";
    btn.className = "danger";
    btn.onclick = () => cancelRun(selectedRunId);
    row.appendChild(btn);
    body.appendChild(row);
  }
}

async function cancelRun(runId) {
  if (!confirm("Cancel this run?")) return;
  try {
    await fetch("/api/runs/" + runId + "/cancel", { method: "POST" });
    setTimeout(() => selectRun(runId), 200);
  } catch (e) {
    alert("Cancel failed: " + e.message);
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[ch]));
}

init();
</script>
</body></html>
"""


# Module-load-time initialization: walks the HTML and
# substitutes ``{token}`` placeholders. This runs once,
# at import time, so any operator who reloads the
# orchx.web module sees a fresh dashboard rendered
# with the values that are constants in this process.
