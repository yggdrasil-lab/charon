"""Centralized database and archive snapshot lifecycle engine and CLI."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import fnmatch
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Protocol

from charon_runtime.heartbeat import touch_heartbeat
from charon_runtime.notify import DEFAULT_IRIS_URL, HttpIrisNotifier, IrisAlert, IrisNotifier

DEFAULT_TARGETS_PATH = Path("/etc/charon-dumper/targets.json")
DEFAULT_BACKUP_DIR = Path("/backups")
DEFAULT_HEARTBEAT_PATH = Path("/tmp/charon_dumper_heartbeat")
DEFAULT_KEEP_DAYS = 30


class RestoreCancelledError(RuntimeError):
    """Raised when an interactive restore is cancelled by the operator."""


@dataclass(frozen=True)
class ArtifactSpec:
    """Artifact naming and glob conventions for a single snapshot target kind."""

    kind: str
    infix: str
    suffix: str

    @property
    def glob(self) -> str:
        """Return non-recursive filename glob pattern for this target kind."""
        return f"*{self.infix}*{self.suffix}"

    def build_filename(self, base_name: str, timestamp: str) -> str:
        """Build canonical snapshot filename from target base name and timestamp."""
        return f"{base_name}{self.infix}{timestamp}{self.suffix}"


ARTIFACT_SPECS: dict[str, ArtifactSpec] = {
    "postgres": ArtifactSpec(kind="postgres", infix="_db_", suffix=".dump"),
    "sqlite": ArtifactSpec(kind="sqlite", infix="_db_", suffix=".sqlite"),
    "archive": ArtifactSpec(kind="archive", infix="_config_", suffix=".tar.gz"),
}


@dataclass(frozen=True)
class SnapshotTarget:
    """Immutable snapshot target configuration loaded from JSON."""

    name: str
    kind: str
    node: str | None = None
    host: str = "localhost"
    port: int = 5432
    user: str = "postgres"
    database: str = "postgres"
    password_file: Path | None = None
    password_env: str = ""
    source: Path | None = None
    source_dir: Path | None = None
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class SnapshotConfig:
    """Runtime configuration for SnapshotLifecycle."""

    targets_path: Path
    backup_dir: Path
    node: str
    keep_days: int = DEFAULT_KEEP_DAYS
    heartbeat_path: Path = DEFAULT_HEARTBEAT_PATH


@dataclass(frozen=True)
class SnapshotInfo:
    """Metadata for a discovered snapshot file on disk."""

    path: Path
    name: str
    size_bytes: int
    mtime: float


@dataclass(frozen=True)
class TargetBackupResult:
    """Outcome of backing up a single target."""

    name: str
    kind: str
    ok: bool
    message: str
    snapshot_path: Path | None = None
    size_bytes: int = 0
    pruned_count: int = 0


@dataclass(frozen=True)
class BackupBatchReport:
    """Aggregate report across all node-matched backup targets."""

    node: str
    total: int
    failed: int
    results: tuple[TargetBackupResult, ...]


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of non-destructive verification for a single target."""

    name: str
    kind: str
    ok: bool
    message: str
    snapshot_path: Path | None = None
    detail_count: int = 0


class PostgresRunner(Protocol):
    """Port abstracting PostgreSQL CLI operations for testability."""

    def is_ready(self, target: SnapshotTarget, password: str) -> bool:
        """Return True if the target PostgreSQL instance accepts connections."""
        ...

    def dump(self, target: SnapshotTarget, password: str, output_path: Path) -> None:
        """Dump the target PostgreSQL database in custom format to output_path."""
        ...

    def restore(self, target: SnapshotTarget, password: str, snapshot_path: Path) -> None:
        """Restore the target PostgreSQL database from snapshot_path."""
        ...

    def list_toc_entries(self, snapshot_path: Path) -> int:
        """Return count of TOC entries in snapshot_path, raising on invalid dump."""
        ...


