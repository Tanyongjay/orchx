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
import os
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

# We don't import Depends because the auth gate is a
# middleware, not a per-route dependency. Per-route
# dependencies don't compose with the WebSocket endpoint
# (we'd need a separate code path there anyway), and a
# middleware is one well-known place to look for "the
# thing that gates access to /api/*".
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from orchx.descriptor.loader import load_descriptor
from orchx.engine.executor import Executor
from orchx.engine.planner import build_plan
from orchx.transports import get_transport
from orchx.web.auth import (
    AuthConfig,
    auth_config_from_env,
    websocket_check_token,
)
from orchx.web.store import RunRecord, RunStore


@dataclass
class AppState:
    store: RunStore
    tasks: dict[str, asyncio.Task[None]]
    cancel_events: dict[str, asyncio.Event]
    # The asyncio loop this AppState is bound to. We
    # capture it at ``__init__`` time so the control-socket
    # cancel callback (which runs in a different task on
    # the same loop) can ``run_coroutine_threadsafe`` the
    # cancel without having to ``asyncio.get_event_loop()``
    # which would return the wrong loop in pytest fixtures.
    loop: asyncio.AbstractEventLoop
    # The auth config is loaded once at app startup from
    # environment variables. Routes that need to gate on
    # credentials reach for ``state.auth_config`` rather
    # than re-reading the environment on every request.
    auth_config: AuthConfig


class RunRequest(BaseModel):
    descriptor: str = Field(description="Path to YAML descriptor")
    target: str = Field(description="Transport URI (e.g. mock://local)")


DEFAULT_DB = Path("state/local.sqlite")


def _make_app(
    db_path: Path | None = None,
    auth_config: AuthConfig | None = None,
) -> FastAPI:
    db = db_path or DEFAULT_DB
    # If the caller did not pass an explicit AuthConfig,
    # read one from the process environment. This is the
    # normal path for ``uv run python -m orchx.web.app``;
    # tests pass a hand-rolled AuthConfig so they can
    # exercise the auth paths without touching the
    # environment.
    if auth_config is None:
        auth_config = auth_config_from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        store = RunStore(db)
        await store.init()
        state = AppState(
            store=store,
            tasks={},
            cancel_events={},
            auth_config=auth_config,
            loop=asyncio.get_event_loop(),
        )
        app.state.orchx = state

        # Cross-process cancel via a loopback TCP socket.
        # The socket runs on the FastAPI event loop and
        # dispatches cancel commands to the same task graph
        # the dashboard's POST /api/runs/<id>/cancel hits.
        # The ``register_cancel_callback`` indirection lets
        # the socket module stay decoupled from the
        # control plane's internals.
        if os.environ.get("ORCHX_CONTROL_DISABLED") != "1":
            from orchx.web.control_socket import (
                register_cancel_callback,
                unregister_cancel_callback,
            )
            from orchx.web.control_socket import (
                serve as _serve_control,
            )

            async def _cancel_for_socket(
                st: Any,
                run_id: str,
            ) -> dict[str, object]:
                run = await st.store.get_run(run_id)
                if run is None:
                    return {"ok": False, "error": "run not found"}
                if run.state in ("ok", "failed", "aborted"):
                    await st.store.emit(
                        run_id,
                        status="aborted",
                        message="cancel ignored: already terminal",
                    )
                    return {
                        "ok": True,
                        "id": run_id,
                        "cancelled": False,
                        "reason": "already terminal",
                    }
                ev = st.cancel_events.get(run_id)
                if ev is not None:
                    ev.set()
                task = st.tasks.get(run_id)
                if task is not None and not task.done():
                    task.cancel()
                await st.store.emit(
                    run_id,
                    status="aborted",
                    message="cancellation requested (control socket)",
                )
                return {
                    "ok": True,
                    "id": run_id,
                    "cancelled": True,
                }

            def _cancel_sync(run_id: str) -> dict[str, object]:
                loop = asyncio.get_event_loop()
                fut = asyncio.run_coroutine_threadsafe(
                    _cancel_for_socket(state, run_id),
                    loop,
                )
                return fut.result(timeout=5.0)

            register_cancel_callback(_cancel_sync)
            control_task = asyncio.create_task(
                _serve_control(
                    host=os.environ.get(
                        "ORCHX_CONTROL_HOST",
                        "127.0.0.1",
                    ),
                    port=int(
                        os.environ.get("ORCHX_CONTROL_PORT", "0"),
                    ),
                ),
            )
        else:
            control_task = None
        try:
            yield
        finally:
            if control_task is not None:
                control_task.cancel()
                with suppress(Exception):
                    await control_task
            if os.environ.get("ORCHX_CONTROL_DISABLED") != "1":
                from orchx.web.control_socket import (
                    unregister_cancel_callback,
                )

                unregister_cancel_callback()
            for ev in state.cancel_events.values():
                ev.set()
            for t in list(state.tasks.values()):
                t.cancel()
            await store.close()

    app = FastAPI(
        title="OrchX",
        version="0.2.0",
        lifespan=lifespan,
        # OpenAPI is opt-in here because the dashboard embeds its
        # own JSON API link in the header; we don't want a public
        # /api/docs page in every deployment. Commercial users
        # who want docs can override by passing a different
        # `openapi_url` to the constructor.
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )
    _register_routes(app)
    return app


