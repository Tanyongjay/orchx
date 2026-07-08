"""Built-in deploy step implementations.

Each ``Step`` value object knows how to:
  * describe its own outcome (id, target host, kind)
  * produce an action that the engine hands to a transport

The transport is the only thing that talks to the OS. Steps do not.
This separation is what lets the same descriptor run against the mock
and against a real Windows host unchanged.
"""

from orchx.steps.steps import (
    Action,
    BuiltinStep,
    build_step_adapter,
    execute_step,
    reverse_step,
)

__all__ = ["Action", "BuiltinStep", "build_step_adapter", "execute_step", "reverse_step"]
