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


# --- helpers ---


def _wait_terminal(client: TestClient, run_id: str, *, deadline_s: int):
    """Poll the run until it leaves 'running' state or the deadline hits."""
    deadline = time.time() + deadline_s
    final = None
    while time.time() < deadline:
        data = client.get(f"/api/runs/{run_id}").json()
        if data["state"] in ("ok", "failed"):
            final = data
            break
        time.sleep(0.05)
    return final
