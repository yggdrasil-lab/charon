"""Shared stdlib-only runtime package for Charon backup and sync services."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from charon_runtime.git_backup import (
    BackupRunReport,
    GitBackupConfig,
    GitBackupEngine,
    GitBackupTarget,
    TargetSyncResult,
    load_git_targets,
)
from charon_runtime.heartbeat import touch_heartbeat
from charon_runtime.notify import HttpIrisNotifier, IrisAlert, IrisNotifier

if TYPE_CHECKING:
    from charon_runtime.snapshot_lifecycle import (
        BackupBatchReport,
        PostgresRunner,
        SnapshotConfig,
        SnapshotInfo,
        SnapshotLifecycle,
        SnapshotTarget,
        SubprocessPostgresRunner,
        TargetBackupResult,
        VerifyResult,
        load_snapshot_targets,
    )

_SNAPSHOT_EXPORTS = frozenset(
    {
        "BackupBatchReport",
        "PostgresRunner",
        "SnapshotConfig",
        "SnapshotInfo",
        "SnapshotLifecycle",
        "SnapshotTarget",
        "SubprocessPostgresRunner",
        "TargetBackupResult",
        "VerifyResult",
        "load_snapshot_targets",
    }
)


def __getattr__(name: str) -> Any:
    if name in _SNAPSHOT_EXPORTS:
        module = importlib.import_module("charon_runtime.snapshot_lifecycle")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BackupBatchReport",
    "BackupRunReport",
    "GitBackupConfig",
    "GitBackupEngine",
    "GitBackupTarget",
    "HttpIrisNotifier",
    "IrisAlert",
    "IrisNotifier",
    "PostgresRunner",
    "SnapshotConfig",
    "SnapshotInfo",
    "SnapshotLifecycle",
    "SnapshotTarget",
    "SubprocessPostgresRunner",
    "TargetBackupResult",
    "TargetSyncResult",
    "VerifyResult",
    "load_git_targets",
    "load_snapshot_targets",
    "touch_heartbeat",
]
