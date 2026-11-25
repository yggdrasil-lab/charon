"""Integration tests for charon_runtime.git_backup using real bare Git repos."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
if (_REPO_ROOT / "charon_runtime").is_dir() and str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from charon_runtime.git_backup import (
    GitBackupConfig,
    GitBackupEngine,
    load_git_targets,
    main as git_backup_main,
)
from charon_runtime.notify import IrisAlert


class _RecordingNotifier:
    def __init__(self) -> None:
        self.alerts: list[IrisAlert] = []

    def send(self, alert: IrisAlert) -> bool:
        self.alerts.append(alert)
        return True


def _run_git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = [
        "git",
        "-C",
        str(cwd),
        "-c",
        "user.name=Alex Tan",
        "-c",
        "user.email=alex.tan@example.invalid",
        "-c",
        "protocol.file.allow=always",
        *args,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _init_bare_with_seed(root: Path, name: str = "remote.git") -> Path:
    bare = root / name
    bare.mkdir(parents=True, exist_ok=True)
    _run_git(bare, "init", "--bare", "-b", "main")

    seed = root / f"{name}_seed"
    _run_git(root, "clone", str(bare), str(seed))
    _run_git(seed, "checkout", "-B", "main")
    (seed / "README.md").write_text("initial\n", encoding="utf-8")
    _run_git(seed, "add", "README.md")
    _run_git(seed, "commit", "-m", "initial commit")
    _run_git(seed, "push", "-u", "origin", "main")
    return bare


def _clone_repo(bare: Path, dest: Path) -> Path:
    _run_git(bare.parent, "clone", str(bare), str(dest))
    _run_git(dest, "checkout", "main")
    return dest


def _write_targets(
    path: Path,
    entries: list[dict[str, object]],
) -> Path:
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


class GitBackupEngineTests(unittest.TestCase):
    """End-to-end verification of GitBackupEngine behavior and safety boundaries."""

    def test_load_git_targets_resolves_env_without_eval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            targets_file = _write_targets(
                Path(tmp) / "targets.json",
                [
                    {
                        "name": "vault",
                        "path": "/tmp/vault",
                        "repo_url_env": "TEST_REPO_URL",
                        "branch": "main",
                        "commit_prefix": "Daily: ",
                        "resolve_submodules": True,
                        "clean_ignored": True,
                    }
                ],
            )
            targets = load_git_targets(
                targets_file,
                {"TEST_REPO_URL": "$(touch /tmp/should_not_execute)"},
            )
            self.assertEqual(len(targets), 1)
            self.assertEqual(targets[0].repo_url, "$(touch /tmp/should_not_execute)")
            self.assertTrue(targets[0].resolve_submodules)
            self.assertTrue(targets[0].clean_ignored)

    def test_happy_path_commit_push_env_prefix_and_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare = _init_bare_with_seed(root)
            local = _clone_repo(bare, root / "local")
            (local / "note.md").write_text("hello world\n", encoding="utf-8")

            targets_file = _write_targets(
                root / "targets.json",
                [
                    {
                        "name": "vault",
                        "path": str(local),
                        "repo_url_env": "VAULT_URL",
                        "branch": "main",
                        "commit_prefix": "Daily Vault Backup: ",
                        "resolve_submodules": False,
                        "clean_ignored": False,
                    }
                ],
            )
            heartbeat = root / "hb" / "charon_git_heartbeat"
            notifier = _RecordingNotifier()
            config = GitBackupConfig(
                targets_path=targets_file,
                user_name="Alex Tan",
                user_email="alex.tan@example.invalid",
                env_name="PROD",
                home_dir=root / "home",
                ssh_dir=root / "ssh",
                heartbeat_path=heartbeat,
            )

            report = GitBackupEngine(config, notifier, env={"VAULT_URL": str(bare)}).sync_all()

            self.assertEqual(report.total, 1)
            self.assertEqual(report.failed, 0)
            self.assertTrue(report.results[0].ok)
            self.assertTrue(heartbeat.is_file())
            self.assertEqual(notifier.alerts, [])

            verify = _clone_repo(bare, root / "verify")
            self.assertEqual((verify / "note.md").read_text(encoding="utf-8"), "hello world\n")
            last_msg = _run_git(verify, "log", "-1", "--pretty=%s").stdout.strip()
            self.assertTrue(last_msg.startswith("[PROD] Daily Vault Backup: "))
            shared = _run_git(local, "config", "--get", "core.sharedRepository").stdout.strip()
            self.assertEqual(shared, "all")

    def test_hostile_home_and_ssh_env_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare = _init_bare_with_seed(root)
            local = _clone_repo(bare, root / "local")
            (local / "isolated.txt").write_text("safe\n", encoding="utf-8")

            home_dir = root / "isolated_home"
            ssh_dir = home_dir / ".ssh"
            ssh_dir.mkdir(parents=True, exist_ok=True)
            (ssh_dir / "id_rsa").write_text("dummy-key\n", encoding="utf-8")

            targets_file = _write_targets(
                root / "targets.json",
                [{"name": "vault", "path": str(local), "repo_url_env": "VAULT_URL"}],
            )
            config = GitBackupConfig(
                targets_path=targets_file,
                user_name="Alex Tan",
                user_email="alex.tan@example.invalid",
                home_dir=home_dir,
                ssh_dir=ssh_dir,
                heartbeat_path=root / "hb",
            )
            notifier = _RecordingNotifier()
            hostile_env = {"HOME": "/root", "PWD": "/root", "VAULT_URL": str(bare)}
            engine = GitBackupEngine(config, notifier, env=hostile_env)

            seen_envs: list[dict[str, str]] = []
            seen_cwds: list[str] = []
            real_run = subprocess.run

            def spy_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                seen_envs.append(dict(kwargs["env"]))  # type: ignore[arg-type]
                seen_cwds.append(str(kwargs["cwd"]))
                return real_run(cmd, **kwargs)  # type: ignore[arg-type]

            with patch("charon_runtime.git_backup.subprocess.run", side_effect=spy_run):
                report = engine.sync_all()

            self.assertEqual(report.failed, 0)
            self.assertGreater(len(seen_envs), 0)
            for env_used in seen_envs:
                self.assertEqual(env_used["HOME"], str(home_dir))
                self.assertEqual(env_used["GIT_TERMINAL_PROMPT"], "0")
                self.assertEqual(env_used["GIT_EDITOR"], "true")
                self.assertIn(str(ssh_dir / "id_rsa"), env_used["GIT_SSH_COMMAND"])
                self.assertIn("BatchMode=yes", env_used["GIT_SSH_COMMAND"])
            for cwd_used in seen_cwds:
                self.assertEqual(cwd_used, str(home_dir))

    def test_missing_dir_and_unreachable_remote_fail_loud_and_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = root / "existing"
            local.mkdir()
            _run_git(local, "init", "-b", "main")
            (local / "f.txt").write_text("x\n", encoding="utf-8")
            _run_git(local, "add", "f.txt")
            _run_git(local, "commit", "-m", "local init")

            targets_file = _write_targets(
                root / "targets.json",
                [
                    {"name": "missing", "path": str(root / "nope"), "repo_url_env": "URL_A"},
                    {"name": "unreachable", "path": str(local), "repo_url_env": "URL_B"},
                ],
            )
            heartbeat = root / "hb"
            notifier = _RecordingNotifier()
            config = GitBackupConfig(
                targets_path=targets_file,
                user_name="Alex Tan",
                user_email="alex.tan@example.invalid",
                home_dir=root / "home",
                ssh_dir=root / "ssh",
                heartbeat_path=heartbeat,
            )
            env = {"URL_A": str(root / "a.git"), "URL_B": str(root / "nonexistent.git")}
            report = GitBackupEngine(config, notifier, env=env).sync_all()

            self.assertEqual(report.total, 2)
            self.assertEqual(report.failed, 2)
            self.assertTrue(heartbeat.is_file())
            self.assertEqual(len(notifier.alerts), 1)
            self.assertEqual(notifier.alerts[0].service, "git-backup")
            self.assertEqual(notifier.alerts[0].severity, "error")
            self.assertIn("missing", notifier.alerts[0].details)
            self.assertIn("unreachable", notifier.alerts[0].details)

    def test_diverged_clean_rebase_merges_both_sides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare = _init_bare_with_seed(root)
            peer = _clone_repo(bare, root / "peer")
            local = _clone_repo(bare, root / "local")

            (peer / "remote_only.txt").write_text("from remote\n", encoding="utf-8")
            _run_git(peer, "add", "remote_only.txt")
            _run_git(peer, "commit", "-m", "remote change")
            _run_git(peer, "push", "origin", "main")

            (local / "local_only.txt").write_text("from local\n", encoding="utf-8")
            _run_git(local, "add", "local_only.txt")
            _run_git(local, "commit", "-m", "local change")

            targets_file = _write_targets(
                root / "targets.json",
                [{"name": "vault", "path": str(local), "repo_url_env": "VAULT_URL"}],
            )
            config = GitBackupConfig(
                targets_path=targets_file,
                user_name="Alex Tan",
                user_email="alex.tan@example.invalid",
                home_dir=root / "home",
                ssh_dir=root / "ssh",
                heartbeat_path=root / "hb",
            )
            notifier = _RecordingNotifier()
            report = GitBackupEngine(config, notifier, env={"VAULT_URL": str(bare)}).sync_all()

            self.assertEqual(report.failed, 0)
            self.assertEqual(notifier.alerts, [])
            verify = _clone_repo(bare, root / "verify")
            self.assertEqual((verify / "remote_only.txt").read_text(encoding="utf-8"), "from remote\n")
            self.assertEqual((verify / "local_only.txt").read_text(encoding="utf-8"), "from local\n")

    def test_diverged_conflict_prefers_local_and_resolves_submodule_160000(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sub_bare = _init_bare_with_seed(root, "sub.git")
            sub_peer = _clone_repo(sub_bare, root / "sub_peer")

            bare = _init_bare_with_seed(root, "main.git")
            setup = _clone_repo(bare, root / "setup")
            _run_git(setup, "submodule", "add", str(sub_bare), "Forge/sub")
            (setup / "shared.txt").write_text("base\n", encoding="utf-8")
            _run_git(setup, "add", "shared.txt", ".gitmodules", "Forge/sub")
            _run_git(setup, "commit", "-m", "add submodule and shared file")
            _run_git(setup, "push", "origin", "main")

            # Advance submodule in two divergent commits
            (sub_peer / "sub.txt").write_text("sub_remote\n", encoding="utf-8")
            _run_git(sub_peer, "add", "sub.txt")
            _run_git(sub_peer, "commit", "-m", "sub remote")
            _run_git(sub_peer, "push", "origin", "main")
            sub_sha_remote = _run_git(sub_peer, "rev-parse", "HEAD").stdout.strip()

            sub_peer2 = _clone_repo(sub_bare, root / "sub_peer2")
            _run_git(sub_peer2, "reset", "--hard", "HEAD~1")
            (sub_peer2 / "sub_local.txt").write_text("sub_local\n", encoding="utf-8")
            _run_git(sub_peer2, "add", "sub_local.txt")
            _run_git(sub_peer2, "commit", "-m", "sub local")
            sub_sha_local = _run_git(sub_peer2, "rev-parse", "HEAD").stdout.strip()

            peer = _clone_repo(bare, root / "peer")
            _run_git(peer, "submodule", "update", "--init", "--recursive")
            (peer / "shared.txt").write_text("remote edit\n", encoding="utf-8")
            _run_git(peer, "update-index", "--cacheinfo", f"160000,{sub_sha_remote},Forge/sub")
            _run_git(peer, "add", "shared.txt")
            _run_git(peer, "commit", "-m", "peer divergent")
            _run_git(peer, "push", "origin", "main")

            local = _clone_repo(bare, root / "local")
            _run_git(local, "submodule", "update", "--init", "--recursive")
            _run_git(local / "Forge" / "sub", "fetch", str(sub_peer2))
            _run_git(local / "Forge" / "sub", "checkout", sub_sha_local)
            (local / "shared.txt").write_text("local edit\n", encoding="utf-8")
            _run_git(local, "add", "shared.txt", "Forge/sub")
            _run_git(local, "commit", "-m", "local divergent")

            targets_file = _write_targets(
                root / "targets.json",
                [
                    {
                        "name": "vault",
                        "path": str(local),
                        "repo_url_env": "VAULT_URL",
                        "resolve_submodules": True,
                    }
                ],
            )
            config = GitBackupConfig(
                targets_path=targets_file,
                user_name="Alex Tan",
                user_email="alex.tan@example.invalid",
                home_dir=root / "home",
                ssh_dir=root / "ssh",
                heartbeat_path=root / "hb",
            )
            notifier = _RecordingNotifier()
            report = GitBackupEngine(config, notifier, env={"VAULT_URL": str(bare)}).sync_all()

            self.assertEqual(report.failed, 0, msg=str(report.results))
            self.assertEqual(notifier.alerts, [])
            verify = _clone_repo(bare, root / "verify")
            self.assertEqual((verify / "shared.txt").read_text(encoding="utf-8"), "local edit\n")
            ls_tree = _run_git(verify, "ls-tree", "HEAD", "Forge/sub").stdout.strip()
            self.assertIn(sub_sha_local, ls_tree)

    def test_unresolvable_rebase_aborts_and_rolls_back_pre_rebase_save(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare = _init_bare_with_seed(root)
            peer = _clone_repo(bare, root / "peer")
            local = _clone_repo(bare, root / "local")

            # Peer commits a directory at "entry/nested.txt"
            (peer / "entry").mkdir()
            (peer / "entry" / "nested.txt").write_text("remote dir\n", encoding="utf-8")
            _run_git(peer, "add", "entry/nested.txt")
            _run_git(peer, "commit", "-m", "remote directory")
            _run_git(peer, "push", "origin", "main")

            # Local commits a regular file at "entry" (file/directory conflict)
            (local / "entry").write_text("local file\n", encoding="utf-8")
            _run_git(local, "add", "entry")
            _run_git(local, "commit", "-m", "local conflict")

            targets_file = _write_targets(
                root / "targets.json",
                [{"name": "vault", "path": str(local), "repo_url_env": "VAULT_URL"}],
            )
            config = GitBackupConfig(
                targets_path=targets_file,
                user_name="Alex Tan",
                user_email="alex.tan@example.invalid",
                home_dir=root / "home",
                ssh_dir=root / "ssh",
                heartbeat_path=root / "hb",
            )
            notifier = _RecordingNotifier()
            real_run = subprocess.run
            saw_continue = False

            def fail_continue(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                nonlocal saw_continue
                if "rebase" in cmd and "--continue" in cmd:
                    saw_continue = True
                    return subprocess.CompletedProcess(cmd, 1, "", "unresolvable conflict")
                return real_run(cmd, **kwargs)  # type: ignore[arg-type]

            with patch("charon_runtime.git_backup.subprocess.run", side_effect=fail_continue):
                report = GitBackupEngine(config, notifier, env={"VAULT_URL": str(bare)}).sync_all()

            self.assertTrue(saw_continue)
            self.assertEqual(report.failed, 1)
            self.assertFalse(report.results[0].ok)
            self.assertFalse((local / ".git" / "rebase-merge").exists())
            last_msg = _run_git(local, "log", "-1", "--pretty=%s").stdout.strip()
            self.assertEqual(last_msg, "local conflict")
            self.assertEqual(len(notifier.alerts), 1)

    def test_clean_ignored_untracks_newly_ignored_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare = _init_bare_with_seed(root)
            local = _clone_repo(bare, root / "local")
            (local / "secret.log").write_text("token\n", encoding="utf-8")
            _run_git(local, "add", "secret.log")
            _run_git(local, "commit", "-m", "track log")
            _run_git(local, "push", "origin", "main")

            (local / ".gitignore").write_text("*.log\n", encoding="utf-8")
            targets_file = _write_targets(
                root / "targets.json",
                [
                    {
                        "name": "vault",
                        "path": str(local),
                        "repo_url_env": "VAULT_URL",
                        "clean_ignored": True,
                    }
                ],
            )
            config = GitBackupConfig(
                targets_path=targets_file,
                user_name="Alex Tan",
                user_email="alex.tan@example.invalid",
                home_dir=root / "home",
                ssh_dir=root / "ssh",
                heartbeat_path=root / "hb",
            )
            notifier = _RecordingNotifier()
            report = GitBackupEngine(config, notifier, env={"VAULT_URL": str(bare)}).sync_all()

            self.assertEqual(report.failed, 0)
            tracked = _run_git(local, "ls-files", "secret.log").stdout.strip()
            self.assertEqual(tracked, "")
            self.assertTrue((local / "secret.log").is_file())

    def test_non_ff_push_rejection_fails_loud_without_force_push(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare = _init_bare_with_seed(root)
            local = _clone_repo(bare, root / "local")
            (local / "change.txt").write_text("local edit\n", encoding="utf-8")

            hook = bare / "hooks" / "pre-receive"
            hook.write_text("#!/bin/sh\necho 'protected branch' >&2\nexit 1\n", encoding="utf-8")
            hook.chmod(hook.stat().st_mode | stat.S_IXUSR)

            targets_file = _write_targets(
                root / "targets.json",
                [{"name": "vault", "path": str(local), "repo_url_env": "VAULT_URL"}],
            )
            config = GitBackupConfig(
                targets_path=targets_file,
                user_name="Alex Tan",
                user_email="alex.tan@example.invalid",
                home_dir=root / "home",
                ssh_dir=root / "ssh",
                heartbeat_path=root / "hb",
            )
            notifier = _RecordingNotifier()
            recorded_cmds: list[list[str]] = []
            real_run = subprocess.run

            def spy_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                recorded_cmds.append(list(cmd))
                return real_run(cmd, **kwargs)  # type: ignore[arg-type]

            with patch("charon_runtime.git_backup.subprocess.run", side_effect=spy_run):
                report = GitBackupEngine(config, notifier, env={"VAULT_URL": str(bare)}).sync_all()

            self.assertEqual(report.failed, 1)
            self.assertFalse(report.results[0].ok)
            self.assertEqual(len(notifier.alerts), 1)
            for cmd in recorded_cmds:
                self.assertNotIn("--force", cmd)
                self.assertNotIn("-f", cmd)
                self.assertNotIn("--soft", cmd)

    def test_cli_main_validation_and_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare = _init_bare_with_seed(root)
            local = _clone_repo(bare, root / "local")
            (local / "cli.txt").write_text("from cli\n", encoding="utf-8")
            targets_file = _write_targets(
                root / "targets.json",
                [{"name": "vault", "path": str(local), "repo_url_env": "VAULT_URL"}],
            )

            with patch.dict(os.environ, {}, clear=True):
                stderr_buf = io.StringIO()
                with redirect_stderr(stderr_buf):
                    self.assertEqual(git_backup_main(), 1)
                self.assertIn("GIT_USER_EMAIL", stderr_buf.getvalue())

            with patch.dict(
                os.environ,
                {
                    "GIT_USER_EMAIL": "alex.tan@example.invalid",
                    "GIT_USER_NAME": "Alex Tan",
                    "TARGETS_CONFIG": str(root / "missing.json"),
                },
                clear=True,
            ):
                stderr_buf = io.StringIO()
                with redirect_stderr(stderr_buf):
                    self.assertEqual(git_backup_main(), 1)
                self.assertIn("not found", stderr_buf.getvalue())

            heartbeat = root / "cli_hb"
            with patch.dict(
                os.environ,
                {
                    "GIT_USER_EMAIL": "alex.tan@example.invalid",
                    "GIT_USER_NAME": "Alex Tan",
                    "TARGETS_CONFIG": str(targets_file),
                    "VAULT_URL": str(bare),
                    "HOME": str(root / "home"),
                    "HEARTBEAT_PATH": str(heartbeat),
                },
                clear=True,
            ):
                stdout_buf = io.StringIO()
                with redirect_stdout(stdout_buf):
                    self.assertEqual(git_backup_main(), 0)
                self.assertIn("completed successfully", stdout_buf.getvalue())
                self.assertTrue(heartbeat.is_file())


if __name__ == "__main__":
    unittest.main()
