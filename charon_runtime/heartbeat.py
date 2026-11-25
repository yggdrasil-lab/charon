"""Heartbeat file utilities for Charon container healthchecks."""

from pathlib import Path


def touch_heartbeat(path: Path) -> None:
    """Create parent directories if needed and touch the heartbeat file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
