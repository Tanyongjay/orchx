"""End-to-end executor tests against the MockTransport."""

import asyncio
from pathlib import Path

from orchx.descriptor.loader import load_descriptor
from orchx.engine.executor import Executor
from orchx.engine.models import StepStatus
from orchx.engine.planner import build_plan
from orchx.transports.mock import MockConfig, MockTransport

REPO_ROOT = Path(__file__).resolve().parents[1]
DESCRIPTOR = REPO_ROOT / "descriptors" / "sample_webapp_erp.yaml"


def _run(transport: MockTransport, *, rollback: bool = True) -> object:
    desc = load_descriptor(DESCRIPTOR)
    plan = build_plan(desc)
    exec_ = Executor(
        descriptor=desc,
        plan=plan,
        transport=transport,
        rollback_on_failure=rollback,
    )
    return asyncio.run(exec_.run())


def test_happy_path_records_actions():
    transport = MockTransport()
    report = _run(transport)

    assert report.exit_code == 0, report.plan.topo_order
    assert transport.journal.actions_for("local", "sql"), "sql action not recorded"
    assert transport.journal.actions_for("local", "iis-site")
    assert transport.journal.actions_for("local", "com-register")


def test_failure_triggers_rollback_of_prior_ok_steps():
    """A package step that fails must trigger rollback of all prior OK steps."""
    cfg = MockConfig.from_json('{"local":[{"action":"package","exit_code":1,"fail_times":99}]}')
    transport = MockTransport(config=cfg)
    report = _run(transport)

    assert report.plan.any_failed()
    # iis-site-remove and com-unregister are the reversals.
    rem = transport.journal.actions_for("local", "iis-site-remove")
    unr = transport.journal.actions_for("local", "com-unregister")
    assert rem, "rollback of iis-site never ran"
    assert unr, "rollback of bridge never ran"
    # Exit code MUST be 1 on forward failure even when rollback succeeded.
    # The semantic is "did the deploy succeed?" — a forward step failed,
    # so no, regardless of how clean the rollback was.
    assert report.exit_code == 1, f"expected exit_code=1 on forward failure, got {report.exit_code}"


def test_retry_succeeds_after_one_failure():
    """A powershell action that fails once must be retried and succeed."""
    cfg = MockConfig.from_json('{"local":[{"action":"powershell","exit_code":2,"fail_times":1}]}')
    transport = MockTransport(config=cfg)
    desc = load_descriptor(DESCRIPTOR)
    # Override an early powershell-bearing step to allow retries on each attempt.
    next_step = next(s for s in desc.steps if s.id == "bridge.register.x86")
    next_step.retries = 2

    plan = build_plan(desc)
    report = asyncio.run(
        Executor(
            descriptor=desc,
            plan=plan,
            transport=transport,
        ).run()
    )

    # Even with retries, the overall outcome must be 'ok'
    # (powershell action recovers after one failure).
    assert report.exit_code == 0, report.plan.topo_order
    statuses = [plan.nodes[s].status for s in plan.topo_order]
    assert all(s in (StepStatus.OK, StepStatus.SKIPPED) for s in statuses)


def test_skip_unmet_when_dependency_fails():
    """Failure of an early step must skip downstream steps."""
    cfg = MockConfig.from_json('{"local":[{"action":"iis-site","exit_code":1,"fail_times":99}]}')
    transport = MockTransport(config=cfg)
    report = _run(transport)

    skip_nodes = [sid for sid, n in report.plan.nodes.items() if n.status == StepStatus.SKIPPED]
    assert skip_nodes, "expected downstream steps to be skipped"
    # Forward failure => exit 1, regardless of how many steps were skipped.
    assert report.exit_code == 1


def test_rollback_disabled_still_returns_nonzero():
    """With rollback off, a forward failure must still surface as exit 1."""
    cfg = MockConfig.from_json('{"local":[{"action":"package","exit_code":1,"fail_times":99}]}')
    transport = MockTransport(config=cfg)
    report = _run(transport, rollback=False)

    assert report.exit_code == 1
    # No reversals should have run.
    rem = transport.journal.actions_for("local", "iis-site-remove")
    unr = transport.journal.actions_for("local", "com-unregister")
    assert not rem, "rollback must not run when rollback_on_failure=False"
    assert not unr, "rollback must not run when rollback_on_failure=False"


# ---------- exit-code matrix ----------


def test_exit_code_matrix_happy_is_zero():
    assert _run(MockTransport()).exit_code == 0


def test_exit_code_matrix_forward_failure_is_one():
    cfg = MockConfig.from_json('{"local":[{"action":"package","exit_code":1,"fail_times":99}]}')
    assert _run(MockTransport(config=cfg)).exit_code == 1


def test_exit_code_matrix_early_failure_is_one():
    """Failure on the very first step must also yield exit 1."""
    cfg = MockConfig.from_json('{"local":[{"action":"iis-site","exit_code":1,"fail_times":99}]}')
    assert _run(MockTransport(config=cfg)).exit_code == 1
