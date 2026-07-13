"""Lock-down tests for the 'orchx state' CLI.

These tests run the CLI as a subprocess against a fresh
in-process SQLite file populated via the orchx.web.store
package. We exercise:

  * state list with an empty DB: prints 'no state db at
    <path>' and exits 0.
  * state list with 3 fake runs: prints a 4-column table
    (id, state, started_at, descriptor) and the footer
    pagination line.
  * state get on a known run id: dumps the run row fields.
  * state get on a missing run id: prints 'run X not
    found' and exits non-zero.
  * state cancel on a pending run: marks it aborted.
  * state cancel on a finished run: prints the
    'already in terminal state' message and exits 0
    (cancel-of-finished is a no-op).
  * state purge with --yes against an old run: removes
    it; without --yes, the typer.confirm abort raises
    Abort (we exercise the abort path via PIPE-driven
    input or by passing --yes).

The lock-down for ``state purge`` is the absence of
accidental deletion: we always require --yes (or an
explicit confirm at the prompt), and the default
retention of 30 days is non-destructive for any
operator running on a freshly-deployed cluster.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from orchx.web.store import RunRecord, RunStore

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_fake_runs(db: Path, runs: list[RunRecord]) -> None:
    """Populate a fresh state DB with a list of runs."""

    async def _go() -> None:
        store = RunStore(db)
        await store.init()
        try:
            for r in runs:
                await store.create_run(r)
        finally:
            await store.close()

    asyncio.run(_go())


def _make_run(idx: int, *, state: str, created_at: int) -> RunRecord:
    return RunRecord(
        id=f"r{idx:04x}deadbeef" + "c" * 24,
        descriptor=f"descriptors/sample_{idx}.yaml",
        target="mock://local",
        state=state,
        created_at=created_at,
    )


# ---------- state list ----------


def test_state_list_on_missing_db_exits_zero(
    tmp_path: Path,
) -> None:
    cp = subprocess.run(
        [
            sys.executable,
            "-m",
            "orchx.cli.app",
            "state",
            "list",
            "--db",
            str(tmp_path / "missing.sqlite"),
        ],
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert cp.returncode == 0
    assert "no state db" in cp.stdout


def test_state_list_shows_runs_table(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite"
    runs = [
        _make_run(1, state="ok", created_at=1_700_000_000),
        _make_run(2, state="failed", created_at=1_700_000_100),
        _make_run(3, state="pending", created_at=1_700_000_200),
    ]
    _write_fake_runs(db, runs)
    cp = subprocess.run(
        [
            sys.executable,
            "-m",
            "orchx.cli.app",
            "state",
            "list",
            "--db",
            str(db),
        ],
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert cp.returncode == 0
    # The 4 columns appear in the header.
    for col in ("id", "state", "started_at", "descriptor"):
        assert col in cp.stdout
    # All three run ids appear (truncated to first 12
    # chars per the table render).
    for r in runs:
        assert r.id[:12] in cp.stdout
    # Footer pagination line.
    assert "showing 3 of 3" in cp.stdout


def test_state_list_filter_by_state(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite"
    runs = [
        _make_run(1, state="ok", created_at=1_700_000_000),
        _make_run(2, state="failed", created_at=1_700_000_100),
        _make_run(3, state="ok", created_at=1_700_000_200),
    ]
    _write_fake_runs(db, runs)
    cp = subprocess.run(
        [
            sys.executable,
            "-m",
            "orchx.cli.app",
            "state",
            "list",
            "--db",
            str(db),
            "--state",
            "ok",
        ],
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert cp.returncode == 0
    assert "ok" in cp.stdout
    # Only the two ok runs appear.
    assert cp.stdout.count("ok ") >= 2  # row state + filter label
    # The failed run id should NOT appear.
    assert "r0002" not in cp.stdout


# ---------- state get ----------


def test_state_get_dumps_row(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite"
    runs = [
        _make_run(1, state="ok", created_at=1_700_000_000),
    ]
    _write_fake_runs(db, runs)
    cp = subprocess.run(
        [
            sys.executable,
            "-m",
            "orchx.cli.app",
            "state",
            "get",
            runs[0].id,
            "--db",
            str(db),
        ],
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert cp.returncode == 0
    # Each row field appears.
    assert "state" in cp.stdout
    assert "ok" in cp.stdout
    assert "mock://local" in cp.stdout


def test_state_get_missing_run_exits_nonzero(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite"
    # We need an existing-but-empty db to actually hit
    # the 'run not found' path; otherwise the CLI short-
    # circuits with 'no state db'. Populate with one
    # fake run, then look up a different id.
    _write_fake_runs(
        db,
        [_make_run(1, state="ok", created_at=1_700_000_000)],
    )
    cp = subprocess.run(
        [
            sys.executable,
            "-m",
            "orchx.cli.app",
            "state",
            "get",
            "nonexistent_id",
            "--db",
            str(db),
        ],
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert cp.returncode != 0
    assert "not found" in cp.stdout


# ---------- state cancel ----------


def test_state_cancel_pending_marks_aborted(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite"
    runs = [
        _make_run(1, state="pending", created_at=1_700_000_000),
    ]
    _write_fake_runs(db, runs)
    cp = subprocess.run(
        [
            sys.executable,
            "-m",
            "orchx.cli.app",
            "state",
            "cancel",
            runs[0].id,
            "--db",
            str(db),
        ],
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert cp.returncode == 0
    # The CLI confirmed the cancel.
    assert "marked" in cp.stdout
    # Read back via a follow-up list/get to confirm
    # the state actually changed.
    cp2 = subprocess.run(
        [
            sys.executable,
            "-m",
            "orchx.cli.app",
            "state",
            "get",
            runs[0].id,
            "--db",
            str(db),
        ],
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert "aborted" in cp2.stdout


def test_state_cancel_finished_is_noop(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite"
    runs = [
        _make_run(1, state="ok", created_at=1_700_000_000),
    ]
    _write_fake_runs(db, runs)
    cp = subprocess.run(
        [
            sys.executable,
            "-m",
            "orchx.cli.app",
            "state",
            "cancel",
            runs[0].id,
            "--db",
            str(db),
        ],
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert cp.returncode == 0
    assert "no-op" in cp.stdout


# ---------- state purge ----------


def test_state_purge_with_yes_deletes_old(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite"
    # 30 days ago (in seconds).
    now = int(os.path.getmtime(__file__))
    very_old = now - 60 * 86_400  # 60 days
    fresh = now - 5 * 86_400  # 5 days
    runs = [
        _make_run(1, state="ok", created_at=very_old),
        _make_run(2, state="ok", created_at=fresh),
    ]
    _write_fake_runs(db, runs)
    cp = subprocess.run(
        [
            sys.executable,
            "-m",
            "orchx.cli.app",
            "state",
            "purge",
            "--older-than-days",
            "30",
            "--yes",
            "--db",
            str(db),
        ],
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert cp.returncode == 0
    # The CLI reported 1 purge.
    assert "purged 1" in cp.stdout
    # The fresh run remains; the old one is gone.
    cp2 = subprocess.run(
        [
            sys.executable,
            "-m",
            "orchx.cli.app",
            "state",
            "list",
            "--db",
            str(db),
        ],
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert "showing 1 of 1" in cp2.stdout
    assert runs[0].id[:12] not in cp2.stdout
    assert runs[1].id[:12] in cp2.stdout


def test_state_purge_without_yes_aborts(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite"
    runs = [
        _make_run(1, state="ok", created_at=0),  # epoch 1970 -- definitely old
    ]
    _write_fake_runs(db, runs)
    cp = subprocess.run(
        [
            sys.executable,
            "-m",
            "orchx.cli.app",
            "state",
            "purge",
            "--older-than-days",
            "1",
            "--db",
            str(db),
        ],
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
        # typer.confirm reads from /dev/tty; without it,
        # typer raises Abort. We send EOF via stdin.
        input="",
    )
    # The Abort() exit code from typer is 1.
    assert cp.returncode != 0
    # The run still exists.
    cp2 = subprocess.run(
        [
            sys.executable,
            "-m",
            "orchx.cli.app",
            "state",
            "list",
            "--db",
            str(db),
        ],
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert "showing 1 of 1" in cp2.stdout
