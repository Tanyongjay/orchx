"""Web control plane: HTTP API + WebSocket + persistent run history.

A *run* is a single execution of a descriptor against a target.
The control plane keeps a process-wide registry of active runs,
persists them to a SQLite store, and streams events over WebSocket
as the engine progresses.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    descriptor    TEXT NOT NULL,
    target        TEXT NOT NULL,
    state         TEXT NOT NULL,
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL,
    exit_code     INTEGER,
    plan_json     TEXT
);
CREATE TABLE IF NOT EXISTS events (
    run_id        TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    step_id       TEXT,
    status        TEXT NOT NULL,
    message       TEXT,
    host          TEXT,
    attempt       INTEGER,
    ts            REAL NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS events_run_idx ON events (run_id, seq);
"""


@dataclass
class RunRecord:
    id: str
    descriptor: str
    target: str
    state: str  # pending | running | ok | failed | aborted
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    plan_json: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "descriptor": self.descriptor,
            "target": self.target,
            "state": self.state,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "events": list(self.events),
        }


class RunStore:
    """SQLite-backed run history with a per-run in-memory event queue.

    Two consumers care about events:
      1. The HTTP WebSocket clients (live tail).
      2. The persistence path (writes to ``events`` table).

    Both subscribe to the same ``asyncio.Queue`` per run.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._queues: dict[str, asyncio.Queue[dict[str, Any] | None]] = {}
        self._seq: dict[str, int] = {}

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.db_path))
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ---- runs ----

    async def create_run(self, run: RunRecord) -> None:
        assert self._conn is not None
        await self._conn.execute(
            "INSERT INTO runs (id, descriptor, target, state, created_at) VALUES (?, ?, ?, ?, ?)",
            (run.id, run.descriptor, run.target, run.state, run.created_at),
        )
        await self._conn.commit()
        self._queues[run.id] = asyncio.Queue()
        self._seq[run.id] = 0

    async def update_run(
        self,
        run_id: str,
        *,
        state: str | None = None,
        started_at: float | None = None,
        finished_at: float | None = None,
        exit_code: int | None = None,
        plan_json: str | None = None,
    ) -> None:
        assert self._conn is not None
        fields: list[str] = []
        args: list[Any] = []
        if state is not None:
            fields.append("state = ?")
            args.append(state)
        if started_at is not None:
            fields.append("started_at = ?")
            args.append(started_at)
        if finished_at is not None:
            fields.append("finished_at = ?")
            args.append(finished_at)
        if exit_code is not None:
            fields.append("exit_code = ?")
            args.append(exit_code)
        if plan_json is not None:
            fields.append("plan_json = ?")
            args.append(plan_json)
        if not fields:
            return
        args.append(run_id)
        await self._conn.execute(
            f"UPDATE runs SET {', '.join(fields)} WHERE id = ?",
            args,
        )
        await self._conn.commit()

    async def list_runs(self) -> list[RunRecord]:
        assert self._conn is not None
        cur = await self._conn.execute(
            "SELECT id, descriptor, target, state, created_at, started_at, "
            "finished_at, exit_code, plan_json "
            "FROM runs ORDER BY created_at DESC LIMIT 100"
        )
        rows = await cur.fetchall()
        return [
            RunRecord(
                id=r[0],
                descriptor=r[1],
                target=r[2],
                state=r[3],
                created_at=r[4],
                started_at=r[5],
                finished_at=r[6],
                exit_code=r[7],
                plan_json=r[8],
            )
            for r in rows
        ]

    async def get_run(self, run_id: str) -> RunRecord | None:
        assert self._conn is not None
        cur = await self._conn.execute(
            "SELECT id, descriptor, target, state, created_at, started_at, "
            "finished_at, exit_code, plan_json "
            "FROM runs WHERE id = ?",
            (run_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return RunRecord(
            id=row[0],
            descriptor=row[1],
            target=row[2],
            state=row[3],
            created_at=row[4],
            started_at=row[5],
            finished_at=row[6],
            exit_code=row[7],
            plan_json=row[8],
        )

    # ---- events ----

    async def emit(
        self,
        run_id: str,
        status: str,
        *,
        step_id: str | None = None,
        message: str | None = None,
        host: str | None = None,
        attempt: int | None = None,
    ) -> None:
        assert self._conn is not None
        seq = self._seq.get(run_id, 0) + 1
        self._seq[run_id] = seq
        event = {
            "seq": seq,
            "ts": time.time(),
            "step_id": step_id,
            "status": status,
            "message": message,
            "host": host,
            "attempt": attempt,
        }
        await self._conn.execute(
            "INSERT INTO events (run_id, seq, step_id, status, message, "
            "host, attempt, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                seq,
                step_id,
                status,
                message,
                host,
                attempt,
                event["ts"],
            ),
        )
        await self._conn.commit()
        queue = self._queues.get(run_id)
        if queue is not None:
            await queue.put(event)

    async def end_run(self, run_id: str) -> None:
        """Mark a run's event stream as closed (sentinel)."""
        queue = self._queues.get(run_id)
        if queue is not None:
            await queue.put(None)

    async def list_events(self, run_id: str) -> list[dict[str, Any]]:
        assert self._conn is not None
        cur = await self._conn.execute(
            "SELECT seq, step_id, status, message, host, attempt, ts "
            "FROM events WHERE run_id = ? ORDER BY seq",
            (run_id,),
        )
        rows = await cur.fetchall()
        return [
            {
                "seq": r[0],
                "step_id": r[1],
                "status": r[2],
                "message": r[3],
                "host": r[4],
                "attempt": r[5],
                "ts": r[6],
            }
            for r in rows
        ]

    async def stream_events(
        self,
        run_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Async iterator over live events; ends when end_run() is called."""
        queue = self._queues.get(run_id)
        if queue is None:
            return
        while True:
            event = await queue.get()
            if event is None:
                return
            yield event
