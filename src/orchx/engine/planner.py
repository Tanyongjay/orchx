"""Build a Plan (DAG) from a Descriptor.

The plan is a pure computation: there are no transport calls, no
I/O, no side effects. The same input always yields the same plan.
"""

from __future__ import annotations

from collections import defaultdict, deque

from orchx.descriptor.models import Descriptor, StepSpec, StepType
from orchx.engine.models import Plan, PlanNode

# Every step that mutates state has a paired reversal step that the
# engine auto-generates when ``type`` ends with ``-remove``.
REVERSIBLE_TYPE_PAIRS: dict[StepType, StepType] = {
    StepType.IIS_SITE: StepType.IIS_SITE_REMOVE,
    StepType.COM_REGISTER: StepType.COM_UNREGISTER,
}


def _deps(step: StepSpec) -> list[str]:
    """Combine explicit ``needs`` IDs and ``needs_role_state`` markers."""
    return list(step.needs)


def build_plan(desc: Descriptor) -> Plan:
    """Construct a Plan from a validated descriptor.

    Validates:
      * every dependency refers to an existing step ID;
      * the graph is acyclic;
      * reversal pairs exist when called for.
    """
    step_by_id = {s.id: s for s in desc.steps}

    nodes: dict[str, PlanNode] = {}
    for s in desc.steps:
        deps = _deps(s)
        # If this is a reversal step `rev:<forward>`, derive a default
        # dependency on its forward step if the user hasn't specified any.
        if s.id.startswith("rev:") and not deps:
            forward_id = s.id[4:]
            if forward_id in step_by_id:
                deps = [forward_id]
        nodes[s.id] = PlanNode(
            step_id=s.id,
            depends_on=deps,
        )

    # Convention: a reversal step id of the form "rev:<forward_id>" is
    # auto-wired to its forward step. The reversal will not run during a
    # forward deploy; the executor invokes it directly on failure.
    for s in desc.steps:
        if s.id.startswith("rev:"):
            forward_id = s.id[4:]
            if forward_id not in step_by_id:
                raise ValueError(
                    f"reversal step {s.id!r} references missing forward {forward_id!r}"
                )
            nodes[forward_id].reverse_step_id = s.id
        else:
            rev_id = f"rev:{s.id}"
            if rev_id in step_by_id:
                nodes[s.id].reverse_step_id = rev_id

    # Validate references and acyclicity.
    missing = [dep for n in nodes.values() for dep in n.depends_on if dep not in step_by_id]
    if missing:
        raise ValueError(f"steps reference missing deps: {sorted(set(missing))}")

    topo: list[str] = _topo_sort(nodes)
    return Plan(nodes=nodes, topo_order=topo)


def _topo_sort(nodes: dict[str, PlanNode]) -> list[str]:
    """Kahn's algorithm. Raises on cycle."""
    in_deg: dict[str, int] = defaultdict(int)
    adj: dict[str, list[str]] = defaultdict(list)
    for n in nodes.values():
        for d in n.depends_on:
            adj[d].append(n.step_id)
            in_deg[n.step_id] += 1

    order: list[str] = []
    ready: deque[str] = deque(n for n in nodes if in_deg[n] == 0)
    while ready:
        cur = ready.popleft()
        order.append(cur)
        for nxt in adj[cur]:
            in_deg[nxt] -= 1
            if in_deg[nxt] == 0:
                ready.append(nxt)
    if len(order) != len(nodes):
        raise ValueError("step DAG has a cycle")
    return order
