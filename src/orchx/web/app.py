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
    async def list_runs() -> list[dict[str, object]]:
        state: AppState = app.state.orchx
        runs = await state.store.list_runs()
        return [r.to_dict() for r in runs]

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
    await state.store.update_run(
        run_id,
        plan_json=json.dumps(
            [
                {
                    "id": sid,
                    "type": next(s.type.value for s in parsed.steps if s.id == sid),
                    "needs": next(s.needs for s in parsed.steps if s.id == sid),
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
    await state.store.update_run(
        run_id,
        state=final_state,
        finished_at=time.time(),
        exit_code=report.exit_code,
    )
    await _teardown()


_INDEX_HTML = """
<!doctype html>
<html><head><meta charset="utf-8"><title>OrchX</title>
<style>
body {
  font: 14px/1.5 -apple-system, sans-serif;
  max-width: 920px; margin: 2em auto; padding: 0 1em; color: #222;
}
h1 { margin: 0 0 .2em; }
pre { background: #f5f5f7; padding: .8em; border-radius: 4px; overflow-x: auto; }
a { color: #0366d6; }
code { background: #f5f5f7; padding: 1px 4px; border-radius: 3px; }
</style></head>
<body>
<h1>OrchX control plane</h1>
<p>API: <code>GET /api/runs</code>, <code>POST /api/runs</code>,
<code>GET /api/runs/{id}</code>,
<code>WS /api/runs/{id}/stream</code>.</p>
<p>Health: <a href="/healthz">/healthz</a> &middot;
Runs: <a href="/api/runs">/api/runs</a></p>
</body></html>
"""


# Module-level app for `uvicorn orchx.web.app:app`
app = _make_app()
