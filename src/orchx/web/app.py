"""Web control plane: HTTP routes + WebSocket + lifespan.

Endpoints:
  * ``GET  /healthz``                — liveness probe.
  * ``GET  /api/runs``               — list runs.
  * ``POST /api/runs``               — kick off a new run (descriptor + target).
  * ``GET  /api/runs/{id}``          — run detail (state + events).
  * ``GET  /api/runs/{id}/events``   — full event list (JSON).
  * ``POST /api/runs/{id}/cancel``   — request cancellation.
  * ``WS   /api/runs/{id}/stream``   — live event stream.
  * ``GET  /``                       — minimal HTML test page.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from orchx.descriptor.loader import load_descriptor
from orchx.engine.executor import Executor
from orchx.engine.planner import build_plan
from orchx.transports import get_transport
from orchx.web.store import RunRecord, RunStore


@dataclass
class AppState:
    store: RunStore
    tasks: dict[str, asyncio.Task[None]]
    cancel_events: dict[str, asyncio.Event]


class RunRequest(BaseModel):
    descriptor: str = Field(description="Path to YAML descriptor")
    target: str = Field(description="Transport URI (e.g. mock://local)")


DEFAULT_DB = Path("state/local.sqlite")


def _make_app(db_path: Path | None = None) -> FastAPI:
    db = db_path or DEFAULT_DB

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        store = RunStore(db)
        await store.init()
        state = AppState(store=store, tasks={}, cancel_events={})
        app.state.orchx = state
        try:
            yield
        finally:
            for ev in state.cancel_events.values():
                ev.set()
            for t in list(state.tasks.values()):
                t.cancel()
            await store.close()

    app = FastAPI(title="OrchX", version="0.1.0a1", lifespan=lifespan)
    _register_routes(app)
    return app


def _register_routes(app: FastAPI) -> None:

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/runs")
    async def list_runs(
        limit: int = 50,
        offset: int = 0,
        state_filter: str | None = None,
    ) -> dict[str, object]:
        # The query parameter is named `state_filter` to avoid
        # collision with the AppState attribute on
        # `app.state.orchx` — FastAPI binds both, but a
        # parameter shadowing the well-known `app.state` name
        # is confusing to read.
        # Normalize inputs at the edge so the response echoes
        # back what we actually used, not what the caller sent.
        # A caller asking for limit=99999 or offset=-100 should
        # see the normalized values in the response.
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        app_state: AppState = app.state.orchx
        runs, total = await app_state.store.list_runs(
            limit=limit,
            offset=offset,
            state=state_filter,
        )
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "state": state_filter,
            "runs": [r.to_dict() for r in runs],
        }

    @app.post("/api/runs")
    async def create_run(req: RunRequest) -> dict[str, object]:
        state: AppState = app.state.orchx
        run = RunRecord(
            id=uuid.uuid4().hex,
            descriptor=req.descriptor,
            target=req.target,
            state="pending",
            created_at=time.time(),
        )
        await state.store.create_run(run)
        cancel = asyncio.Event()
        state.cancel_events[run.id] = cancel
        task = asyncio.create_task(_run_in_background(state, run.id, cancel))
        state.tasks[run.id] = task
        return {"id": run.id, "state": run.state}

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict[str, object]:
        state: AppState = app.state.orchx
        run = await state.store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        if run.state in ("ok", "failed", "aborted"):
            # Still log the cancel attempt so the audit trail captures it.
            await state.store.emit(
                run_id,
                status="aborted",
                message="cancel ignored: already terminal",
            )
            return {
                "id": run_id,
                "state": run.state,
                "cancelled": False,
                "reason": "already terminal",
            }
        ev = state.cancel_events.get(run_id)
        if ev is not None:
            ev.set()
        task = state.tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
        await state.store.emit(
            run_id,
            status="aborted",
            message="cancellation requested",
        )
        return {"id": run_id, "cancelled": True}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, object]:
        state: AppState = app.state.orchx
        run = await state.store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        events = await state.store.list_events(run_id)
        d = run.to_dict()
        d["events"] = events
        # Expose the plan view (id + type + needs + uses_secret)
        # so the dashboard can render the 🔐 indicator next to
        # steps that touch the vault. The plan_json itself
        # never contains resolved secret values — see
        # tests/test_secret_template.py.
        d["plan"] = json.loads(run.plan_json) if run.plan_json else []
        return d

    @app.get("/api/runs/{run_id}/events")
    async def get_events(run_id: str) -> list[dict[str, object]]:
        state: AppState = app.state.orchx
        run = await state.store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return await state.store.list_events(run_id)

    @app.websocket("/api/runs/{run_id}/stream")
    async def stream(ws: WebSocket, run_id: str) -> None:
        state: AppState = app.state.orchx
        await ws.accept()
        try:
            for ev in await state.store.list_events(run_id):
                await ws.send_json(ev)
            async for ev in state.store.stream_events(run_id):
                await ws.send_json(ev)
        except WebSocketDisconnect:
            return
        finally:
            await ws.close()

    @app.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse(_INDEX_HTML)


def _event_emit(state: AppState, run_id: str, cancel: asyncio.Event):
    """Bridge Engine.on_event -> store.emit, with cancellation check.

    The ``on_event`` callback runs synchronously inside the engine;
    we schedule the async DB write as a background task and also
    check the cancellation flag so a long step yields quickly when
    the user cancels.
    """

    async def emit(node, attempt) -> None:
        if cancel.is_set():
            raise asyncio.CancelledError("cancelled by user")
        await state.store.emit(
            run_id,
            status=attempt.status.value,
            step_id=node.step_id,
            message=attempt.message,
            host=attempt.host,
            attempt=attempt.attempt,
        )

    return emit


async def _run_in_background(
    state: AppState,
    run_id: str,
    cancel: asyncio.Event,
) -> None:
    run = await state.store.get_run(run_id)
    assert run is not None  # just created
    await state.store.update_run(
        run_id,
        state="running",
        started_at=time.time(),
    )
    await state.store.emit(run_id, status="pending", message="run started")

    # Tear-down helper: run this in every exit path to keep state clean.
    async def _teardown() -> None:
        state.cancel_events.pop(run_id, None)
        state.tasks.pop(run_id, None)
        await state.store.end_run(run_id)

    try:
        transport = get_transport(run.target)
    except Exception as e:
        await state.store.emit(
            run_id,
            status="failed",
            step_id="<transport>",
            message=str(e),
        )
        await state.store.update_run(
            run_id,
            state="failed",
            finished_at=time.time(),
            exit_code=2,
        )
        await _teardown()
        return

    try:
        parsed = load_descriptor(Path(run.descriptor))
    except Exception as e:
        await state.store.emit(
            run_id,
            status="failed",
            step_id="<load>",
            message=str(e),
        )
        await state.store.update_run(
            run_id,
            state="failed",
            finished_at=time.time(),
            exit_code=2,
        )
        await _teardown()
        return

    plan = build_plan(parsed)
    # Build a per-step `uses_secret` flag so the dashboard can
    # show a small "🔐" indicator next to steps that touch the
    # vault. The flag is derived from a scan of the step's
    # command/script payload for any `{{ secret.* }}` token. We
    # never persist the resolved value; we only persist the
    # boolean and the step id.
    secret_users: dict[str, bool] = {}
    for s in parsed.steps:
        uses = False
        cmd = getattr(s, "cmd", None)
        if cmd and any(isinstance(c, str) and "{{ secret." in c for c in cmd):
            uses = True
        if not uses:
            script = getattr(s, "script", None)
            if isinstance(script, str) and "{{ secret." in script:
                uses = True
        if not uses:
            url = getattr(s, "url", None)
            if isinstance(url, str) and "{{ secret." in url:
                uses = True
        secret_users[s.id] = uses

    await state.store.update_run(
        run_id,
        plan_json=json.dumps(
            [
                {
                    "id": sid,
                    "type": next(s.type.value for s in parsed.steps if s.id == sid),
                    "needs": next(s.needs for s in parsed.steps if s.id == sid),
                    "uses_secret": secret_users.get(sid, False),
                }
                for sid in plan.topo_order
            ]
        ),
    )

    exec_ = Executor(
        descriptor=parsed,
        plan=plan,
        transport=transport,
        on_event=_event_emit(state, run_id, cancel),
        should_cancel=cancel.is_set,
    )
    try:
        report = await exec_.run()
    except asyncio.CancelledError:
        await state.store.update_run(
            run_id,
            state="aborted",
            finished_at=time.time(),
            exit_code=130,
        )
        await _teardown()
        return
    except Exception as e:  # noqa: BLE001
        await state.store.emit(
            run_id,
            status="failed",
            step_id="<engine>",
            message=str(e),
        )
        await state.store.update_run(
            run_id,
            state="failed",
            finished_at=time.time(),
            exit_code=1,
        )
        await _teardown()
        return

    final_state = "aborted" if report.aborted else "ok" if report.exit_code == 0 else "failed"
    # Emit a synthetic state-change event so WebSocket clients learn
    # the final state even if they connected after the run started
    # and missed the earlier state transitions.
    await state.store.emit(
        run_id,
        status=final_state,
        step_id="<run>",
        message=f"run finished: exit_code={report.exit_code}",
    )
    await state.store.update_run(
        run_id,
        state=final_state,
        finished_at=time.time(),
        exit_code=report.exit_code,
    )
    await _teardown()


_INDEX_HTML = """
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
  <span class="tagline" style="margin-left:auto"><a href="/api/runs" style="color:#58a6ff">JSON API</a> · <a href="/healthz" style="color:#58a6ff">healthz</a></span>
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
</main>

<script>
const $ = (id) => document.getElementById(id);
let selectedRunId = null;
let ws = null;

async function init() {
  await loadDescriptorOptions();
  await refreshRuns();
  setInterval(refreshRuns, 2000);
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
  ws = new WebSocket(`${proto}//${location.host}/api/runs/${runId}/stream`);
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


# Module-level app for `uvicorn orchx.web.app:app`
app = _make_app()
