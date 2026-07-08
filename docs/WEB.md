# Web Control Plane (v1)

The web layer is an opt-in HTTP + WebSocket front-end on top of the
engine and a SQLite-backed run history. It is **not** required to use
OrchX — the CLI (`orchx plan`, `orchx deploy`) is the primary
surface. The control plane exists to let multiple operators watch
deploys in real time, replay past runs, and integrate with external
dashboards.

## Install

The web layer is in the `[web]` extra so the base install stays
small (no FastAPI / uvicorn / aiosqlite on the wire):

```bash
uv sync --extra web
```

## Run

```bash
uvicorn orchx.web.app:app --host 0.0.0.0 --port 8765
```

Open <http://localhost:8765/> for the (very small) HTML test page.

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET`  | `/healthz` | Liveness probe. |
| `GET`  | `/api/runs` | List recent runs (newest first, cap 100). |
| `POST` | `/api/runs` | Kick off a new run. Body: `{descriptor, target}`. |
| `GET`  | `/api/runs/{id}` | Run detail + full event list. |
| `GET`  | `/api/runs/{id}/events` | Event list only. |
| `WS`   | `/api/runs/{id}/stream` | Live event stream: replays history, then tails. |

## How it works

1. The user POSTs `{descriptor, target}`.
2. The handler creates a `RunRecord` in SQLite and schedules the
   engine run in a background `asyncio.Task`.
3. The engine emits events; the `Executor._emit` shim schedules the
   async callback as a `create_task` so the engine never blocks on
   persistence.
4. Each event is appended to the `events` table and pushed into a
   per-run `asyncio.Queue`.
5. WebSocket subscribers see the historical events first, then the
   live tail.

## Storage

`state/local.sqlite` is the default location. The schema is two
tables (`runs` and `events`) and is created on first start. The path
is configurable per-app instance — pass `db_path` to
`orchx.web.app._make_app`.

## Authentication

None in v1. The control plane assumes it runs on a trusted network.
Production deployments should put it behind an authenticating proxy
(Caddy, nginx, Cloudflare Access, etc.).

## Future work

- Per-run log streaming (currently only step events are persisted).
- Pause / resume / cancel from the HTTP API.
- Multi-tenant namespace separation.
- Prometheus / OpenTelemetry export.
- A real frontend (the index page is a placeholder).
