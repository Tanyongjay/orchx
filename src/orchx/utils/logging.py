"""Logging helpers — Typer + Rich + structured for tests."""

from __future__ import annotations

import logging
import sys
from typing import Any

from rich.console import Console
from rich.logging import RichHandler

_LOGGER_NAME = "orchx"


def make_logger(console: Console | None = None) -> Any:
    """Return a logger that prints to Rich and is reusable across the CLI."""
    logger = logging.getLogger(_LOGGER_NAME)
    if logger.handlers:
        return logger
    handler = RichHandler(console=console or Console(stderr=True), rich_tracebacks=True)
    handler.setLevel(logging.INFO)
    fmt = logging.Formatter("%(message)s")
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def configure_root(level: str = "INFO") -> None:
    """Set root logger to a non-Rich baseline (used by tests)."""
    root = logging.getLogger()
    root.handlers.clear()
    h = logging.StreamHandler(stream=sys.stderr)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(h)
    root.setLevel(level)
