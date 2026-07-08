"""Descriptor package: YAML/Pydantic models describing deployable systems."""

from orchx.descriptor.loader import load_descriptor, render_template
from orchx.descriptor.models import (
    Descriptor,
    NodeSpec,
    RoleSpec,
    StepSpec,
    StepType,
    TopologySpec,
)

__all__ = [
    "Descriptor",
    "NodeSpec",
    "RoleSpec",
    "StepSpec",
    "StepType",
    "TopologySpec",
    "load_descriptor",
    "render_template",
]
