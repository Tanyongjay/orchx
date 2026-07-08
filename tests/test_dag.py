"""Tests for the planner / DAG builder."""

from textwrap import dedent

import pytest

from orchx.descriptor.loader import load_descriptor
from orchx.engine.planner import build_plan


def test_dag_topo_orders_steps(tmp_path):
    p = tmp_path / "d.yaml"
    p.write_text(
        dedent(
            """
        system: { name: X, code: xx, version: "1.0.0" }
        install_root: C:\\\\app
        topology:
          roles:
            - { name: web, count: 1 }
        steps:
          - { id: a, type: powershell, script: "echo a" }
          - { id: b, type: powershell, script: "echo b", needs: [a] }
          - { id: c, type: powershell, script: "echo c", needs: [b] }
          - { id: d, type: powershell, script: "echo d", needs: [a, c] }
        """
        )
    )
    desc = load_descriptor(p)
    plan = build_plan(desc)
    assert plan.topo_order[0] == "a"
    assert plan.topo_order[-1] == "d"
    assert plan.nodes["b"].depends_on == ["a"]


def test_dag_rejects_unknown_dep(tmp_path):
    p = tmp_path / "d.yaml"
    p.write_text(
        dedent(
            """
        system: { name: X, code: xx, version: "1.0.0" }
        topology: { roles: [{ name: web, count: 1 }] }
        steps:
          - { id: a, type: powershell, script: "x", needs: [does-not-exist] }
        """
        )
    )
    with pytest.raises(ValueError, match="missing deps"):
        load_descriptor(p)
        build_plan(load_descriptor(p))


def test_dag_rejects_cycle(tmp_path):
    p = tmp_path / "d.yaml"
    p.write_text(
        dedent(
            """
        system: { name: X, code: xx, version: "1.0.0" }
        topology: { roles: [{ name: web, count: 1 }] }
        steps:
          - { id: a, type: powershell, script: "a", needs: [b] }
          - { id: b, type: powershell, script: "b", needs: [a] }
        """
        )
    )
    desc = load_descriptor(p)
    with pytest.raises(ValueError, match="cycle"):
        build_plan(desc)
