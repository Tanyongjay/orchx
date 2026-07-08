"""Plan executor — runs a Plan against a Transport.

The executor is the only place that:
  * mutates PlanNode statuses,
  * invokes the transport,
  * decides to retry / skip / roll back.

Transport failures and step-level failures are kept separate:
  * ``step succeeded but transport raised`` -> step FAILED, retry
  * ``step succeeded`` and ``transport ok`` -> step OK
  * any failure past `--tolerate` -> reverse running forward steps
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import suppress

from orchx.descriptor.models import Descriptor
from orchx.engine.models import (
    Plan,
    PlanNode,
    RunReport,
    StepAttempt,
    StepStatus,
)
from orchx.steps.steps import build_step_adapter, execute_step, reverse_step
from orchx.transports.base import Transport

ResolveHost = Callable[[str, PlanNode], str]


def default_resolve_host(_role: str | None, _node: PlanNode) -> str:
    """Default host resolver — controls / mock / admin host."""
    return "local"


class Executor:
    """Single-run executor for a Plan.

    The same instance is not thread-safe but runs cooperatively well
    across async tasks. A typical run allocates one Executor per
    invocation; the engine constructs/disposes it via ``run()``.
    """

    def __init__(
        self,
        *,
        descriptor: Descriptor,
        plan: Plan,
        transport: Transport,
        resolve_host: ResolveHost | None = None,
        rollback_on_failure: bool = True,
        on_event: Callable[[PlanNode, StepAttempt], None] | None = None,
    ) -> None:
        self.descriptor = descriptor
        self.plan = plan
        self.transport = transport
        self.resolve_host: ResolveHost = resolve_host or default_resolve_host
        self.rollback_on_failure = rollback_on_failure
        self.on_event = on_event or self._default_event
        self._step_by_id = {s.id: s for s in descriptor.steps}

    def _emit(self, node: PlanNode, attempt: StepAttempt) -> None:
        result = self.on_event(node, attempt)
        if asyncio.iscoroutine(result):
            # Schedule the async callback and let it run in the
            # background. We do not block the engine on persistence
            # so HTTP/WS clients see progress in real time without
            # serialising on the engine.
            asyncio.create_task(result)
        return

    @staticmethod
    def _default_event(node: PlanNode, attempt: StepAttempt) -> None:
        # Quiet by default; CLI replaces this with a Rich live view.
        return None

    async def _run_one(self, node: PlanNode, role: str | None) -> tuple[bool, str]:
        spec = self._step_by_id[node.step_id]
        host = self.resolve_host(role, node)
        adapter = build_step_adapter(spec, host)
        # Establish state to RUNNING.
        node.status = StepStatus.RUNNING
        attempt_idx = len(node.attempts) + 1
        att = StepAttempt(
            step_id=node.step_id,
            attempt=attempt_idx,
            status=StepStatus.RUNNING,
            started_at=time.time(),
            host=host,
        )
        node.attempts.append(att)
        self._emit(node, att)

        total_attempts = 1 + spec.retries
        last_msg = ""
        for n in range(1, total_attempts + 1):
            att.attempt = n
            ok, msg = await execute_step(adapter, self.transport)
            last_msg = msg
            if ok:
                node.status = StepStatus.OK
                att.status = StepStatus.OK
                att.message = msg
                att.finished_at = time.time()
                self._emit(node, att)
                return (True, msg)
            # Failed; stop retrying if this was the last attempt.
            if n == total_attempts:
                att.status = StepStatus.FAILED
                att.message = msg
                att.finished_at = time.time()
                self._emit(node, att)
                node.status = StepStatus.FAILED
                return (False, msg)
            await asyncio.sleep(spec.retry_backoff_seconds)
        # unreachable
        return (False, last_msg)

    async def _rollback(self, succeeded_ids: list[str]) -> None:
        """Best-effort reversal of every previously-OK step."""
        for step_id in reversed(succeeded_ids):
            node = self.plan.nodes[step_id]
            spec = self._step_by_id[step_id]
            host = self.resolve_host(None, node)
            adapter = build_step_adapter(spec, host)
            node.status = StepStatus.ROLLING_BACK
            self._emit(
                node,
                StepAttempt(
                    step_id=step_id,
                    attempt=0,
                    status=StepStatus.ROLLING_BACK,
                    host=host,
                    message="rollback",
                ),
            )
            ok, msg = await reverse_step(adapter, self.transport)
            node.status = StepStatus.ROLLED_BACK if ok else StepStatus.ROLLBACK_FAILED
            self._emit(
                node,
                StepAttempt(
                    step_id=step_id,
                    attempt=0,
                    status=node.status,
                    host=host,
                    message=msg,
                ),
            )

    async def _skip_unmet(self, node: PlanNode, reason: str) -> None:
        node.status = StepStatus.SKIPPED
        self._emit(
            node,
            StepAttempt(
                step_id=node.step_id,
                attempt=0,
                status=StepStatus.SKIPPED,
                message=reason,
            ),
        )

    def _deps_ok(self, node: PlanNode) -> bool:
        for dep in node.depends_on:
            dep_node = self.plan.nodes.get(dep)
            if dep_node is None or dep_node.status != StepStatus.OK:
                return False
        return True

    async def run(self) -> RunReport:
        report = RunReport(plan=self.plan, started_at=time.time())
        succeeded: list[str] = []
        aborted = False
        try:
            for step_id in self.plan.topo_order:
                node = self.plan.nodes[step_id]
                # Reversal steps are free-standing; they only run during rollback.
                spec = self._step_by_id[step_id]
                if spec.id.startswith("rev:"):
                    await self._skip_unmet(node, reason="reversal-handled-by-executor")
                    continue
                if not self._deps_ok(node):
                    await self._skip_unmet(node, reason="dependency-not-ok")
                    continue
                spec = self._step_by_id[step_id]
                ok, msg = await self._run_one(node, spec.on_host)
                if ok:
                    succeeded.append(step_id)
                else:
                    # Mark descendants as skipped.
                    for sid in self._downstream(step_id):
                        await self._skip_unmet(
                            self.plan.nodes[sid],
                            reason=f"upstream-failed:{step_id}",
                        )
                    if self.rollback_on_failure:
                        await self._rollback(succeeded)
                    break
        except asyncio.CancelledError:
            aborted = True
            raise
        finally:
            report.finished_at = time.time()
            report.aborted = aborted
            with suppress(Exception):
                await self.transport.close()
        return report

    def _downstream(self, step_id: str) -> list[str]:
        seen: set[str] = set()
        frontier: list[str] = [step_id]
        while frontier:
            cur = frontier.pop()
            for sid, n in self.plan.nodes.items():
                if sid in seen:
                    continue
                if cur in n.depends_on:
                    seen.add(sid)
                    frontier.append(sid)
        return sorted(seen)