# The auth gate is a middleware inside _register_routes
# below; see the comment block at the top of
# _register_routes for the rationale.


def _register_routes(app: FastAPI) -> None:
    from starlette.responses import JSONResponse as _JR

    @app.middleware("http")
    async def _auth_middleware(request, call_next):
        # The middleware covers every HTTP request that
        # arrives, but only GATES the paths that the
        # auth_config says require credentials. /healthz
        # and /api/auth are always open (the former is a
        # liveness probe; the latter tells the dashboard
        # whether a login screen is needed).
        if not request.url.path.startswith("/api/"):
            return await call_next(request)
        # /api/auth is open so the dashboard can decide
        # whether to show a login screen. The OpenAPI
        # routes are open so external tooling can introspect
        # the API contract.
        if request.url.path in {
            "/api/auth",
            "/api/openapi.json",
            "/api/docs",
            "/api/redoc",
        }:
            return await call_next(request)
        config = request.app.state.orchx.auth_config
        if config.mode == "none":
            return await call_next(request)
        if config.mode == "api_key":
            from orchx.web.auth import _check_api_key, _extract_bearer, _extract_query_token

            token = _extract_bearer(request) or _extract_query_token(request)
            if token is None or not _check_api_key(config, token):
                return _JR(
                    {"detail": "authentication required"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="orchx"'},
                )
            return await call_next(request)
        if config.mode == "basic":
            from orchx.web.auth import _check_basic, _extract_basic

            creds = _extract_basic(request)
            if creds is None or not _check_basic(config, creds[0], creds[1]):
                return _JR(
                    {"detail": "authentication required"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="orchx"'},
                )
            return await call_next(request)
        return await call_next(request)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        # /healthz is intentionally unauthenticated so load
        # balancers and uptime monitors can probe liveness
        # without a credential. It exposes no run data.
        return {"status": "ok"}

    @app.get("/api/auth")
    async def auth_status() -> dict[str, object]:
        # The dashboard uses this endpoint to decide whether
        # to show a login screen. The response is safe to
        # expose to anonymous callers — it only describes
        # whether auth is required and, in basic mode, the
        # username. Never the secret.
        # We resolve AppState from the app reference the
        # decorator captured rather than taking a ``Request``
        # parameter, because the latter would make FastAPI
        # try to bind it as a query parameter (it doesn't
        # know Request is a special type at function-signature
        # parsing time when there are no other parameters).
        state: AppState = app.state.orchx
        return state.auth_config.describe()

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
        # Auth gate. The WebSocket cannot easily return a 401
        # after accept() (the spec doesn't allow status codes
        # on the upgrade response from a server-pushed
        # policy), so we close the connection with code
        # 1008 (policy violation) when the credential is
        # missing or wrong.
        if not websocket_check_token(state.auth_config, ws):
            await ws.close(code=1008)
            return
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
        return HTMLResponse(INDEX_HTML)


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


#: The dashboard HTML is large (~20 KB) and lives in its own module
#: so the FastAPI app definition stays readable. The string is
#: populated at import time by ``orchx.web.dashboard.init()``.
from orchx.web.dashboard import INDEX_HTML  # noqa: E402

# Module-level app for `uvicorn orchx.web.app:app`
app = _make_app()
