"""Engine data models.

The engine treats steps as nodes in a DAG and tracks state across
attempts. ``StepStatus`` is a str-valued Enum so it serialises
trivially to JSON. The executor enforces legal transitions:

    PENDING   -> RUNNING   -> OK
                         -> FAILED       -> (retry) -> RUNNING
                         -> SKIPPED
    FAILED    -> ROLLING_BACK -> ROLLED_BACK
                                  -> ROLLBACK_FAILED
"""

from dataclasses import dataclass, field
from enum import StrEnum


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"


@dataclass
class StepAttempt:
    step_id: str
    attempt: int
    status: StepStatus
    message: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    host: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "attempt": self.attempt,
            "status": self.status.value,
            "message": self.message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "host": self.host,
        }


@dataclass
class PlanNode:
    step_id: str
    depends_on: list[str]
    status: StepStatus = StepStatus.PENDING
    attempts: list[StepAttempt] = field(default_factory=list)
    reverse_step_id: str | None = None


@dataclass
class Plan:
    nodes: dict[str, PlanNode] = field(default_factory=dict)
    topo_order: list[str] = field(default_factory=list)

    def all_done(self) -> bool:
        return all(n.status in (StepStatus.OK, StepStatus.SKIPPED) for n in self.nodes.values())

    def any_failed(self) -> bool:
        return any(
            n.status in (StepStatus.FAILED, StepStatus.ROLLBACK_FAILED) for n in self.nodes.values()
        )


@dataclass
class RunReport:
    plan: Plan
    started_at: float = 0.0
    finished_at: float = 0.0
    aborted: bool = False

    @property
    def exit_code(self) -> int:
        if self.aborted:
            return 130
        if self.plan.any_failed():
            return 1
        return 0
