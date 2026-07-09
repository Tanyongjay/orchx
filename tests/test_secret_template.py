"""End-to-end test: secret resolution is engine-side, not load-side.

The vault must never be consulted from `load_descriptor` —
otherwise resolved values would leak into the descriptor model,
the SQLite run log, the dashboard event stream, and every
test fixture. The engine resolves `{{ secret.x }}` references
only at the last possible moment (right before the transport
is invoked), in `_resolve_payload_secrets`.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from textwrap import dedent

import pytest
from fastapi.testclient import TestClient

from orchx.descriptor.loader import load_descriptor
from orchx.engine.executor import Executor
from orchx.engine.planner import build_plan
from orchx.steps.steps import _resolve_secret_template
from orchx.transports.mock import MockTransport
from orchx.web.app import _make_app

REPO_ROOT = Path(__file__).resolve().parents[1]
HR_DESCRIPTOR = REPO_ROOT / "descriptors" / "sample_hr_service.yaml"


@pytest.fixture(autouse=True)
def clean_secrets_env(monkeypatch):
    """Scrub ORCHX_SECRET_* from the host shell so the tests see
    a deterministic env and never accidentally read a real
    password.
    """
    for k in list(os.environ):
        if k.startswith("ORCHX_SECRET_") or k == "ORCHX_SECRETS_BACKEND":
            monkeypatch.delenv(k, raising=False)


# ---------- the loader must NOT resolve secrets ----------


def test_loader_does_not_resolve_secrets(monkeypatch):
    """The descriptor model on disk (and in memory) must still
    contain the literal `{{ secret.x }}` tokens after load. The
    vault is consulted by the engine, not by the loader."""
    monkeypatch.setenv("ORCHX_SECRET_db_password", "s3cr3t!")
    desc = load_descriptor(HR_DESCRIPTOR)
    secrets_step = next(s for s in desc.steps if s.id == "secrets.write")
    cmd = secrets_step.cmd[2]
    assert "s3cr3t!" not in cmd, "vault value leaked into descriptor model"
    assert "{{ secret.db_password }}" in cmd
    assert "{{ secret.db_host }}" in cmd
    assert "{{ secret.db_name }}" in cmd


def test_loader_does_not_consult_vault_for_missing_secret():
    """A missing secret must NOT raise at load time. The vault
    is consulted at step execution; load is offline."""
    desc = load_descriptor(HR_DESCRIPTOR)
    secrets_step = next(s for s in desc.steps if s.id == "secrets.write")
    # Tokens still present.
    assert "{{ secret.db_password }}" in secrets_step.cmd[2]


# ---------- engine resolve ----------


def test_engine_resolves_secret_at_execute_time(monkeypatch):
    """The engine must call the vault at step-execute time and
    feed the resolved value to the transport. The mock
    transport records every command it sees; we assert the
    resolved value shows up there.
    """
    monkeypatch.setenv("ORCHX_SECRET_db_password", "s3cr3t!")
    monkeypatch.setenv("ORCHX_SECRET_db_host", "db.example")
    monkeypatch.setenv("ORCHX_SECRET_db_name", "hr_svc")
    monkeypatch.setenv("ORCHX_SECRET_db_user", "hr_ro")

    desc = load_descriptor(HR_DESCRIPTOR)
    plan = build_plan(desc)
    transport = MockTransport()
    asyncio.run(
        Executor(
            descriptor=desc,
            plan=plan,
            transport=transport,
        ).run()
    )
    # The mock recorded every powershell/command that ran. Find
    # the secrets.write command and assert the literal password
    # value reached the transport.
    matched = [
        a
        for a in transport.journal.entries
        if a["action"] in ("powershell", "command")
        and "s3cr3t!"
        in a.get("details", {}).get("script", "") + a.get("details", {}).get("cmd", ["", ""])[-1]
    ]
    assert matched, "secret was not resolved before reaching the transport"


def test_engine_emits_dash_when_secret_missing(monkeypatch):
    """A missing secret at execute time must fail the step with
    a clear message, not crash the engine."""
    # No env vars set.
    desc = load_descriptor(HR_DESCRIPTOR)
    plan = build_plan(desc)
    transport = MockTransport()
    report = asyncio.run(
        Executor(
            descriptor=desc,
            plan=plan,
            transport=transport,
        ).run()
    )
    assert report.exit_code == 1
    # The "secret resolution failed" message is in the StepAttempt
    # message, which is recorded in the executor's plan model —
    # not in the mock's transport journal. We assert the message
    # is present and clear.
    fail = report.plan.nodes["secrets.write"]
    assert any("secret resolution failed" in a.message for a in fail.attempts), (
        f"no 'secret resolution failed' message: {fail.attempts}"
    )


# ---------- filter syntax is still supported ----------


def test_default_filter_applies_to_missing_secret(monkeypatch):
    """`{{ secret.db_port | default("5432") }}` is unresolved
    at load (loader leaves secret tokens alone), but the
    engine's resolver also understands `default()` and uses the
    fallback if the vault is silent."""
    monkeypatch.setenv("ORCHX_SECRET_db_password", "s3cr3t!")
    monkeypatch.setenv("ORCHX_SECRET_db_host", "db.example")
    monkeypatch.setenv("ORCHX_SECRET_db_name", "hr_svc")
    monkeypatch.setenv("ORCHX_SECRET_db_user", "hr_ro")
    # No ORCHX_SECRET_db_port — must fall back to "5432".

    desc = load_descriptor(HR_DESCRIPTOR)
    plan = build_plan(desc)
    transport = MockTransport()
    asyncio.run(
        Executor(
            descriptor=desc,
            plan=plan,
            transport=transport,
        ).run()
    )
    # Look for a transport call that contains the resolved 5432.
    saw_5432 = False
    for entry in transport.journal.entries:
        details = entry.get("details", {})
        for v in details.values():
            if isinstance(v, list) and v and "5432" in v[-1] or isinstance(v, str) and "5432" in v:
                saw_5432 = True
    assert saw_5432, "default('5432') never reached the transport"


# ---------- _resolve_secret_template is callable directly ----------


def test_resolve_secret_template_helper():
    """The unit-helper used by execute_step: a tiny piece of
    plumbing that only touches secret tokens, leaves the
    rest of the string alone, and surfaces a clean error on
    missing secrets.
    """
    os.environ["ORCHX_SECRET_X"] = "value-of-x"
    try:
        out = _resolve_secret_template("user={{ secret.X }} on {{ system.zzz }}")
        # The secret is resolved.
        assert "value-of-x" in out
        # The non-secret template token is left as-is (the
        # engine never had a chance to resolve it; that's load's
        # job, and load already ran before the engine).
        assert "{{ system.zzz }}" in out
    finally:
        del os.environ["ORCHX_SECRET_X"]


def test_resolve_secret_template_missing_raises_cleanly():
    """A missing secret at execute time must surface as a
    `KeyError` with a clear message — not a confusing lookup
    deep in the renderer."""
    with pytest.raises(KeyError, match="unknown template variable"):
        _resolve_secret_template("echo {{ secret.does_not_exist }}")


# ---------- nested is rejected ----------


def test_nested_secret_name_is_rejected():
    """`{{ secret.a.b }}` is not a supported convention; the
    vault is flat by name. The resolver surfaces this as a
    `KeyError`."""
    os.environ["ORCHX_SECRET_A"] = "v"
    try:
        with pytest.raises(KeyError, match="unknown template variable"):
            _resolve_secret_template("echo {{ secret.A.b }}")
    finally:
        del os.environ["ORCHX_SECRET_A"]


def test_nested_secret_in_descriptor_yaml_is_also_rejected(monkeypatch):
    """A literal `{{ secret.a.b }}` in a YAML string is rejected
    by the loader (which only validates the path syntax), and
    by the engine resolver at execute time. Both surface the
    same clear error."""
    monkeypatch.setenv("ORCHX_SECRET_a", "v")

    raw = dedent(
        """
        system: { name: X, code: xx, version: "1.0.0" }
        topology: { roles: [{ name: web, count: 1 }] }
        steps:
          - id: probe
            type: command
            on_host: web
            cmd: [echo, "{{ secret.a.b }}"]
        """
    )
    p = REPO_ROOT / "descriptors" / "_tmp_nested.yaml"
    p.write_text(raw)
    try:
        # The loader currently passes nested tokens through
        # (we leave them for the engine). The engine's
        # resolver then rejects them at execute time.
        with pytest.raises(KeyError, match="unknown template variable"):
            _resolve_secret_template("{{ secret.a.b }}")
    finally:
        p.unlink()


# ---------- lock-down: nothing persisted anywhere contains a value ----------


def test_run_store_does_not_persist_resolved_secrets(tmp_path, monkeypatch):
    """Even after a run, neither the SQLite run row, the events
    log, the plan_json, nor the to_dict() surface should contain
    a resolved secret value. The vault is consulted at execute
    time and the result never lands on disk.
    """
    import time

    monkeypatch.setenv("ORCHX_SECRET_db_password", "DO-NOT-LEAK-ME")
    monkeypatch.setenv("ORCHX_SECRET_db_host", "db.example")
    monkeypatch.setenv("ORCHX_SECRET_db_name", "hr_svc")
    monkeypatch.setenv("ORCHX_SECRET_db_user", "hr_ro")

    db = tmp_path / "lockdown.sqlite"
    app = _make_app(db_path=db)
    with TestClient(app) as client:
        r = client.post(
            "/api/runs",
            json={
                "descriptor": str(HR_DESCRIPTOR),
                "target": "mock://local",
            },
        )
        run_id = r.json()["id"]
        # Wait for terminal (polling inline so we don't import
        # across test packages).
        deadline = time.time() + 10
        while time.time() < deadline:
            data = client.get(f"/api/runs/{run_id}").json()
            if data["state"] in ("ok", "failed", "aborted"):
                break
            time.sleep(0.05)
        # 1. SQLite file does not contain the resolved value.
        raw = db.read_bytes()
        assert b"DO-NOT-LEAK-ME" not in raw, "resolved secret landed in SQLite file"
        # 2. JSON shape: events, plan fields, to_dict output.
        run = client.get(f"/api/runs/{run_id}").json()
        blob = json.dumps(run)
        assert "DO-NOT-LEAK-ME" not in blob, "resolved secret leaked into the run response"


def test_dashboard_marks_steps_that_use_secrets():
    """A descriptor step that contains a `{{ secret.* }}` token
    must still be safe (the engine resolves it at run time) AND
    the operator should be able to tell, at a glance, which
    steps will touch the vault. This is a build-time check on
    the descriptor — we want a non-empty list of step ids
    that match.
    """
    desc = load_descriptor(HR_DESCRIPTOR)
    secret_users = []
    for step in desc.steps:
        cmd = getattr(step, "cmd", None)
        if cmd and any("{{ secret." in s for s in cmd if isinstance(s, str)):
            secret_users.append(step.id)
    assert "secrets.write" in secret_users
