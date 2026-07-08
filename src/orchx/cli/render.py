"""Rendering helpers — DAG as ASCII / Rich tables."""

from __future__ import annotations

from rich.table import Table

from orchx.descriptor.models import Descriptor
from orchx.engine.models import Plan


def render_plan_table(parsed: Descriptor, plan: Plan) -> Table:
    """Return a Rich table showing every step, its deps, and status."""
    tbl = Table(title="Execution plan", expand=True)
    tbl.add_column("step id", style="cyan")
    tbl.add_column("type")
    tbl.add_column("host (role)")
    tbl.add_column("needs")
    tbl.add_column("status")

    steps_by_id = {s.id: s for s in parsed.steps}
    for sid in plan.topo_order:
        spec = steps_by_id[sid]
        node = plan.nodes[sid]
        tbl.add_row(
            sid,
            spec.type.value,
            spec.on_host or "(control)",
            ", ".join(spec.needs) or "-",
            node.status.value,
        )
    return tbl
