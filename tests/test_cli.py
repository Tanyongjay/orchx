"""Tests for the CLI surface: --verbose and --json output."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def clean_secrets_env(monkeypatch):
    """The CLI reads ORCHX_SECRET_* from os.environ; scrub the
    host shell so a developer's real machine can't leak values
    into the assertion path.
    """
    for k in list(os.environ):
        if k.startswith("ORCHX_SECRET_") or k == "ORCHX_SECRETS_BACKEND":
            monkeypatch.delenv(k, raising=False)


def _orchx(*args, env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Invoke the CLI as a subprocess so we exercise the actual
    typer entrypoint, not a unit-test import.
    """
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "orchx.cli.app", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_deploy_json_emits_parseable_run_report() -> None:
    """`orchx deploy --json` emits a single RunReport as JSON
    on stdout. The report must include exit_code, started_at,
    finished_at, topo_order, and a list of node dicts.
    """
    cp = _orchx(
        "deploy",
        "descriptors/sample_webapp_erp.yaml",
        "--target",
        "mock://local",
        "--no-rollback",
        "--json",
    )
    assert cp.returncode == 0, cp.stderr
    data = json.loads(cp.stdout)
    assert data["exit_code"] == 0
    assert data["aborted"] is False
    assert isinstance(data["started_at"], float)
    assert isinstance(data["finished_at"], float)
    assert data["finished_at"] >= data["started_at"]
    assert isinstance(data["topo_order"], list)
    assert len(data["topo_order"]) >= 5
    assert isinstance(data["nodes"], list)
    # Each node carries attempts; the happy descriptor leaves
    # every attempt in the ok or skipped state.
    for n in data["nodes"]:
        assert {"step_id", "status", "depends_on", "attempts"} <= set(n.keys())
        for a in n["attempts"]:
            assert {
                "step_id",
                "attempt",
                "status",
                "host",
                "message",
                "started_at",
                "finished_at",
            } <= set(a.keys())


def test_deploy_json_propagates_exit_code_on_failure() -> None:
    """When the deploy fails, the process must exit with the
    RunReport's exit_code (1) so shell pipelines and CI jobs
    can detect the failure. --json must still emit a parseable
    RunReport in this case.
    """
    cp = _orchx(
        "deploy",
        "descriptors/sample_webapp_erp.yaml",
        "--target",
        "mock://local",
        "--chaos",
        '{"local":[{"action":"package","exit_code":1,"fail_times":99}]}',
        "--json",
    )
    assert cp.returncode == 1
    data = json.loads(cp.stdout)
    assert data["exit_code"] == 1
    # The failed step is package, so we expect a non-empty
    # attempts list on the package node with status=failed.
    failed = [n for n in data["nodes"] if n["status"] == "failed"]
    assert failed, "expected at least one failed node in the JSON report"


def test_deploy_json_does_not_print_rich_to_stdout() -> None:
    """When --json is on, the rich summary must NOT leak to
    stdout — the JSON document is the only thing on stdout so
    it can be piped to jq without corruption.
    """
    cp = _orchx(
        "deploy",
        "descriptors/sample_webapp_erp.yaml",
        "--target",
        "mock://local",
        "--no-rollback",
        "--json",
    )
    # The first non-whitespace character of stdout should be '{'.
    stripped = cp.stdout.lstrip()
    assert stripped.startswith("{"), f"stdout was: {cp.stdout[:200]!r}"
    json.loads(cp.stdout)


def test_deploy_verbose_writes_json_per_event_to_stderr() -> None:
    """`--verbose` keeps the rich UI on stdout and writes one
    JSON line per event to stderr.
    """
    cp = _orchx(
        "deploy",
        "descriptors/sample_webapp_erp.yaml",
        "--target",
        "mock://local",
        "--no-rollback",
        "--verbose",
    )
    assert cp.returncode == 0
    # Each line of stderr is a JSON event.
    lines = [ln for ln in cp.stderr.splitlines() if ln.startswith("{")]
    assert lines, f"no JSON events on stderr, got: {cp.stderr!r}"
    for ln in lines:
        ev = json.loads(ln)
        assert {"step_id", "status", "attempt", "host"} <= set(ev.keys())
    # At least one event per non-reversal step.
    assert len(lines) >= 5