class SubprocessPostgresRunner:
    """Subprocess adapter invoking PostgreSQL 16 client utilities."""

    def __init__(self, base_env: Mapping[str, str] | None = None) -> None:
        self._base_env = os.environ if base_env is None else base_env

    def _pg_env(self, password: str) -> dict[str, str]:
        env = dict(self._base_env)
        env["PGPASSWORD"] = password
        return env

    def is_ready(self, target: SnapshotTarget, password: str) -> bool:
        """Probe PostgreSQL readiness via pg_isready."""
        cmd = [
            "pg_isready",
            "-h",
            target.host,
            "-p",
            str(target.port),
            "-U",
            target.user,
            "-d",
            target.database,
        ]
        proc = subprocess.run(cmd, env=self._pg_env(password), capture_output=True, text=True, check=False)
        return proc.returncode == 0

    def dump(self, target: SnapshotTarget, password: str, output_path: Path) -> None:
        """Execute pg_dump -F c -f and raise RuntimeError on failure."""
        cmd = [
            "pg_dump",
            "-h",
            target.host,
            "-p",
            str(target.port),
            "-U",
            target.user,
            "-d",
            target.database,
            "-F",
            "c",
            "-f",
            str(output_path),
        ]
        _run_checked(cmd, env=self._pg_env(password), context=f"pg_dump for '{target.name}'")

    def restore(self, target: SnapshotTarget, password: str, snapshot_path: Path) -> None:
        """Execute pg_restore --clean --if-exists and raise RuntimeError on failure."""
        cmd = [
            "pg_restore",
            "-h",
            target.host,
            "-p",
            str(target.port),
            "-U",
            target.user,
            "-d",
            target.database,
            "--clean",
            "--if-exists",
            str(snapshot_path),
        ]
        _run_checked(cmd, env=self._pg_env(password), context=f"pg_restore for '{target.name}'")

    def list_toc_entries(self, snapshot_path: Path) -> int:
        """Run pg_restore --list and return numeric TOC entry count."""
        proc = _run_checked(
            ["pg_restore", "--list", str(snapshot_path)],
            env=dict(self._base_env),
            context=f"pg_restore --list for '{snapshot_path.name}'",
        )
        return sum(1 for line in proc.stdout.splitlines() if line[:1].isdigit())


def _run_checked(
    cmd: Sequence[str],
    env: Mapping[str, str],
    context: str,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, env=dict(env), capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"{context} failed: {detail}")
    return proc


def _parse_target(entry: dict[str, object]) -> SnapshotTarget:
    name = str(entry["name"])
    kind = str(entry["type"])
    if kind not in ARTIFACT_SPECS:
        raise ValueError(f"Unsupported target type {kind!r} for target {name!r}")
    raw_node = entry.get("node")
    node = str(raw_node) if raw_node is not None else None
    raw_pw_file = entry.get("password_file")
    raw_source = entry.get("source")
    raw_source_dir = entry.get("source_dir")
    raw_paths = entry.get("paths") or ()
    return SnapshotTarget(
        name=name,
        kind=kind,
        node=node,
        host=str(entry.get("host") or "localhost"),
        port=int(entry.get("port") or 5432),
        user=str(entry.get("user") or "postgres"),
        database=str(entry.get("database") or "postgres"),
        password_file=Path(str(raw_pw_file)) if raw_pw_file else None,
        password_env=str(entry.get("password_env") or ""),
        source=Path(str(raw_source)) if raw_source else None,
        source_dir=Path(str(raw_source_dir)) if raw_source_dir else None,
        paths=tuple(str(p) for p in raw_paths),
    )


def load_snapshot_targets(
    targets_path: Path,
    node: str | None = None,
) -> tuple[SnapshotTarget, ...]:
    """Load snapshot targets from JSON, optionally filtering by node."""
    raw = json.loads(targets_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"Expected JSON array in {targets_path}")

    targets: list[SnapshotTarget] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError(f"Expected target object in {targets_path}")
        target = _parse_target(entry)
        if node is None or target.node in (None, "*", node):
            targets.append(target)
    return tuple(targets)


