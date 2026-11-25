"""Deterministic unit tests for charon_runtime.bisync_recovery."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if (_REPO_ROOT / "charon_runtime").is_dir() and str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import charon_runtime
from charon_runtime.bisync_recovery import (
    BISYNC_COMMON_FLAGS,
    BisyncConfig,
    BisyncRecoveryEngine,
    CacheState,
    classify_cache,
    clear_directory_contents,
    main as bisync_main,
    prepare_rclone_environment,
)
from charon_runtime.notify import IrisAlert


class _FakeIrisNotifier:
    def __init__(self) -> None:
        self.alerts: list[IrisAlert] = []

    def send(self, alert: IrisAlert) -> bool:
        self.alerts.append(alert)
        return True


class _ScriptedRcloneRunner:
    def __init__(
        self,
        handlers: Sequence[Callable[[Sequence[str], Mapping[str, str]], int]],
    ) -> None:
        self._handlers = list(handlers)
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def run_bisync(self, args: Sequence[str], env: Mapping[str, str]) -> int:
        self.calls.append((list(args), dict(env)))
        if not self._handlers:
            raise AssertionError("Unexpected extra rclone bisync invocation")
        handler = self._handlers.pop(0)
        return handler(args, env)


class BisyncRecoveryTests(unittest.TestCase):
    """Verify cache recovery, environment prep, flags, grace window, and shutdown."""

    def setUp(self) -> None:
        self._saved_rclone_config = os.environ.get("RCLONE_CONFIG")
        self._saved_secret_name = os.environ.get("RCLONE_CONFIG_SECRET_NAME")
        self._saved_home = os.environ.get("HOME")
        self._saved_cache_dir = os.environ.get("RCLONE_CACHE_DIR")
        self._saved_vault_path = os.environ.get("GDRIVE_VAULT_PATH")

    def tearDown(self) -> None:
        self._restore_env("RCLONE_CONFIG", self._saved_rclone_config)
        self._restore_env("RCLONE_CONFIG_SECRET_NAME", self._saved_secret_name)
        self._restore_env("HOME", self._saved_home)
        self._restore_env("RCLONE_CACHE_DIR", self._saved_cache_dir)
        self._restore_env("GDRIVE_VAULT_PATH", self._saved_vault_path)

    @staticmethod
    def _restore_env(key: str, value: str | None) -> None:
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    @staticmethod
    def _make_config(root: Path, *, sync_interval: int = 300) -> BisyncConfig:
        return BisyncConfig(
            gdrive_vault_path="Obsidian/Vault",
            data_dir=root / "data",
            cache_root=root / "cache",
            sync_interval=sync_interval,
            secret_name="charon_rclone_config",
            secrets_dir=root / "secrets",
            rclone_config_dest=root / "tmp" / "rclone.conf",
            heartbeat_path=root / "tmp" / "charon_sync_heartbeat",
        )

    def test_lazy_package_exports(self) -> None:
        self.assertIs(charon_runtime.BisyncRecoveryEngine, BisyncRecoveryEngine)
        self.assertIs(charon_runtime.BisyncConfig, BisyncConfig)
        self.assertIs(charon_runtime.CacheState, CacheState)
        self.assertEqual(charon_runtime.BISYNC_COMMON_FLAGS, BISYNC_COMMON_FLAGS)

    def test_prepare_rclone_environment_copies_secret_and_fixes_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secrets_dir = root / "secrets"
            secrets_dir.mkdir()
            secret_file = secrets_dir / "charon_rclone_config"
            secret_file.write_text("[gdrive]\ntype = drive\n", encoding="utf-8")

            config_dest = root / "out" / "rclone.conf"
            data_dir = root / "data"
            nested_dir = data_dir / "folder_646"
            deep_dir = nested_dir / "child_646"
            deep_dir.mkdir(parents=True)
            os.chmod(deep_dir, 0o646)
            os.chmod(nested_dir, 0o646)
            os.chmod(data_dir, 0o646)

            try:
                stdout_buf = io.StringIO()
                with redirect_stdout(stdout_buf):
                    staged = prepare_rclone_environment(
                        secret_name="charon_rclone_config",
                        secrets_dir=secrets_dir,
                        config_dest=config_dest,
                        data_dir=data_dir,
                    )

                self.assertEqual(staged, config_dest)
                self.assertEqual(os.environ.get("RCLONE_CONFIG"), str(config_dest))
                self.assertEqual(config_dest.read_text(encoding="utf-8"), "[gdrive]\ntype = drive\n")
                self.assertEqual(stat.S_IMODE(config_dest.stat().st_mode), 0o600)
                for directory in (data_dir, nested_dir, deep_dir):
                    self.assertTrue(stat.S_IMODE(directory.stat().st_mode) & stat.S_IXUSR)
            finally:
                for directory in (data_dir, nested_dir, deep_dir):
                    if directory.exists():
                        os.chmod(directory, 0o755)

    def test_prepare_rclone_environment_missing_secret_warns_and_preserves_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._make_config(root)
            cfg.bisync_cache.mkdir(parents=True)
            lst_file = cfg.bisync_cache / "vault.lst"
            lst_file.write_text("ok", encoding="utf-8")

            stderr_buf = io.StringIO()
            stdout_buf = io.StringIO()
            with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                staged = prepare_rclone_environment(
                    secret_name="missing_secret",
                    secrets_dir=cfg.secrets_dir,
                    config_dest=cfg.rclone_config_dest,
                    data_dir=cfg.data_dir,
                )

            self.assertIsNone(staged)
            self.assertIn("not found", stderr_buf.getvalue())
            self.assertTrue(lst_file.is_file())

    def test_startup_cache_validation_and_maxdepth_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._make_config(root)
            notifier = _FakeIrisNotifier()
            engine = BisyncRecoveryEngine(cfg, notifier, runner=_ScriptedRcloneRunner([]))

            # MISSING_DIR
            self.assertEqual(engine.validate_startup_cache(), CacheState.MISSING_DIR)

            # INCOMPLETE: nested .lst in subdir must NOT count as maxdepth=1 .lst
            nested = cfg.bisync_cache / "subdir"
            nested.mkdir(parents=True)
            (nested / "ignored.lst").write_text("nested", encoding="utf-8")
            self.assertEqual(classify_cache(cfg.bisync_cache), CacheState.INCOMPLETE)
            self.assertEqual(engine.validate_startup_cache(), CacheState.INCOMPLETE)
            self.assertTrue(cfg.cache_root.is_dir())
            self.assertEqual(list(cfg.cache_root.iterdir()), [])

            # RECOVERABLE_OLD must NOT be wiped
            cfg.bisync_cache.mkdir(parents=True)
            old_file = cfg.bisync_cache / "vault.lst-old"
            old_file.write_text("old", encoding="utf-8")
            self.assertEqual(engine.validate_startup_cache(), CacheState.RECOVERABLE_OLD)
            self.assertTrue(old_file.is_file())

            # HEALTHY_LST kept
            lst_file = cfg.bisync_cache / "vault.lst"
            lst_file.write_text("lst", encoding="utf-8")
            self.assertEqual(engine.validate_startup_cache(), CacheState.HEALTHY_LST)
            self.assertTrue(lst_file.is_file())

    def test_flag_contract_and_env_preservation_on_resync_and_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._make_config(root)
            cfg.data_dir.mkdir(parents=True)
            os.environ["HOME"] = str(cfg.cache_root)
            os.environ["RCLONE_CACHE_DIR"] = str(cfg.cache_root)
            os.environ.pop("RCLONE_CONFIG", None)

            def do_resync(args: Sequence[str], env: Mapping[str, str]) -> int:
                del args, env
                cfg.bisync_cache.mkdir(parents=True, exist_ok=True)
                (cfg.bisync_cache / "vault.lst").write_text("ok", encoding="utf-8")
                return 0

            def do_sync(args: Sequence[str], env: Mapping[str, str]) -> int:
                del args, env
                return 0

            runner = _ScriptedRcloneRunner([do_resync, do_sync])
            notifier = _FakeIrisNotifier()
            engine = BisyncRecoveryEngine(cfg, notifier, runner=runner)

            resync_outcome = engine.run_once()
            sync_outcome = engine.run_once()

            self.assertEqual(resync_outcome.mode, "resync")
            self.assertEqual(resync_outcome.status, "initial_resync_ok")
            self.assertEqual(sync_outcome.mode, "sync")
            self.assertEqual(sync_outcome.status, "sync_ok")
            self.assertEqual(len(runner.calls), 2)

            resync_args, resync_env = runner.calls[0]
            sync_args, sync_env = runner.calls[1]
            self.assertEqual(
                resync_args,
                ["gdrive:Obsidian/Vault", str(cfg.data_dir), "--resync", *BISYNC_COMMON_FLAGS],
            )
            self.assertEqual(
                sync_args,
                ["gdrive:Obsidian/Vault", str(cfg.data_dir), *BISYNC_COMMON_FLAGS],
            )
            for args in (resync_args, sync_args):
                for expected in ("--checksum", "--resilient", "--recover", "--max-lock", "15m", "--create-empty-src-dirs"):
                    self.assertIn(expected, args)
                self.assertNotIn("--compare", args)

            for env in (resync_env, sync_env):
                self.assertEqual(env["RCLONE_CONFIG"], str(cfg.rclone_config_dest))
                self.assertEqual(env["HOME"], str(cfg.cache_root))
                self.assertEqual(env["RCLONE_CACHE_DIR"], str(cfg.cache_root))

    def test_outage_1_non_critical_error_with_intact_lst_does_not_wipe_or_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._make_config(root)
            cfg.bisync_cache.mkdir(parents=True)
            lst_file = cfg.bisync_cache / "vault.lst"
            lst_file.write_text("ok", encoding="utf-8")

            runner = _ScriptedRcloneRunner([lambda _a, _e: 1])
            notifier = _FakeIrisNotifier()
            engine = BisyncRecoveryEngine(cfg, notifier, runner=runner)

            outcome = engine.run_once()
            self.assertEqual(outcome.status, "sync_non_critical")
            self.assertFalse(outcome.cache_cleared)
            self.assertFalse(outcome.alerted)
            self.assertEqual(engine.consecutive_state_failures, 0)
            self.assertTrue(lst_file.is_file())
            self.assertEqual(notifier.alerts, [])

    def test_outages_2_3_4_grace_window_recovery_and_consecutive_abort_resync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._make_config(root)
            cfg.bisync_cache.mkdir(parents=True)
            old_file = cfg.bisync_cache / "vault.lst-old"
            old_file.write_text("backup", encoding="utf-8")

            def fail_keeping_old(_args: Sequence[str], _env: Mapping[str, str]) -> int:
                return 2

            def recover_to_lst(_args: Sequence[str], _env: Mapping[str, str]) -> int:
                (cfg.bisync_cache / "vault.lst").write_text("healed", encoding="utf-8")
                return 0

            def drop_lst_leaving_old(_args: Sequence[str], _env: Mapping[str, str]) -> int:
                lst = cfg.bisync_cache / "vault.lst"
                if lst.exists():
                    lst.unlink()
                return 2

            def fail_again_with_old(_args: Sequence[str], _env: Mapping[str, str]) -> int:
                return 2

            def resync_succeeds(_args: Sequence[str], _env: Mapping[str, str]) -> int:
                cfg.bisync_cache.mkdir(parents=True, exist_ok=True)
                (cfg.bisync_cache / "vault.lst").write_text("fresh", encoding="utf-8")
                return 0

            runner = _ScriptedRcloneRunner(
                [
                    fail_keeping_old,
                    recover_to_lst,
                    drop_lst_leaving_old,
                    fail_again_with_old,
                    resync_succeeds,
                ]
            )
            notifier = _FakeIrisNotifier()
            engine = BisyncRecoveryEngine(cfg, notifier, runner=runner)

            # Outage 2: first abort missing .lst with .lst-old -> 1-cycle grace window
            o1 = engine.run_once()
            self.assertEqual(o1.status, "recovery_deferred")
            self.assertFalse(o1.cache_cleared)
            self.assertFalse(o1.alerted)
            self.assertEqual(engine.consecutive_state_failures, 1)
            self.assertTrue(old_file.is_file())
            self.assertEqual(notifier.alerts, [])

            # Outage 3: next cycle recovers -> resets counter
            o2 = engine.run_once()
            self.assertEqual(o2.status, "sync_ok")
            self.assertEqual(engine.consecutive_state_failures, 0)
            self.assertEqual(notifier.alerts, [])

            # Outage 4: grace window triggers once, consecutive abort clears cache & alerts, next cycle resyncs
            o3 = engine.run_once()
            self.assertEqual(o3.status, "recovery_deferred")
            self.assertEqual(engine.consecutive_state_failures, 1)

            o4 = engine.run_once()
            self.assertEqual(o4.status, "aborted_cache_cleared")
            self.assertTrue(o4.cache_cleared)
            self.assertTrue(o4.alerted)
            self.assertEqual(engine.consecutive_state_failures, 0)
            self.assertTrue(cfg.cache_root.is_dir())
            self.assertFalse(cfg.bisync_cache.exists())
            self.assertEqual(len(notifier.alerts), 1)
            self.assertEqual(notifier.alerts[0].title, "Bisync Aborted (Cache Cleared)")

            o5 = engine.run_once()
            self.assertEqual(o5.mode, "resync")
            self.assertEqual(o5.status, "initial_resync_ok")
            self.assertIn("--resync", runner.calls[4][0])

    def test_outage_5_abort_with_neither_lst_nor_old_clears_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._make_config(root)
            cfg.bisync_cache.mkdir(parents=True)
            (cfg.bisync_cache / "lockfile.lck").write_text("stale", encoding="utf-8")

            runner = _ScriptedRcloneRunner([lambda _a, _e: 1])
            notifier = _FakeIrisNotifier()
            engine = BisyncRecoveryEngine(cfg, notifier, runner=runner)

            outcome = engine.run_once()
            self.assertEqual(outcome.status, "aborted_cache_cleared")
            self.assertTrue(outcome.cache_cleared)
            self.assertTrue(outcome.alerted)
            self.assertEqual(list(cfg.cache_root.iterdir()), [])
            self.assertEqual(len(notifier.alerts), 1)
            self.assertEqual(notifier.alerts[0].title, "Bisync Aborted (Cache Cleared)")

    def test_outage_6_initial_resync_total_failure_clears_and_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._make_config(root)

            def fail_resync(_args: Sequence[str], _env: Mapping[str, str]) -> int:
                cfg.bisync_cache.mkdir(parents=True, exist_ok=True)
                (cfg.bisync_cache / "partial.tmp").write_text("junk", encoding="utf-8")
                return 3

            runner = _ScriptedRcloneRunner([fail_resync])
            notifier = _FakeIrisNotifier()
            engine = BisyncRecoveryEngine(cfg, notifier, runner=runner)

            outcome = engine.run_once()
            self.assertEqual(outcome.mode, "resync")
            self.assertEqual(outcome.status, "initial_resync_failed")
            self.assertTrue(outcome.cache_cleared)
            self.assertTrue(outcome.alerted)
            self.assertEqual(list(cfg.cache_root.iterdir()), [])
            self.assertEqual(len(notifier.alerts), 1)
            self.assertEqual(notifier.alerts[0].title, "Initial Resync Failed")

    def test_sigterm_during_sync_skips_sleep_and_touches_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._make_config(root, sync_interval=300)
            wait_calls: list[float] = []

            def fake_wait(seconds: float) -> bool:
                wait_calls.append(seconds)
                return True

            engine_holder: list[BisyncRecoveryEngine] = []

            def sync_with_stop(_args: Sequence[str], _env: Mapping[str, str]) -> int:
                engine_holder[0].request_stop()
                return 0

            runner = _ScriptedRcloneRunner([sync_with_stop])
            notifier = _FakeIrisNotifier()
            engine = BisyncRecoveryEngine(cfg, notifier, runner=runner, wait_fn=fake_wait)
            engine_holder.append(engine)

            rc = engine.run_forever()
            self.assertEqual(rc, 0)
            self.assertEqual(wait_calls, [])
            self.assertTrue(cfg.heartbeat_path.is_file())
            self.assertFalse(engine.sync_in_progress)
            self.assertTrue(engine.stop_requested)

    def test_cli_prepare_env_and_run_fail_loud_without_vault_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secrets_dir = root / "secrets"
            secrets_dir.mkdir()
            (secrets_dir / "custom_secret").write_text("[gdrive]\n", encoding="utf-8")
            config_dest = root / "rclone.conf"
            data_dir = root / "data"
            data_dir.mkdir()

            os.environ["RCLONE_CONFIG_SECRET_NAME"] = "custom_secret"
            rc = bisync_main(
                [
                    "prepare-env",
                    "--secrets-dir",
                    str(secrets_dir),
                    "--config-dest",
                    str(config_dest),
                    "--data-dir",
                    str(data_dir),
                ]
            )
            self.assertEqual(rc, 0)
            self.assertTrue(config_dest.is_file())
            self.assertEqual(stat.S_IMODE(config_dest.stat().st_mode), 0o600)

            os.environ.pop("GDRIVE_VAULT_PATH", None)
            stderr_buf = io.StringIO()
            with redirect_stderr(stderr_buf):
                fail_rc = bisync_main(["run"])
            self.assertEqual(fail_rc, 1)
            self.assertIn("GDRIVE_VAULT_PATH", stderr_buf.getvalue())

    def test_clear_directory_contents_preserves_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cache_mount"
            root.mkdir()
            sub = root / "nested"
            sub.mkdir()
            (sub / "file.txt").write_text("x", encoding="utf-8")
            (root / "top.txt").write_text("y", encoding="utf-8")

            clear_directory_contents(root)
            self.assertTrue(root.is_dir())
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
