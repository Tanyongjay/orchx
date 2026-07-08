"""Web control plane: HTTP routes + WebSocket + lifespan.

Endpoints:
  * ``GET  /healthz``                — liveness probe.
  * ``GET  /api/runs``               — list runs.
  * ``POST /api/runs``               — kick off a new run (descriptor + target).
  * ``GET  /api/runs/{id}``          — run detail (state + events).
  * ``GET  /api/runs/{id}/events``   — full event list (JSON).
  * ``WS   /api/runs/{id}/stream``   — live event stream.
  * ``GET  /``                       — minimal HTML test page.
"""

from __future__ import annotations

import asyncio
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


# ---- request / response models ----


class RunRequest(BaseModel):
    descriptor: str = Field(description="Path to YAML descriptor")
    target: str = Field(description="Transport URI (e.g. mock://local)")


# ---- lifespan ----

DEFAULT_DB = Path("state/local.sqlite")


def _make_app(db_path: Path | None = None) -> FastAPI:
    db = db_path or DEFAULT_DB

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        store = RunStore(db)
        await store.init()
        state = AppState(store=store, tasks={})
        app.state.orchx = state
        try:
            yield
        finally:
            # cancel in-flight runs
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
        # schedule the engine run in the background
        task = asyncio.create_task(_run_in_background(state, run.id))
        state.tasks[run.id] = task
        return {"id": run.id, "state": run.state}

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
        # First: send all historical events, then live tail.
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


def _event_emit(state: AppState, run_id: str):
    """Bridge Engine.on_event -> store.emit."""

    async def emit(node, attempt) -> None:
        await state.store.emit(
            run_id,
            status=attempt.status.value,
            step_id=node.step_id,
            message=attempt.message,
            host=attempt.host,
            attempt=attempt.attempt,
        )

    return emit


async def _run_in_background(state: AppState, run_id: str) -> None:
    run = await state.store.get_run(run_id)
    assert run is not None  # just created
    await state.store.update_run(run_id, state="running", started_at=time.time())
    await state.store.emit(run_id, status="pending", message="run started")

    transport = get_transport(run.target)
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
        await state.store.end_run(run_id)
        return

    plan = build_plan(parsed)
    # Persist a compact plan summary.
    import json

    await state.store.update_run(
        run_id,
        plan_json=json.dumps(
            [
                {
                    "id": sid,
                    "type": next(s.type.value for s in parsed.steps if s.id == sid),
                    "needs": parsed.steps and next(s.needs for s in parsed.steps if s.id == sid),
                }
                for sid in plan.topo_order
            ]
        ),
    )

    exec_ = Executor(
        descriptor=parsed,
        plan=plan,
        transport=transport,
        on_event=_event_emit(state, run_id),
    )
    try:
        report = await exec_.run()
    except Exception as e:  # noqa: BLE001  — report all errors
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
        await state.store.end_run(run_id)
        return

    state_terminal = "ok" if report.exit_code == 0 else "failed"
    await state.store.update_run(
        run_id,
        state=state_terminal,
        finished_at=time.time(),
        exit_code=report.exit_code,
    )
    await state.store.end_run(run_id)


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