def resolve_postgres_password(target: SnapshotTarget, env: Mapping[str, str]) -> str:
    """Resolve PostgreSQL password from password_file or env without eval or defaults."""
    password = ""
    if target.password_file is not None and target.password_file.is_file():
        password = target.password_file.read_text(encoding="utf-8").strip()
    if not password and target.password_env:
        password = env.get(target.password_env, "").strip()
    if not password:
        raise ValueError(
            f"Missing PostgreSQL password for target '{target.name}' "
            f"(password_file={target.password_file}, password_env={target.password_env!r})."
        )
    return password


def _default_confirm(prompt: str) -> bool:
    print(prompt, end="", flush=True)
    answer = sys.stdin.readline().strip()
    return answer == "yes"


def _format_size(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "K", "M", "G", "T"):
        if value < 1024.0 or unit == "T":
            return f"{int(value)}B" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{size_bytes}B"


def _copy_sqlite(source_path: Path, dest_path: Path) -> None:
    src_conn: sqlite3.Connection | None = None
    dst_conn: sqlite3.Connection | None = None
    try:
        src_conn = sqlite3.connect(str(source_path))
        dst_conn = sqlite3.connect(str(dest_path))
        src_conn.backup(dst_conn)
    finally:
        if dst_conn is not None:
            dst_conn.close()
        if src_conn is not None:
            src_conn.close()


def _check_sqlite_integrity(db_path: Path) -> int:
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("PRAGMA integrity_check;").fetchone()
        status = str(row[0]) if row else ""
        if status != "ok":
            raise RuntimeError(f"Integrity check failed ({status or 'empty'})")
        count_row = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table';"
        ).fetchone()
        return int(count_row[0]) if count_row else 0
    finally:
        if conn is not None:
            conn.close()


def _validate_link_target(member: tarfile.TarInfo, dest_root: Path) -> None:
    link_raw = member.linkname
    if not link_raw or PurePosixPath(link_raw).is_absolute():
        raise ValueError(f"Unsafe archive link target: {member.name} -> {link_raw}")
    if member.issym():
        base = (dest_root / member.name).parent
        resolved = (base / link_raw).resolve()
    else:
        resolved = (dest_root / link_raw).resolve()
    if not resolved.is_relative_to(dest_root):
        raise ValueError(f"Archive link escapes destination: {member.name} -> {link_raw}")


def _validate_tar_members(
    members: Sequence[tarfile.TarInfo],
    source_dir: Path,
) -> list[tarfile.TarInfo]:
    dest_root = source_dir.resolve()
    validated: list[tarfile.TarInfo] = []
    for member in members:
        posix_path = PurePosixPath(member.name)
        if posix_path.is_absolute() or ".." in posix_path.parts:
            raise ValueError(f"Unsafe archive member path: {member.name}")
        if member.isdev() or member.isfifo():
            raise ValueError(f"Unsupported special archive entry: {member.name}")
        resolved = (dest_root / member.name).resolve()
        if not resolved.is_relative_to(dest_root):
            raise ValueError(f"Archive member escapes destination: {member.name}")
        if member.issym() or member.islnk():
            _validate_link_target(member, dest_root)
        validated.append(member)
    return validated


