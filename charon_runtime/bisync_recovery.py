"""Rclone bisync recovery engine, environment setup, and CLI entrypoint."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import fnmatch
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import threading
from typing import Protocol

from charon_runtime.heartbeat import touch_heartbeat
from charon_runtime.notify import DEFAULT_IRIS_URL, HttpIrisNotifier, IrisAlert, IrisNotifier

BISYNC_COMMON_FLAGS: tuple[str, ...] = (
    "--verbose",
    "--checksum",
    "--resilient",
    "--recover",
    "--max-lock",
    "15m",
    "--create-empty-src-dirs",
)
_BISYNC_CACHE_SUBPATH = Path(".cache") / "rclone" / "bisync"
DEFAULT_SYNC_INTERVAL = 300
DEFAULT_SECRET_NAME = "charon_rclone_config"


class CacheState(str, Enum):
    """Health classification of the rclone bisync state directory."""

    MISSING_DIR = "missing_dir"
    HEALTHY_LST = "healthy_lst"
    RECOVERABLE_OLD = "recoverable_old"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class BisyncConfig:
    """Immutable configuration for BisyncRecoveryEngine."""

    gdrive_vault_path: str
    data_dir: Path = Path("/data")
    cache_root: Path = Path("/var/cache/rclone")
    sync_interval: int = DEFAULT_SYNC_INTERVAL
    secret_name: str = DEFAULT_SECRET_NAME
    secrets_dir: Path = Path("/run/secrets")
    rclone_config_dest: Path = Path("/tmp/rclone.conf")
    heartbeat_path: Path = Path("/tmp/charon_sync_heartbeat")

    @property
    def bisync_cache(self) -> Path:
        """Return the bisync listing directory under cache_root."""
        return self.cache_root / _BISYNC_CACHE_SUBPATH


@dataclass(frozen=True)
class CycleOutcome:
    """Outcome summary of a single bisync or resync cycle."""

    mode: str
    exit_code: int
    status: str
    cache_cleared: bool
    alerted: bool


class RcloneRunner(Protocol):
    """Port for executing rclone bisync commands."""

    def run_bisync(self, args: Sequence[str], env: Mapping[str, str]) -> int:
        """Execute rclone bisync with args and env, returning process exit code."""
        ...


class SubprocessRcloneRunner:
    """Subprocess adapter invoking the rclone binary."""

    def run_bisync(self, args: Sequence[str], env: Mapping[str, str]) -> int:
        """Run rclone bisync with RCLONE_CONFIG and cache env preserved."""
        completed = subprocess.run(
            ["rclone", "bisync", *args],
            env=dict(env),
            check=False,
        )
        return completed.returncode


def _log(message: str, *, stderr: bool = False) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    stream = sys.stderr if stderr else sys.stdout
    print(f"[{stamp}] {message}", file=stream, flush=True)


def _is_real_dir(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def _ensure_owner_exec(path: Path) -> None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if not (mode & stat.S_IXUSR):
            os.chmod(path, mode | stat.S_IXUSR)
    except OSError:
        return


def _iter_subdirs(directory: Path) -> list[Path]:
    try:
        with os.scandir(directory) as entries:
            return [Path(entry.path) for entry in entries if _is_real_dir(entry)]
    except OSError:
        return []


def _fix_directory_permissions(data_dir: Path) -> None:
    stack: list[Path] = [data_dir]
    while stack:
        current = stack.pop()
        _ensure_owner_exec(current)
        stack.extend(_iter_subdirs(current))


def prepare_rclone_environment(
    secret_name: str,
    secrets_dir: Path = Path("/run/secrets"),
    config_dest: Path = Path("/tmp/rclone.conf"),
    data_dir: Path | None = Path("/data"),
    fix_data_permissions: bool = True,
) -> Path | None:
    """Stage rclone config secret with 0600 mode and repair directory +x bits."""
    _log("Initializing rclone environment...")
    secret_path = secrets_dir / secret_name
    result_path: Path | None = None
    if secret_path.is_file():
        _log("Copying rclone config from secret to writable location...")
        config_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(secret_path, config_dest)
        os.chmod(config_dest, 0o600)
        os.environ["RCLONE_CONFIG"] = str(config_dest)
        result_path = config_dest
    else:
        _log(f"WARNING: Secret {secret_path} not found!", stderr=True)

    if fix_data_permissions and data_dir is not None and data_dir.is_dir():
        _log("Fixing directory permissions...")
        _fix_directory_permissions(data_dir)
    return result_path


def _has_matching_entry(directory: Path, pattern: str) -> bool:
    try:
        with os.scandir(directory) as entries:
            return any(fnmatch.fnmatch(entry.name, pattern) for entry in entries)
    except OSError:
        return False


def classify_cache(bisync_cache: Path) -> CacheState:
    """Classify bisync cache state without crossing subdirectories."""
    if not bisync_cache.is_dir():
        return CacheState.MISSING_DIR
    if _has_matching_entry(bisync_cache, "*.lst"):
        return CacheState.HEALTHY_LST
    if _has_matching_entry(bisync_cache, "*.lst-old"):
        return CacheState.RECOVERABLE_OLD
    return CacheState.INCOMPLETE


def _remove_child(child: Path) -> None:
    try:
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    except OSError:
        return


def clear_directory_contents(root: Path) -> None:
    """Remove all children inside root while preserving the root directory itself."""
    if not root.is_dir():
        return
    try:
        children = list(root.iterdir())
    except OSError:
        return
    for child in children:
        _remove_child(child)


class BisyncRecoveryEngine:
    """Deep module managing rclone bisync execution, cache recovery, and shutdown."""

    def __init__(
        self,
        config: BisyncConfig,
        notifier: IrisNotifier,
        runner: RcloneRunner | None = None,
        wait_fn: Callable[[float], bool] | None = None,
    ) -> None:
        self._config = config
        self._notifier = notifier
        self._runner: RcloneRunner = runner or SubprocessRcloneRunner()
        self._stop_event = threading.Event()
        self._wait_fn: Callable[[float], bool] = wait_fn or self._stop_event.wait
        self.stop_requested = False
        self.sync_in_progress = False
        self.consecutive_state_failures = 0

    def request_stop(self) -> None:
        """Signal graceful shutdown, waking an idle sleep without aborting active sync."""
        if self.sync_in_progress:
            _log("SIGTERM/SIGINT received. Sync in progress, will complete before shutdown...")
        else:
            _log("SIGTERM/SIGINT received. Shutting down gracefully...")
        self.stop_requested = True
        self._stop_event.set()

    def validate_startup_cache(self) -> CacheState:
        """Inspect startup bisync cache and clear only incomplete state."""
        state = classify_cache(self._config.bisync_cache)
        if state is CacheState.HEALTHY_LST:
            _log("Bisync cache healthy - will use normal (fast) sync")
        elif state is CacheState.RECOVERABLE_OLD:
            _log("Bisync primary listings missing, backup .lst-old present - will attempt --recover on first sync")
        elif state is CacheState.INCOMPLETE:
            _log("Bisync cache incomplete (missing .lst and .lst-old files) - clearing to force --resync")
            clear_directory_contents(self._config.cache_root)
        else:
            _log("No bisync cache found - first run, will use --resync")
        return state

    def run_once(self) -> CycleOutcome:
        """Execute one resync or normal bisync cycle and touch heartbeat."""
        self.sync_in_progress = True
        try:
            env = self._build_runner_env()
            remote = f"gdrive:{self._config.gdrive_vault_path}"
            if not self._config.bisync_cache.is_dir():
                return self._run_initial_resync(remote, env)
            return self._run_normal_sync(remote, env)
        finally:
            self.sync_in_progress = False
            touch_heartbeat(self._config.heartbeat_path)

    def run_forever(self, max_cycles: int | None = None) -> int:
        """Prepare environment, validate cache, and run cycles until stopped."""
        prepare_rclone_environment(
            secret_name=self._config.secret_name,
            secrets_dir=self._config.secrets_dir,
            config_dest=self._config.rclone_config_dest,
            data_dir=self._config.data_dir,
        )
        self.validate_startup_cache()
        cycles = 0
        while not self.stop_requested and (max_cycles is None or cycles < max_cycles):
            _log("----------------------------------------------------------------")
            self.run_once()
            cycles += 1
            if self.stop_requested:
                _log("Shutdown was requested during sync. Exiting gracefully without sleep...")
                break
            if max_cycles is not None and cycles >= max_cycles:
                break
            _log(f"Sync complete. Sleeping for {self._config.sync_interval} seconds...")
            self._wait_fn(self._config.sync_interval)
        _log("Rclone service stopped.")
        return 0

    def _build_runner_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["RCLONE_CONFIG"] = os.environ.get("RCLONE_CONFIG") or str(self._config.rclone_config_dest)
        return env

    def _run_initial_resync(self, remote: str, env: Mapping[str, str]) -> CycleOutcome:
        _log("First run of session: Initializing with --resync...")
        args = [remote, str(self._config.data_dir), "--resync", *BISYNC_COMMON_FLAGS]
        rc = self._runner.run_bisync(args, env)
        if rc == 0:
            self.consecutive_state_failures = 0
            _log("Initial resync successful.")
            return CycleOutcome("resync", rc, "initial_resync_ok", False, False)
        if classify_cache(self._config.bisync_cache) is CacheState.HEALTHY_LST:
            self.consecutive_state_failures = 0
            _log("WARNING: Resync completed with non-critical errors. Proceeding with normal sync.", stderr=True)
            return CycleOutcome("resync", rc, "initial_resync_non_critical", False, False)
        return self._clear_cache_and_alert(
            mode="resync",
            exit_code=rc,
            status="initial_resync_failed",
            title="Initial Resync Failed",
            details="Initial bisync resync failed entirely (missing state files). Retrying next loop.",
        )

    def _run_normal_sync(self, remote: str, env: Mapping[str, str]) -> CycleOutcome:
        _log("Subsequent run: Syncing changes...")
        args = [remote, str(self._config.data_dir), *BISYNC_COMMON_FLAGS]
        rc = self._runner.run_bisync(args, env)
        if rc == 0:
            self.consecutive_state_failures = 0
            _log("Sync successful.")
            return CycleOutcome("sync", rc, "sync_ok", False, False)
        state = classify_cache(self._config.bisync_cache)
        if state is CacheState.HEALTHY_LST:
            self.consecutive_state_failures = 0
            _log("WARNING: Sync completed with non-critical or retryable errors (state files intact).", stderr=True)
            return CycleOutcome("sync", rc, "sync_non_critical", False, False)
        if state is CacheState.RECOVERABLE_OLD and self.consecutive_state_failures < 1:
            self.consecutive_state_failures += 1
            _log(
                f"WARNING: Sync aborted with missing .lst files, but .lst-old backup exists (attempt {self.consecutive_state_failures}). Deferring cache clear so next cycle can self-heal via --recover.",
                stderr=True,
            )
            return CycleOutcome("sync", rc, "recovery_deferred", False, False)
        return self._clear_cache_and_alert(
            mode="sync",
            exit_code=rc,
            status="aborted_cache_cleared",
            title="Bisync Aborted (Cache Cleared)",
            details="Sync failed with unrecoverable state loss after recovery attempt. Cache cleared; next cycle will trigger full resync.",
        )

    def _clear_cache_and_alert(
        self,
        mode: str,
        exit_code: int,
        status: str,
        title: str,
        details: str,
    ) -> CycleOutcome:
        self.consecutive_state_failures = 0
        _log(f"ERROR: {title}. Clearing bisync cache to force resync on next run.", stderr=True)
        clear_directory_contents(self._config.cache_root)
        alerted = bool(
            self._notifier.send(
                IrisAlert(
                    service="charon-sync",
                    severity="error",
                    title=title,
                    details=details,
                )
            )
        )
        return CycleOutcome(mode, exit_code, status, True, alerted)


def _parse_sync_interval(raw: str | None) -> int:
    if raw is None or not raw.strip():
        return DEFAULT_SYNC_INTERVAL
    value = int(raw)
    if value < 0:
        raise ValueError(f"SYNC_INTERVAL must be non-negative, got {value}")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m charon_runtime.bisync_recovery",
        description="Charon rclone bisync recovery engine and environment helper.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Run the continuous rclone bisync recovery loop.")
    prep = sub.add_parser("prepare-env", help="Stage rclone secret config and fix /data directory +x bits.")
    prep.add_argument("--secrets-dir", default="/run/secrets")
    prep.add_argument("--config-dest", default="/tmp/rclone.conf")
    prep.add_argument("--data-dir", default="/data")
    prep.add_argument("--no-fix-permissions", action="store_true")
    return parser


def _run_command() -> int:
    vault_path = os.environ.get("GDRIVE_VAULT_PATH", "").strip()
    if not vault_path:
        print("ERROR: GDRIVE_VAULT_PATH must be set.", file=sys.stderr)
        return 1
    try:
        interval = _parse_sync_interval(os.environ.get("SYNC_INTERVAL"))
    except ValueError as exc:
        print(f"ERROR: Invalid SYNC_INTERVAL: {exc}", file=sys.stderr)
        return 1

    secret_name = os.environ.get("RCLONE_CONFIG_SECRET_NAME", "").strip() or DEFAULT_SECRET_NAME
    cache_root = Path(os.environ.get("RCLONE_CACHE_DIR", "").strip() or "/var/cache/rclone")
    iris_url = os.environ.get("IRIS_NOTIFY_URL", "").strip() or DEFAULT_IRIS_URL
    config = BisyncConfig(
        gdrive_vault_path=vault_path,
        cache_root=cache_root,
        sync_interval=interval,
        secret_name=secret_name,
    )
    engine = BisyncRecoveryEngine(config=config, notifier=HttpIrisNotifier(url=iris_url))
    signal.signal(signal.SIGTERM, lambda _sig, _frame: engine.request_stop())
    signal.signal(signal.SIGINT, lambda _sig, _frame: engine.request_stop())
    return engine.run_forever()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for Charon rclone bisync loop and environment setup."""
    args = _build_parser().parse_args(argv)
    if args.command == "run":
        return _run_command()
    secret_name = os.environ.get("RCLONE_CONFIG_SECRET_NAME", "").strip() or DEFAULT_SECRET_NAME
    prepare_rclone_environment(
        secret_name=secret_name,
        secrets_dir=Path(args.secrets_dir),
        config_dest=Path(args.config_dest),
        data_dir=Path(args.data_dir),
        fix_data_permissions=not args.no_fix_permissions,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
