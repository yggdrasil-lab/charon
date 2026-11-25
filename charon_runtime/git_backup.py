"""Centralized Git backup engine, target loader, and CLI entrypoint."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from charon_runtime.heartbeat import touch_heartbeat
from charon_runtime.notify import DEFAULT_IRIS_URL, HttpIrisNotifier, IrisAlert, IrisNotifier

DEFAULT_TARGETS_PATH = Path("/etc/charon-git/targets.json")
_STRIPPED_GIT_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


class GitBackupError(RuntimeError):
    """Raised when a Git backup step fails and must be reported loudly."""


@dataclass(frozen=True)
class GitBackupTarget:
    """Single repository target configured for Charon Git backup."""

    name: str
    path: Path
    repo_url: str
    branch: str
    commit_prefix: str
    resolve_submodules: bool
    clean_ignored: bool


@dataclass(frozen=True)
class GitBackupConfig:
    """Runtime configuration for GitBackupEngine."""

    targets_path: Path
    user_name: str
    user_email: str
    env_name: str = ""
    home_dir: Path = Path("/tmp")
    ssh_dir: Path = Path("/tmp/.ssh")
    heartbeat_path: Path = Path("/tmp/charon_git_heartbeat")


@dataclass(frozen=True)
class TargetSyncResult:
    """Outcome of syncing a single Git backup target."""

    name: str
    ok: bool
    message: str


@dataclass(frozen=True)
class BackupRunReport:
    """Aggregate report across all configured Git backup targets."""

    total: int
    failed: int
    results: tuple[TargetSyncResult, ...]


def _is_local_remote(repo_url: str) -> bool:
    return repo_url.startswith("/") or repo_url.startswith("file://")


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def load_git_targets(
    targets_path: Path,
    env: Mapping[str, str],
) -> tuple[GitBackupTarget, ...]:
    """Load backup targets from JSON and resolve repo_url_env from env without eval."""
    raw_data = json.loads(targets_path.read_text(encoding="utf-8"))
    if not isinstance(raw_data, list):
        raise ValueError(f"Expected JSON array in {targets_path}")

    targets: list[GitBackupTarget] = []
    for entry in raw_data:
        if not isinstance(entry, dict):
            raise ValueError(f"Expected target object in {targets_path}")
        repo_url_env = str(entry["repo_url_env"])
        prefix = entry.get("commit_prefix")
        targets.append(
            GitBackupTarget(
                name=str(entry["name"]),
                path=Path(str(entry["path"])),
                repo_url=env.get(repo_url_env, ""),
                branch=str(entry.get("branch") or "main"),
                commit_prefix=str(prefix if prefix is not None else "Daily Backup: "),
                resolve_submodules=bool(entry.get("resolve_submodules", False)),
                clean_ignored=bool(entry.get("clean_ignored", False)),
            )
        )
    return tuple(targets)


class GitBackupEngine:
    """Deep module encapsulating isolated Git sync, conflict recovery, and alerting."""

    def __init__(
        self,
        config: GitBackupConfig,
        notifier: IrisNotifier,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        self._notifier = notifier
        self._env = os.environ if env is None else env

    def sync_all(self) -> BackupRunReport:
        """Run backup across all targets, touch heartbeat, and alert Iris on failures."""
        os.umask(0o002)
        self._config.home_dir.mkdir(parents=True, exist_ok=True)
        targets = load_git_targets(self._config.targets_path, self._env)
        results: list[TargetSyncResult] = []
        try:
            for target in targets:
                results.append(self._sync_target(target))
        finally:
            touch_heartbeat(self._config.heartbeat_path)

        failed_results = [result for result in results if not result.ok]
        if failed_results:
            details = " ".join(
                f"Target '{result.name}' failed backup: {result.message}"
                for result in failed_results
            )
            self._notifier.send(
                IrisAlert(
                    service="git-backup",
                    severity="error",
                    title="Git Backup Failed",
                    details=details,
                )
            )
        return BackupRunReport(
            total=len(results),
            failed=len(failed_results),
            results=tuple(results),
        )

    def _build_env(self, repo_url: str) -> dict[str, str]:
        isolated = dict(self._env)
        for key in _STRIPPED_GIT_ENV_VARS:
            isolated.pop(key, None)
        isolated["HOME"] = str(self._config.home_dir)
        isolated["GIT_TERMINAL_PROMPT"] = "0"
        isolated["GIT_EDITOR"] = "true"
        ssh_key = self._config.ssh_dir / "id_rsa"
        if ssh_key.exists() or (repo_url and not _is_local_remote(repo_url)):
            isolated["GIT_SSH_COMMAND"] = (
                f"ssh -i {ssh_key}"
                f" -o UserKnownHostsFile={self._config.ssh_dir}/known_hosts"
                " -o StrictHostKeyChecking=accept-new"
                " -o BatchMode=yes"
            )
        return isolated

    def _run_git(
        self,
        target_path: Path,
        args: Sequence[str],
        repo_url: str = "",
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [
            "git",
            "-C",
            str(target_path),
            "-c",
            f"user.name={self._config.user_name}",
            "-c",
            f"user.email={self._config.user_email}",
            "-c",
            "safe.directory=*",
            *args,
        ]
        return subprocess.run(
            cmd,
            env=self._build_env(repo_url),
            cwd=str(self._config.home_dir),
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
        )

    def _run_git_checked(
        self,
        target_path: Path,
        args: Sequence[str],
        context: str,
        repo_url: str = "",
    ) -> subprocess.CompletedProcess[str]:
        result = self._run_git(target_path, args, repo_url=repo_url)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise GitBackupError(f"{context}: {detail}")
        return result

    def _sync_target(self, target: GitBackupTarget) -> TargetSyncResult:
        if not target.path.is_dir():
            return TargetSyncResult(target.name, False, f"Directory '{target.path}' not found.")
        if not target.repo_url:
            return TargetSyncResult(target.name, False, "Remote repository URL is empty.")
        try:
            self._ensure_repo_and_branch(target)
            self._pull_and_reconcile(target)
            self._clean_ignored_files(target)
            self._commit_if_dirty(target)
            self._push_with_rebase(target)
            return TargetSyncResult(target.name, True, "Synced.")
        except (GitBackupError, OSError, subprocess.SubprocessError) as exc:
            return TargetSyncResult(target.name, False, str(exc))

    def _ensure_repo_and_branch(self, target: GitBackupTarget) -> None:
        if not (target.path / ".git").is_dir():
            self._run_git_checked(target.path, ["init"], "git init", target.repo_url)
        remote_check = self._run_git(target.path, ["remote", "get-url", "origin"], target.repo_url)
        remote_cmd = "set-url" if remote_check.returncode == 0 else "add"
        self._run_git_checked(
            target.path,
            ["remote", remote_cmd, "origin", target.repo_url],
            f"configure origin for {target.name}",
            target.repo_url,
        )
        self._run_git_checked(
            target.path,
            ["config", "core.sharedRepository", "all"],
            f"set sharedRepository for {target.name}",
            target.repo_url,
        )
        head_res = self._run_git(target.path, ["rev-parse", "--abbrev-ref", "HEAD"], target.repo_url)
        current_branch = head_res.stdout.strip() if head_res.returncode == 0 else ""
        if current_branch != target.branch:
            self._switch_branch(target)

    def _switch_branch(self, target: GitBackupTarget) -> None:
        ref = f"refs/heads/{target.branch}"
        has_local = self._run_git(target.path, ["show-ref", "--verify", "--quiet", ref]).returncode == 0
        if has_local:
            self._run_git_checked(target.path, ["checkout", target.branch], "checkout branch", target.repo_url)
            return
        track_res = self._run_git(
            target.path,
            ["checkout", "-b", target.branch, f"origin/{target.branch}"],
            target.repo_url,
        )
        if track_res.returncode != 0:
            self._run_git_checked(target.path, ["checkout", "-b", target.branch], "create branch", target.repo_url)

    def _pull_and_reconcile(self, target: GitBackupTarget) -> None:
        pull_res = self._run_git(target.path, ["pull", "--ff-only", "origin", target.branch], target.repo_url)
        if pull_res.returncode == 0:
            return
        self._run_git_checked(
            target.path,
            ["fetch", "origin", target.branch],
            f"fetch origin/{target.branch}",
            target.repo_url,
        )
        ff_res = self._run_git(target.path, ["merge", "--ff-only", f"origin/{target.branch}"], target.repo_url)
        if ff_res.returncode == 0:
            return
        self._run_git_checked(target.path, ["add", "."], "stage pre-rebase save", target.repo_url)
        self._run_git_checked(
            target.path,
            ["commit", "--allow-empty", "-m", f"Pre-rebase save: {_utc_timestamp()}"],
            "create pre-rebase save commit",
            target.repo_url,
        )
        self._abort_stale_rebase(target)
        rebase_res = self._run_git(
            target.path,
            ["pull", "--rebase", "-X", "theirs", "origin", target.branch],
            target.repo_url,
        )
        if rebase_res.returncode != 0:
            self._resolve_rebase_conflicts(target)

    def _abort_stale_rebase(self, target: GitBackupTarget) -> None:
        rebase_dir = target.path / ".git" / "rebase-merge"
        if not rebase_dir.exists():
            return
        abort_res = self._run_git(target.path, ["rebase", "--abort"], target.repo_url)
        if abort_res.returncode != 0:
            shutil.rmtree(rebase_dir, ignore_errors=True)

    def _resolve_rebase_conflicts(self, target: GitBackupTarget) -> None:
        self._run_git(target.path, ["checkout", "--theirs", "."], target.repo_url)
        if target.resolve_submodules:
            self._stage_unmerged_submodules(target)
        self._run_git(target.path, ["add", "-u"], target.repo_url)
        self._run_git(target.path, ["add", "."], target.repo_url)
        cont_res = self._run_git(target.path, ["rebase", "--continue"], target.repo_url)
        if cont_res.returncode == 0:
            return
        abort_res = self._run_git(target.path, ["rebase", "--abort"], target.repo_url)
        if abort_res.returncode != 0:
            shutil.rmtree(target.path / ".git" / "rebase-merge", ignore_errors=True)
        self._rollback_pre_rebase_save(target)
        detail = cont_res.stderr.strip() or cont_res.stdout.strip() or "rebase --continue failed"
        raise GitBackupError(f"Rebase conflict resolution failed for '{target.name}': {detail}")

    def _stage_unmerged_submodules(self, target: GitBackupTarget) -> None:
        ls_res = self._run_git(target.path, ["ls-files", "-u"], target.repo_url)
        if ls_res.returncode != 0:
            return
        subpaths = sorted(
            {
                line.split("\t", 1)[1]
                for line in ls_res.stdout.splitlines()
                if line.startswith("160000") and "\t" in line
            }
        )
        for subpath in subpaths:
            self._run_git(target.path, ["add", subpath], target.repo_url)

    def _rollback_pre_rebase_save(self, target: GitBackupTarget) -> None:
        log_res = self._run_git(target.path, ["log", "-1", "--pretty=%s"], target.repo_url)
        if log_res.returncode == 0 and log_res.stdout.strip().startswith("Pre-rebase save:"):
            self._run_git(target.path, ["reset", "HEAD~1"], target.repo_url)

    def _clean_ignored_files(self, target: GitBackupTarget) -> None:
        if not target.clean_ignored or not (target.path / ".gitignore").is_file():
            return
        ls_res = self._run_git(target.path, ["ls-files", "--cached"], target.repo_url)
        if ls_res.returncode != 0 or not ls_res.stdout.strip():
            return
        ignore_res = self._run_git(
            target.path,
            ["check-ignore", "--stdin", "--no-index"],
            repo_url=target.repo_url,
            input_text=ls_res.stdout,
        )
        if ignore_res.returncode != 0 or not ignore_res.stdout.strip():
            return
        ignored_files = [line for line in ignore_res.stdout.splitlines() if line]
        self._run_git_checked(
            target.path,
            ["rm", "--cached", "--", *ignored_files],
            f"untrack ignored files for {target.name}",
            target.repo_url,
        )

    def _commit_if_dirty(self, target: GitBackupTarget) -> None:
        self._run_git_checked(target.path, ["add", "."], f"stage changes for {target.name}", target.repo_url)
        diff_res = self._run_git(
            target.path,
            ["diff", "--staged", "--quiet", "--ignore-submodules=dirty"],
            target.repo_url,
        )
        if diff_res.returncode == 0:
            return
        if diff_res.returncode != 1:
            raise GitBackupError(f"git diff --staged failed for {target.name}: {diff_res.stderr.strip()}")
        prefix = f"[{self._config.env_name}] {target.commit_prefix}" if self._config.env_name else target.commit_prefix
        self._run_git_checked(
            target.path,
            ["commit", "-m", f"{prefix}{_utc_timestamp()}"],
            f"commit changes for {target.name}",
            target.repo_url,
        )

    def _push_with_rebase(self, target: GitBackupTarget) -> None:
        push_res = self._run_git(target.path, ["push", "origin", target.branch], target.repo_url)
        if push_res.returncode == 0:
            return
        self._run_git_checked(
            target.path,
            ["fetch", "origin", target.branch],
            f"fetch before push retry for {target.name}",
            target.repo_url,
        )
        rebase_res = self._run_git(target.path, ["pull", "--rebase", "origin", target.branch], target.repo_url)
        if rebase_res.returncode != 0:
            abort_res = self._run_git(target.path, ["rebase", "--abort"], target.repo_url)
            if abort_res.returncode != 0:
                shutil.rmtree(target.path / ".git" / "rebase-merge", ignore_errors=True)
            detail = rebase_res.stderr.strip() or rebase_res.stdout.strip() or "rebase failed"
            raise GitBackupError(f"Push rebase failed for '{target.name}': {detail}")
        self._run_git_checked(
            target.path,
            ["push", "origin", target.branch],
            f"push after rebase for {target.name}",
            target.repo_url,
        )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for centralized Charon Git backup."""
    del argv
    user_email = os.environ.get("GIT_USER_EMAIL", "").strip()
    user_name = os.environ.get("GIT_USER_NAME", "").strip()
    if not user_email or not user_name:
        print("Error: GIT_USER_EMAIL and GIT_USER_NAME must be set.", file=sys.stderr)
        return 1

    targets_path = Path(os.environ.get("TARGETS_CONFIG", str(DEFAULT_TARGETS_PATH)))
    if not targets_path.is_file():
        print(f"Error: Targets configuration file '{targets_path}' not found.", file=sys.stderr)
        return 1

    home_dir = Path(os.environ.get("HOME", "/tmp") or "/tmp")
    config = GitBackupConfig(
        targets_path=targets_path,
        user_name=user_name,
        user_email=user_email,
        env_name=os.environ.get("ENV", "").strip(),
        home_dir=home_dir,
        ssh_dir=home_dir / ".ssh",
        heartbeat_path=Path(os.environ.get("HEARTBEAT_PATH", "/tmp/charon_git_heartbeat")),
    )
    notifier = HttpIrisNotifier(url=os.environ.get("IRIS_URL", DEFAULT_IRIS_URL))
    report = GitBackupEngine(config=config, notifier=notifier).sync_all()
    if report.failed > 0:
        print(f"Git backup completed with {report.failed}/{report.total} failures.", file=sys.stderr)
        return 1
    print(f"All {report.total} git backup targets completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