class SnapshotLifecycle:
    """Deep module governing backup, retention, discovery, verification, and restore."""

    def __init__(
        self,
        config: SnapshotConfig,
        notifier: IrisNotifier,
        postgres: PostgresRunner | None = None,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], float] | None = None,
        confirm_fn: Callable[[str], bool] | None = None,
    ) -> None:
        self._config = config
        self._notifier = notifier
        self._env = os.environ if env is None else env
        self._postgres = postgres or SubprocessPostgresRunner(self._env)
        self._clock = clock or time.time
        self._confirm_fn = confirm_fn or _default_confirm

    def backup_all(self) -> BackupBatchReport:
        """Back up all node-matched targets, prune expired snapshots, touch heartbeat, and alert Iris."""
        targets = load_snapshot_targets(self._config.targets_path, node=self._config.node)
        timestamp = datetime.fromtimestamp(self._clock()).strftime("%Y%m%d_%H%M%S")
        results: list[TargetBackupResult] = []
        try:
            for target in targets:
                results.append(self._backup_target(target, timestamp))
        finally:
            touch_heartbeat(self._config.heartbeat_path)

        failed = sum(1 for item in results if not item.ok)
        report = BackupBatchReport(
            node=self._config.node,
            total=len(results),
            failed=failed,
            results=tuple(results),
        )
        if report.total > 0:
            self._send_batch_alert(report)
        return report

    def list_snapshots(
        self,
        target_name: str | None = None,
    ) -> dict[str, tuple[SnapshotInfo, ...]]:
        """Discover non-recursive maxdepth=1 snapshots matching target kind glob, newest first."""
        if target_name is not None:
            target = self._find_target(target_name)
            return {target.name: self._scan_target_dir(target)}

        targets = load_snapshot_targets(self._config.targets_path, node=self._config.node)
        return {target.name: self._scan_target_dir(target) for target in targets}

    def verify(self, target_name: str | None = None) -> tuple[VerifyResult, ...]:
        """Verify latest snapshot integrity non-destructively for one or all node targets."""
        if target_name is not None:
            try:
                target = self._find_target(target_name)
            except ValueError as exc:
                return (VerifyResult(name=target_name, kind="unknown", ok=False, message=str(exc)),)
            return (self._verify_target(target),)

        targets = load_snapshot_targets(self._config.targets_path, node=self._config.node)
        return tuple(self._verify_target(target) for target in targets)

    def restore(
        self,
        target_name: str,
        requested_file: str | None = None,
        confirm: bool = False,
    ) -> Path:
        """Restore a named or latest snapshot after confirmation, with SQLite rollback protection."""
        target = self._find_target(target_name)
        snapshot_path = self._resolve_snapshot_for_restore(target, requested_file)
        if not confirm:
            prompt = self._build_confirm_prompt(target, snapshot_path)
            if not self._confirm_fn(prompt):
                raise RestoreCancelledError(f"Restore cancelled for target '{target.name}'.")

        if target.kind == "postgres":
            self._restore_postgres(target, snapshot_path)
        elif target.kind == "sqlite":
            self._restore_sqlite(target, snapshot_path)
        elif target.kind == "archive":
            self._restore_archive(target, snapshot_path)
        else:
            raise ValueError(f"Unsupported target type {target.kind!r}")
        return snapshot_path

    def _find_target(self, target_name: str) -> SnapshotTarget:
        for target in load_snapshot_targets(self._config.targets_path):
            if target.name == target_name:
                return target
        raise ValueError(f"Target '{target_name}' not found in {self._config.targets_path}.")

    def _target_dir(self, target: SnapshotTarget) -> Path:
        return self._config.backup_dir / target.name

    def _scan_target_dir(self, target: SnapshotTarget) -> tuple[SnapshotInfo, ...]:
        target_dir = self._target_dir(target)
        if not target_dir.is_dir():
            return ()
        spec = ARTIFACT_SPECS[target.kind]
        items: list[SnapshotInfo] = []
        for entry in target_dir.iterdir():
            if entry.is_file() and fnmatch.fnmatch(entry.name, spec.glob):
                stat = entry.stat()
                items.append(
                    SnapshotInfo(
                        path=entry,
                        name=entry.name,
                        size_bytes=stat.st_size,
                        mtime=stat.st_mtime,
                    )
                )
        items.sort(key=lambda item: (item.mtime, item.name), reverse=True)
        return tuple(items)

    def _backup_target(self, target: SnapshotTarget, timestamp: str) -> TargetBackupResult:
        target_dir = self._target_dir(target)
        target_dir.mkdir(parents=True, exist_ok=True)
        spec = ARTIFACT_SPECS[target.kind]
        out_path = target_dir / spec.build_filename(Path(target.name).name, timestamp)
        try:
            if target.kind == "postgres":
                self._backup_postgres(target, out_path)
            elif target.kind == "sqlite":
                self._backup_sqlite(target, out_path)
            elif target.kind == "archive":
                self._backup_archive(target, out_path)
            else:
                raise ValueError(f"Unsupported target type {target.kind!r}")
        except Exception as exc:
            out_path.unlink(missing_ok=True)
            return TargetBackupResult(
                name=target.name,
                kind=target.kind,
                ok=False,
                message=str(exc),
            )

        pruned = self._prune_expired(target_dir, spec.glob)
        size_bytes = out_path.stat().st_size
        return TargetBackupResult(
            name=target.name,
            kind=target.kind,
            ok=True,
            message=f"{_format_size(size_bytes)} ({target.kind})",
            snapshot_path=out_path,
            size_bytes=size_bytes,
            pruned_count=pruned,
        )

    def _backup_postgres(self, target: SnapshotTarget, out_path: Path) -> None:
        password = resolve_postgres_password(target, self._env)
        if not self._postgres.is_ready(target, password):
            raise RuntimeError(f"Unreachable on {target.host}:{target.port}")
        self._postgres.dump(target, password, out_path)

    def _backup_sqlite(self, target: SnapshotTarget, out_path: Path) -> None:
        if target.source is None or not target.source.is_file():
            raise FileNotFoundError(f"File not found ({target.source})")
        _copy_sqlite(target.source, out_path)

    def _backup_archive(self, target: SnapshotTarget, out_path: Path) -> None:
        if target.source_dir is None or not target.source_dir.is_dir():
            raise FileNotFoundError(f"Directory not found ({target.source_dir})")
        existing = [rel for rel in target.paths if (target.source_dir / rel).exists()]
        if not existing:
            raise FileNotFoundError(f"No matching paths in {target.source_dir}")
        with tarfile.open(out_path, "w:gz") as archive:
            for rel in existing:
                archive.add(target.source_dir / rel, arcname=rel)

    def _prune_expired(self, target_dir: Path, pattern: str) -> int:
        if self._config.keep_days <= 0 or not target_dir.is_dir():
            return 0
        cutoff = self._clock() - (self._config.keep_days * 86400)
        pruned = 0
        for entry in target_dir.iterdir():
            if not entry.is_file() or not fnmatch.fnmatch(entry.name, pattern):
                continue
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                pruned += 1
        return pruned

    def _send_batch_alert(self, report: BackupBatchReport) -> None:
        succeeded = report.total - report.failed
        summary = "\n".join(f"- {res.name}: {res.message}" for res in report.results)
        if report.failed > 0:
            severity = "warning"
            title = "Daily Database Backup Completed with Issues"
            details = (
                f"Node: {report.node}\n"
                f"Succeeded: {succeeded}, Failed: {report.failed}\n\n"
                f"Targets:\n{summary}"
            )
        else:
            severity = "info"
            title = "Daily Database Backup Succeeded"
            details = (
                f"Node: {report.node}\n"
                f"All {succeeded} targets backed up successfully.\n\n"
                f"Targets:\n{summary}\n\n"
                f"Local retention pruned > {self._config.keep_days} days."
            )
        self._notifier.send(
            IrisAlert(
                service="charon-dumper",
                severity=severity,
                title=title,
                details=details,
                node=report.node,
            )
        )

    def _verify_target(self, target: SnapshotTarget) -> VerifyResult:
        snapshots = self._scan_target_dir(target)
        if not snapshots:
            target_dir = self._target_dir(target)
            msg = f"Directory not found ({target_dir})" if not target_dir.is_dir() else "No matching snapshots found"
            return VerifyResult(name=target.name, kind=target.kind, ok=False, message=msg)

        latest = snapshots[0]
        try:
            if target.kind == "postgres":
                count = self._postgres.list_toc_entries(latest.path)
                msg = f"{latest.name} ({_format_size(latest.size_bytes)}, {count} TOC entries)"
            elif target.kind == "sqlite":
                count = self._verify_sqlite(latest.path)
                msg = f"{latest.name} ({_format_size(latest.size_bytes)}, {count} tables, integrity OK)"
            elif target.kind == "archive":
                count = self._verify_archive(latest.path)
                msg = f"{latest.name} ({_format_size(latest.size_bytes)}, {count} entries)"
            else:
                raise ValueError(f"Unsupported target type {target.kind!r}")
        except Exception as exc:
            return VerifyResult(
                name=target.name,
                kind=target.kind,
                ok=False,
                message=f"Verification failed on {latest.name}: {exc}",
                snapshot_path=latest.path,
            )
        return VerifyResult(
            name=target.name,
            kind=target.kind,
            ok=True,
            message=msg,
            snapshot_path=latest.path,
            detail_count=count,
        )

    def _verify_sqlite(self, snapshot_path: Path) -> int:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sandbox = Path(tmp_dir) / "sandbox.sqlite"
            _copy_sqlite(snapshot_path, sandbox)
            return _check_sqlite_integrity(sandbox)

    def _verify_archive(self, snapshot_path: Path) -> int:
        with tarfile.open(snapshot_path, "r:gz") as archive:
            return len(archive.getmembers())

    def _resolve_snapshot_for_restore(
        self,
        target: SnapshotTarget,
        requested_file: str | None,
    ) -> Path:
        if requested_file is None or requested_file == "latest":
            snapshots = self._scan_target_dir(target)
            if not snapshots:
                raise FileNotFoundError(
                    f"No matching snapshots found in '{self._target_dir(target)}'."
                )
            return snapshots[0].path

        direct = Path(requested_file)
        if direct.is_file():
            return direct
        target_dir = self._target_dir(target).resolve()
        candidate = (target_dir / requested_file).resolve()
        if candidate.is_relative_to(target_dir) and candidate.is_file():
            return candidate
        raise FileNotFoundError(f"Specified backup file '{requested_file}' not found.")

    def _build_confirm_prompt(self, target: SnapshotTarget, snapshot_path: Path) -> str:
        return (
            "======================================================================\n"
            f"RESTORE WARNING: Target '{target.name}'\n"
            f"Using backup:    {snapshot_path}\n"
            f"Target Type:     {target.kind}\n"
            "======================================================================\n"
            f"This operation will overwrite current data for target '{target.name}'.\n"
            "Are you sure you want to proceed? (yes/no): "
        )

    def _restore_postgres(self, target: SnapshotTarget, snapshot_path: Path) -> None:
        password = resolve_postgres_password(target, self._env)
        if not self._postgres.is_ready(target, password):
            raise RuntimeError(f"Target '{target.name}' is unreachable on {target.host}:{target.port}.")
        self._postgres.restore(target, password, snapshot_path)

    def _restore_sqlite(self, target: SnapshotTarget, snapshot_path: Path) -> None:
        if target.source is None:
            raise ValueError(f"SQLite target '{target.name}' missing source path.")
        source = target.source
        source.parent.mkdir(parents=True, exist_ok=True)
        safety_path = Path(f"{source}.pre_restore_bak")
        has_safety = False
        if source.is_file():
            shutil.copy2(source, safety_path)
            has_safety = True
        try:
            _copy_sqlite(snapshot_path, source)
            _check_sqlite_integrity(source)
        except Exception as exc:
            if has_safety and safety_path.is_file():
                shutil.move(str(safety_path), str(source))
            elif not has_safety:
                source.unlink(missing_ok=True)
            raise RuntimeError(f"SQLite restore failed for '{target.name}': {exc}") from exc
        safety_path.unlink(missing_ok=True)

    def _restore_archive(self, target: SnapshotTarget, snapshot_path: Path) -> None:
        if target.source_dir is None:
            raise ValueError(f"Archive target '{target.name}' missing source_dir path.")
        target.source_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(snapshot_path, "r:gz") as archive:
            members = _validate_tar_members(archive.getmembers(), target.source_dir)
            if hasattr(tarfile, "data_filter"):
                archive.extractall(path=target.source_dir, members=members, filter="data")
            else:
                archive.extractall(path=target.source_dir, members=members)


