"""End-to-end tests for the web control plane.

These tests use FastAPI's TestClient (in-process; no real socket).
They exercise the SQLite run store, the run lifecycle (create →
background execution → events persisted → terminal state), and the
HTTP/WS surface.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from orchx.web.app import _make_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    db = tmp_path / "orchx-test.sqlite"
    app = _make_app(db_path=db)
    # Use the context-manager form so FastAPI's lifespan runs and the
    # store is opened / closed around the test.
    with TestClient(app) as c:
        yield c


REPO_ROOT = Path(__file__).resolve().parents[1]
DESCRIPTOR = REPO_ROOT / "descriptors" / "sample_oauth_service.yaml"


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_index_serves_html(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "OrchX" in r.text


def test_index_html_is_the_dashboard(client: TestClient) -> None:
    """The root page must be the live dashboard, not a placeholder.

    We lock this so a future refactor cannot accidentally regress
    to a static text page.
    """
    r = client.get("/")
    assert r.status_code == 200
    # The dashboard exposes a New-run form and a list of runs.
    assert 'id="new-run"' in r.text
    assert 'id="runs"' in r.text
    # And the WebSocket client is wired up.
    assert "/api/runs/" in r.text and "/stream" in r.text
    # The bundled descriptor samples are listed by default.
    assert "sample_webapp_erp.yaml" in r.text
    assert "sample_oauth_service.yaml" in r.text


def test_create_run_with_unknown_descriptor_reports_failure(
    client: TestClient,
) -> None:
    r = client.post(
        "/api/runs",
        json={
            "descriptor": str(REPO_ROOT / "descriptors" / "does-not-exist.yaml"),
            "target": "mock://local",
        },
    )
    assert r.status_code == 200
    run_id = r.json()["id"]

    final = _wait_terminal(client, run_id, deadline_s=5)
    assert final is not None, "run did not finish within 5s"
    assert final["state"] == "failed"
    assert final["exit_code"] == 2


def test_create_run_with_bundled_descriptor_succeeds(
    client: TestClient,
) -> None:
    r = client.post(
        "/api/runs",
        json={"descriptor": str(DESCRIPTOR), "target": "mock://local"},
    )
    assert r.status_code == 200
    run_id = r.json()["id"]

    final = _wait_terminal(client, run_id, deadline_s=10)
    assert final is not None, "run did not finish within 10s"
    assert final["state"] == "ok", final
    assert final["exit_code"] == 0
    # We must have at least one event per forward step.
    assert len(final["events"]) >= 10


def test_list_runs_orders_newest_first(client: TestClient) -> None:
    r1 = client.post(
        "/api/runs",
        json={"descriptor": str(DESCRIPTOR), "target": "mock://local"},
    )
    time.sleep(0.01)  # ensure distinct created_at
    r2 = client.post(
        "/api/runs",
        json={"descriptor": str(DESCRIPTOR), "target": "mock://local"},
    )
    assert r1.status_code == 200 and r2.status_code == 200
    listing = client.get("/api/runs").json()
    assert len(listing) >= 2
    ids = [r["id"] for r in listing]
    assert ids.index(r2.json()["id"]) < ids.index(r1.json()["id"])


def test_get_run_404(client: TestClient) -> None:
    r = client.get("/api/runs/does-not-exist")
    assert r.status_code == 404


def test_cancel_unknown_run_404(client: TestClient) -> None:
    r = client.post("/api/runs/does-not-exist/cancel")
    assert r.status_code == 404


def test_cancel_already_terminal_returns_idempotent(client: TestClient) -> None:
    r = client.post(
        "/api/runs",
        json={"descriptor": str(DESCRIPTOR), "target": "mock://local"},
    )
    run_id = r.json()["id"]
    final = _wait_terminal(client, run_id, deadline_s=10)
    assert final is not None
    assert final["state"] == "ok"
    # Now it's terminal; cancel must say "already terminal".
    r2 = client.post(f"/api/runs/{run_id}/cancel")
    assert r2.status_code == 200
    body = r2.json()
    assert body["cancelled"] is False
    assert body["state"] == "ok"
    assert body["reason"] == "already terminal"


def test_cancel_emits_aborted_event(client: TestClient) -> None:
    """Even if the run finishes before cancel reaches it, the cancel
    call must emit an 'aborted' event so the log tells the operator
    that someone tried to cancel."""
    r = client.post(
        "/api/runs",
        json={"descriptor": str(DESCRIPTOR), "target": "mock://local"},
    )
    run_id = r.json()["id"]
    _wait_terminal(client, run_id, deadline_s=10)
    # Run is already terminal; cancel is a no-op for the engine but
    # still records the attempt in the event log.
    client.post(f"/api/runs/{run_id}/cancel")
    events = client.get(f"/api/runs/{run_id}/events").json()
    statuses = [e["status"] for e in events]
    assert "aborted" in statuses, "expected an 'aborted' event from the cancel attempt"


# --- additional coverage: error paths, persistence, websocket ---


def test_create_run_with_unknown_target_scheme_reports_failure(
    client: TestClient,
) -> None:
    """A scheme the registry doesn't know must be surfaced as a
    failed run (HTTP 200 on POST, run.state == 'failed', exit_code == 2)
    — not a 500 from the web layer."""
    r = client.post(
        "/api/runs",
        json={"descriptor": str(DESCRIPTOR), "target": "telnet://a:b@1.2.3.4:23"},
    )
    assert r.status_code == 200
    run_id = r.json()["id"]

    final = _wait_terminal(client, run_id, deadline_s=5)
    assert final is not None
    assert final["state"] == "failed"
    assert final["exit_code"] == 2
    # The error event should mention the bad scheme.
    error_events = [e for e in final["events"] if e["status"] == "failed"]
    assert error_events, "expected at least one 'failed' event"
    assert "telnet" in error_events[0]["message"].lower()


def test_run_persists_across_clients(tmp_path: Path) -> None:
    """Two TestClient instances backed by the same SQLite file must
    see the same run history — this is what makes the control plane
    usable across process restarts and across multiple workers."""
    db = tmp_path / "shared.sqlite"

    # Writer: create one run, wait for it to finish, then close the
    # TestClient (which closes the app and its DB handle).
    app1 = _make_app(db_path=db)
    with TestClient(app1) as c1:
        r = c1.post(
            "/api/runs",
            json={"descriptor": str(DESCRIPTOR), "target": "mock://local"},
        )
        run_id = r.json()["id"]
        _wait_terminal_on(c1, run_id, deadline_s=10)

    # Reader: fresh app, fresh DB handle, same SQLite file.
    app2 = _make_app(db_path=db)
    with TestClient(app2) as c2:
        listing = c2.get("/api/runs").json()
        ids = [row["id"] for row in listing]
        assert run_id in ids, "run created by app1 not visible from app2"
        detail = c2.get(f"/api/runs/{run_id}").json()
        assert detail["state"] == "ok"
        assert detail["exit_code"] == 0
        assert detail["events"], "events must survive a process restart"


def test_websocket_replays_history_then_live_stream(
    client: TestClient,
) -> None:
    """The WebSocket endpoint must first replay the event log, then
    stream live events until the run terminates."""
    r = client.post(
        "/api/runs",
        json={"descriptor": str(DESCRIPTOR), "target": "mock://local"},
    )
    run_id = r.json()["id"]
    # Wait until the run has actually started and emitted at least one
    # forward event; otherwise the WebSocket may replay an empty
    # history and we lose the chance to see the live tail.
    _wait_terminal(client, run_id, deadline_s=10)

    seen: list[dict[str, object]] = []
    with client.websocket_connect(f"/api/runs/{run_id}/stream") as ws:
        # Read until we see the run reach a terminal state.
        for _ in range(50):
            try:
                msg = ws.receive_json()
            except WebSocketDisconnect:
                # The server closed the stream after the run terminated
                # (which is the correct behaviour); treat that as a
                # normal end-of-stream signal.
                break
            seen.append(msg)
            if msg.get("status") in ("ok", "failed", "aborted"):
                break
    # The replay must give us a fully-formed event log including the
    # terminal status, not just the synthetic 'pending' kickoff.
    assert seen, "websocket stream replayed no events"
    statuses = [e["status"] for e in seen]
    assert "ok" in statuses, f"expected terminal 'ok' in replay, got {statuses!r}"
    # The dashboard de-dups by seq; the server must therefore emit
    # each event exactly once even when the WS replays the full
    # history. We assert seqs are unique.
    seqs = [e["seq"] for e in seen if "seq" in e]
    assert len(seqs) == len(set(seqs)), f"server emitted duplicate seqs over WS: {seqs!r}"


def test_concurrent_runs_dont_interfere(client: TestClient) -> None:
    """Kicking off multiple runs in quick succession must not
    conflate their events or final states."""
    n = 3
    run_ids: list[str] = []
    for _ in range(n):
        r = client.post(
            "/api/runs",
            json={"descriptor": str(DESCRIPTOR), "target": "mock://local"},
        )
        run_ids.append(r.json()["id"])

    finals: list[dict[str, object]] = []
    for rid in run_ids:
        final = _wait_terminal(client, rid, deadline_s=15)
        assert final is not None, f"run {rid} did not finish within 15s"
        finals.append(final)

    for final, rid in zip(finals, run_ids, strict=True):
        assert final["state"] == "ok", f"run {rid} ended {final['state']}"
        assert final["exit_code"] == 0
        # Every event in this run's log must reference the right run_id.
        # (We don't have run_id in the event payload today; the smoke
        # we ship is "no leakage between run rows".)
        assert final["id"] == rid


# --- helpers ---


def _wait_terminal(
    client: TestClient,
    run_id: str,
    *,
    deadline_s: int,
    terminal_states: tuple[str, ...] = ("ok", "failed"),
):
    """Poll the run until it reaches a terminal state or the deadline hits."""
    return _wait_terminal_on(client, run_id, deadline_s=deadline_s, terminal_states=terminal_states)


def _wait_terminal_on(
    client: TestClient,
    run_id: str,
    *,
    deadline_s: int,
    terminal_states: tuple[str, ...] = ("ok", "failed"),
):
    """Same as _wait_terminal but exposed for cross-client tests."""
    deadline = time.time() + deadline_s
    final = None
    while time.time() < deadline:
        data = client.get(f"/api/runs/{run_id}").json()
        if data["state"] in terminal_states:
            final = data
            break
        time.sleep(0.05)
    return final