def _config_from_env(env: Mapping[str, str]) -> SnapshotConfig:
    node = env.get("NODE_HOSTNAME") or env.get("NODE") or socket.gethostname()
    keep_raw = env.get("BACKUP_KEEP_DAYS") or str(DEFAULT_KEEP_DAYS)
    return SnapshotConfig(
        targets_path=Path(env.get("TARGETS_CONFIG") or str(DEFAULT_TARGETS_PATH)),
        backup_dir=Path(env.get("BACKUP_DIR") or str(DEFAULT_BACKUP_DIR)),
        node=node,
        keep_days=int(keep_raw),
        heartbeat_path=Path(env.get("HEARTBEAT_PATH") or str(DEFAULT_HEARTBEAT_PATH)),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m charon_runtime.snapshot_lifecycle",
        description="Charon database and configuration snapshot lifecycle CLI.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("backup", help="Run scheduled backups for the current node.")

    for alias in ("list", "ls"):
        p_list = sub.add_parser(alias, help="List available snapshots.")
        p_list.add_argument("target", nargs="?", default=None)

    for alias in ("verify", "check"):
        p_verify = sub.add_parser(alias, help="Verify snapshot integrity non-destructively.")
        p_verify.add_argument("target", nargs="?", default=None)
        p_verify.add_argument("--all", action="store_true")

    p_restore = sub.add_parser("restore", help="Restore a target snapshot.")
    p_restore.add_argument("target")
    p_restore.add_argument("snapshot_file", nargs="?", default=None)
    p_restore.add_argument("--confirm", action="store_true")
    return parser


def _print_inventory(lifecycle: SnapshotLifecycle, config: SnapshotConfig, target: str | None) -> int:
    inventory = lifecycle.list_snapshots(target)
    if target is not None:
        snapshots = inventory[target]
        print(f"=== Available backups for '{target}' ({config.backup_dir / target}) ===")
        for item in snapshots:
            ts = datetime.fromtimestamp(item.mtime).strftime("%Y-%m-%d %H:%M:%S")
            print(f"{item.path} ({_format_size(item.size_bytes)}) Modified: {ts}")
        return 0

    print(f"=== Backup Inventory on node '{config.node}' ({config.backup_dir}) ===")
    for name, snapshots in inventory.items():
        if snapshots:
            latest = snapshots[0]
            print(f"  * {name:<20} - {len(snapshots)} snapshot(s), Latest: {latest.name} ({_format_size(latest.size_bytes)})")
        else:
            print(f"  * {name:<20} - 0 snapshots found")
    return 0


def _run_verify(lifecycle: SnapshotLifecycle, target: str | None, verify_all: bool) -> int:
    selected = None if (verify_all or target in (None, "--all")) else target
    results = lifecycle.verify(selected)
    for res in results:
        tag = "[PASS]" if res.ok else "[FAIL]"
        print(f"  {tag} {res.name:<20} [{res.kind}] - {res.message}")
    failed = sum(1 for res in results if not res.ok)
    if selected is None:
        passed = len(results) - failed
        print("-" * 70)
        print(f"Integrity Audit Complete: {passed} passed, {failed} failed out of {len(results)} targets.")
    return 0 if failed == 0 else 1


def _run_cli_command(
    args: argparse.Namespace,
    lifecycle: SnapshotLifecycle,
    config: SnapshotConfig,
) -> int:
    if args.command == "backup":
        report = lifecycle.backup_all()
        print(f"Backup finished: {report.total - report.failed} succeeded, {report.failed} failed.")
        return 0 if report.failed == 0 else 1
    if args.command in ("list", "ls"):
        return _print_inventory(lifecycle, config, args.target)
    if args.command in ("verify", "check"):
        return _run_verify(lifecycle, args.target, bool(args.all))
    if args.command == "restore":
        try:
            restored = lifecycle.restore(args.target, args.snapshot_file, confirm=bool(args.confirm))
        except RestoreCancelledError as exc:
            print(str(exc))
            return 0
        print(f"SUCCESS: Restored '{args.target}' from {restored}.")
        return 0
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint preserving dumper.sh and restore.sh compatibility."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1

    config = _config_from_env(os.environ)
    if not config.targets_path.is_file():
        print(f"ERROR: Targets configuration file '{config.targets_path}' not found.", file=sys.stderr)
        return 1

    notifier = HttpIrisNotifier(url=os.environ.get("IRIS_URL") or DEFAULT_IRIS_URL)
    lifecycle = SnapshotLifecycle(config=config, notifier=notifier)
    try:
        return _run_cli_command(args, lifecycle, config)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
