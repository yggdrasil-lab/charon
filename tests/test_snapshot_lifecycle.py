"""Unit and integration tests for charon_runtime.snapshot_lifecycle."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
if (_REPO_ROOT / "charon_runtime").is_dir() and str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from charon_runtime.notify import IrisAlert
from charon_runtime.snapshot_lifecycle import (
    RestoreCancelledError,
    SnapshotConfig,
    SnapshotLifecycle,
    SnapshotTarget,
    SubprocessPostgresRunner,
    load_snapshot_targets,
    main as snapshot_main,
    resolve_postgres_password,
)


class _RecordingNotifier:
    def __init__(self) -> None:
        self.alerts: list[IrisAlert] = []

    def send(self, alert: IrisAlert) -> bool:
        self.alerts.append(alert)
        return True


class _FakePostgresRunner:
    def __init__(
        self,
        *,
        ready: bool = True,
        fail_dump: bool = False,
        fail_restore: bool = False,
        toc_entries: int = 14,
        fail_toc: bool = False,
    ) -> None:
        self.ready = ready
        self.fail_dump = fail_dump
        self.fail_restore = fail_restore
        self.toc_entries = toc_entries
        self.fail_toc = fail_toc
        self.calls: list[tuple[str, str, str]] = []

    def is_ready(self, target: SnapshotTarget, password: str) -> bool:
        self.calls.append(("is_ready", target.name, password))
        return self.ready

    def dump(self, target: SnapshotTarget, password: str, output_path: Path) -> None:
        self.calls.append(("dump", target.name, password))
        output_path.write_bytes(b"PGDMP-PARTIAL")
        if self.fail_dump:
            raise RuntimeError("pg_dump simulated failure")
        output_path.write_bytes(b"PGDMP-VALID-SNAPSHOT")

    def restore(self, target: SnapshotTarget, password: str, snapshot_path: Path) -> None:
        self.calls.append(("restore", target.name, f"{password}:{snapshot_path.name}"))
        if self.fail_restore:
            raise RuntimeError("pg_restore simulated failure")

    def list_toc_entries(self, snapshot_path: Path) -> int:
        self.calls.append(("list_toc_entries", snapshot_path.name, ""))
        if self.fail_toc:
            raise RuntimeError("invalid dump TOC")
        return self.toc_entries


def _write_targets(path: Path, entries: list[dict[str, object]]) -> Path:
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def _create_sqlite_db(db_path: Path, rows: list[tuple[int, str]]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, label TEXT NOT NULL)")
        conn.execute("DELETE FROM items")
        conn.executemany("INSERT INTO items (id, label) VALUES (?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


def _read_sqlite_rows(db_path: Path) -> list[tuple[int, str]]:
    conn = sqlite3.connect(str(db_path))
    try:
        return list(conn.execute("SELECT id, label FROM items ORDER BY id"))
    finally:
        conn.close()


class SnapshotTargetAndPasswordTests(unittest.TestCase):
    """Verify target loading, node filtering, and fail-loud password resolution."""

    def test_node_filtering_handles_gaia_muspelheim_wildcard_and_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            targets_file = _write_targets(
                Path(tmp) / "targets.json",
                [
                    {"name": "gaia_db", "type": "postgres", "node": "gaia"},
                    {"name": "musp_db", "type": "sqlite", "node": "muspelheim", "source": "/tmp/a.db"},
                    {"name": "shared_all", "type": "archive", "node": "*", "source_dir": "/tmp", "paths": ["x"]},
                    {"name": "default_null", "type": "sqlite", "source": "/tmp/b.db"},
                ],
            )
            gaia_names = [t.name for t in load_snapshot_targets(targets_file, node="gaia")]
            musp_names = [t.name for t in load_snapshot_targets(targets_file, node="muspelheim")]
            all_names = [t.name for t in load_snapshot_targets(targets_file, node=None)]

            self.assertEqual(gaia_names, ["gaia_db", "shared_all", "default_null"])
            self.assertEqual(musp_names, ["musp_db", "shared_all", "default_null"])
            self.assertEqual(all_names, ["gaia_db", "musp_db", "shared_all", "default_null"])

    def test_production_targets_json_parses_for_both_nodes(self) -> None:
        candidates = (
            _REPO_ROOT / "dumper" / "targets.json",
            Path("/etc/charon-dumper/targets.json"),
        )
        prod_targets = next((path for path in candidates if path.is_file()), candidates[0])
        gaia_targets = load_snapshot_targets(prod_targets, node="gaia")
        musp_targets = load_snapshot_targets(prod_targets, node="muspelheim")
        self.assertGreater(len(gaia_targets), 0)
        self.assertGreater(len(musp_targets), 0)

    def test_resolve_postgres_password_uses_file_then_env_and_rejects_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pw_file = Path(tmp) / "secret"
            pw_file.write_text("  file-secret-123 \n", encoding="utf-8")

            from_file = SnapshotTarget(
                name="authentik",
                kind="postgres",
                password_file=pw_file,
                password_env="AUTH_PW",
            )
            self.assertEqual(
                resolve_postgres_password(from_file, {"AUTH_PW": "env-secret"}),
                "file-secret-123",
            )

            missing_file = SnapshotTarget(
                name="eir",
                kind="postgres",
                password_file=Path(tmp) / "does-not-exist",
                password_env="EIR_PW",
            )
            self.assertEqual(
                resolve_postgres_password(missing_file, {"EIR_PW": "$(echo unsafe)"}),
                "$(echo unsafe)",
            )

            empty_target = SnapshotTarget(name="broken", kind="postgres", password_env="MISSING")
            with self.assertRaises(ValueError):
                resolve_postgres_password(empty_target, {})


class SnapshotLifecycleSqliteAndArchiveTests(unittest.TestCase):
    """End-to-end tests for SQLite, Archive, retention, and safety rollback."""

    def test_sqlite_backup_verify_restore_and_corrupt_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live_db = root / "data" / "lldap" / "users.db"
            _create_sqlite_db(live_db, [(1, "alex"), (2, "sam")])
            targets_file = _write_targets(
                root / "targets.json",
                [{"name": "lldap", "type": "sqlite", "node": "gaia", "source": str(live_db)}],
            )
            backup_dir = root / "backups"
            heartbeat = root / "hb" / "charon_dumper_heartbeat"
            notifier = _RecordingNotifier()
            config = SnapshotConfig(
                targets_path=targets_file,
                backup_dir=backup_dir,
                node="gaia",
                keep_days=30,
                heartbeat_path=heartbeat,
            )
            lifecycle = SnapshotLifecycle(config, notifier, clock=lambda: 1700000000.0)

            report = lifecycle.backup_all()
            self.assertEqual(report.total, 1)
            self.assertEqual(report.failed, 0)
            self.assertTrue(heartbeat.is_file())
            self.assertEqual(len(notifier.alerts), 1)
            self.assertEqual(notifier.alerts[0].severity, "info")
            self.assertEqual(notifier.alerts[0].node, "gaia")

            verify_results = lifecycle.verify()
            self.assertEqual(len(verify_results), 1)
            self.assertTrue(verify_results[0].ok)
            self.assertEqual(verify_results[0].detail_count, 1)

            # Mutate live DB, then restore from valid snapshot and confirm Safety Snapshot deleted
            _create_sqlite_db(live_db, [(99, "mutated")])
            restored_path = lifecycle.restore("lldap", confirm=True)
            self.assertTrue(restored_path.is_file())
            self.assertEqual(_read_sqlite_rows(live_db), [(1, "alex"), (2, "sam")])
            self.assertFalse(Path(f"{live_db}.pre_restore_bak").exists())

            # Mutate live DB again and attempt restore from corrupt snapshot -> rollback preserves live DB
            _create_sqlite_db(live_db, [(42, "preserved-live")])
            corrupt_snapshot = backup_dir / "lldap" / "lldap_db_20990101_000000.sqlite"
            corrupt_snapshot.write_bytes(b"corrupted-not-a-sqlite-database")
            with self.assertRaises(RuntimeError):
                lifecycle.restore("lldap", requested_file=corrupt_snapshot.name, confirm=True)
            self.assertEqual(_read_sqlite_rows(live_db), [(42, "preserved-live")])
            self.assertFalse(Path(f"{live_db}.pre_restore_bak").exists())

    def test_sqlite_integrity_check_failure_rolls_back_safety_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live_db = root / "data" / "app.db"
            _create_sqlite_db(live_db, [(1, "healthy")])
            targets_file = _write_targets(
                root / "targets.json",
                [{"name": "app", "type": "sqlite", "node": "gaia", "source": str(live_db)}],
            )
            config = SnapshotConfig(targets_path=targets_file, backup_dir=root / "backups", node="gaia")
            lifecycle = SnapshotLifecycle(config, _RecordingNotifier(), clock=lambda: 1700000000.0)
            lifecycle.backup_all()

            _create_sqlite_db(live_db, [(7, "current-live")])
            with patch(
                "charon_runtime.snapshot_lifecycle._check_sqlite_integrity",
                side_effect=RuntimeError("Integrity check failed (malformed)"),
            ):
                with self.assertRaises(RuntimeError):
                    lifecycle.restore("app", confirm=True)

            self.assertEqual(_read_sqlite_rows(live_db), [(7, "current-live")])
            self.assertFalse(Path(f"{live_db}.pre_restore_bak").exists())

    def test_archive_backup_verify_restore_and_traversal_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_dir = root / "src" / "authentik"
            (src_dir / "media").mkdir(parents=True)
            (src_dir / "media" / "logo.txt").write_text("logo-v1\n", encoding="utf-8")

            targets_file = _write_targets(
                root / "targets.json",
                [
                    {
                        "name": "authentik/data",
                        "type": "archive",
                        "node": "gaia",
                        "source_dir": str(src_dir),
                        "paths": ["media", "custom-templates"],
                    }
                ],
            )
            backup_dir = root / "backups"
            config = SnapshotConfig(targets_path=targets_file, backup_dir=backup_dir, node="gaia")
            lifecycle = SnapshotLifecycle(config, _RecordingNotifier(), clock=lambda: 1700000000.0)

            report = lifecycle.backup_all()
            self.assertEqual(report.failed, 0)
            snap = report.results[0].snapshot_path
            assert snap is not None
            self.assertTrue(snap.name.startswith("data_config_"))
            self.assertTrue(snap.name.endswith(".tar.gz"))

            verify_res = lifecycle.verify("authentik/data")
            self.assertTrue(verify_res[0].ok)
            self.assertGreaterEqual(verify_res[0].detail_count, 2)

            (src_dir / "media" / "logo.txt").write_text("overwritten\n", encoding="utf-8")
            lifecycle.restore("authentik/data", confirm=True)
            self.assertEqual((src_dir / "media" / "logo.txt").read_text(encoding="utf-8"), "logo-v1\n")

            # Craft traversal tar.gz and verify restore rejects it without writing outside source_dir
            escaped_target = root / "escaped.txt"
            evil_tar = backup_dir / "authentik" / "data" / "data_config_20990101_000000.tar.gz"
            with tarfile.open(evil_tar, "w:gz") as tf:
                payload = b"pwned"
                info = tarfile.TarInfo(name="../../escaped.txt")
                info.size = len(payload)
                tf.addfile(info, io.BytesIO(payload))
            with self.assertRaises(ValueError):
                lifecycle.restore("authentik/data", requested_file=evil_tar.name, confirm=True)
            self.assertFalse(escaped_target.exists())

            # Craft symlink escape tar.gz and verify restore rejects it
            evil_sym = backup_dir / "authentik" / "data" / "data_config_20990101_000001.tar.gz"
            with tarfile.open(evil_sym, "w:gz") as tf:
                sym = tarfile.TarInfo(name="media/link")
                sym.type = tarfile.SYMTYPE
                sym.linkname = "../../../../etc/passwd"
                tf.addfile(sym)
            with self.assertRaises(ValueError):
                lifecycle.restore("authentik/data", requested_file=evil_sym.name, confirm=True)

    def test_archive_missing_source_dir_and_missing_paths_fail_loud(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            empty_dir = root / "empty_src"
            empty_dir.mkdir()
            targets_file = _write_targets(
                root / "targets.json",
                [
                    {
                        "name": "missing_dir",
                        "type": "archive",
                        "node": "gaia",
                        "source_dir": str(root / "nope"),
                        "paths": ["a"],
                    },
                    {
                        "name": "missing_paths",
                        "type": "archive",
                        "node": "gaia",
                        "source_dir": str(empty_dir),
                        "paths": ["does_not_exist"],
                    },
                ],
            )
            notifier = _RecordingNotifier()
            config = SnapshotConfig(targets_path=targets_file, backup_dir=root / "backups", node="gaia")
            report = SnapshotLifecycle(config, notifier).backup_all()

            self.assertEqual(report.total, 2)
            self.assertEqual(report.failed, 2)
            self.assertEqual(len(notifier.alerts), 1)
            self.assertEqual(notifier.alerts[0].severity, "warning")

    def test_retention_pruning_respects_clock_and_maxdepth_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_dir = root / "backups"
            auth_dir = backup_dir / "authentik"
            nested_dir = auth_dir / "data"
            nested_dir.mkdir(parents=True)

            now = 1_700_000_000.0
            old_dump = auth_dir / "authentik_db_20230101_000000.dump"
            fresh_dump = auth_dir / "authentik_db_20231110_000000.dump"
            nested_old_archive = nested_dir / "data_config_20230101_000000.tar.gz"
            old_dump.write_bytes(b"old")
            fresh_dump.write_bytes(b"fresh")
            nested_old_archive.write_bytes(b"nested-must-survive")

            os.utime(old_dump, (now - 40 * 86400, now - 40 * 86400))
            os.utime(fresh_dump, (now - 5 * 86400, now - 5 * 86400))
            os.utime(nested_old_archive, (now - 40 * 86400, now - 40 * 86400))

            targets_file = _write_targets(
                root / "targets.json",
                [
                    {
                        "name": "authentik",
                        "type": "postgres",
                        "node": "gaia",
                        "password_env": "AUTH_PW",
                    }
                ],
            )
            pg = _FakePostgresRunner()
            config = SnapshotConfig(targets_path=targets_file, backup_dir=backup_dir, node="gaia", keep_days=30)
            lifecycle = SnapshotLifecycle(
                config,
                _RecordingNotifier(),
                postgres=pg,
                env={"AUTH_PW": "pw"},
                clock=lambda: now,
            )

            listed = lifecycle.list_snapshots("authentik")["authentik"]
            self.assertEqual([item.name for item in listed], [fresh_dump.name, old_dump.name])

            report = lifecycle.backup_all()
            self.assertEqual(report.failed, 0)
            self.assertEqual(report.results[0].pruned_count, 1)
            self.assertFalse(old_dump.exists())
            self.assertTrue(fresh_dump.exists())
            self.assertTrue(nested_old_archive.exists())


class SnapshotLifecyclePostgresAndCliTests(unittest.TestCase):
    """Tests for Postgres runner, CLI subcommands, confirmation, and zero-target alert skip."""

    def test_postgres_unreachable_and_dump_failure_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            targets_file = _write_targets(
                root / "targets.json",
                [{"name": "eir", "type": "postgres", "node": "gaia", "password_env": "EIR_PW"}],
            )
            backup_dir = root / "backups"
            notifier = _RecordingNotifier()
            config = SnapshotConfig(targets_path=targets_file, backup_dir=backup_dir, node="gaia")

            unready_pg = _FakePostgresRunner(ready=False)
            report_unready = SnapshotLifecycle(
                config, notifier, postgres=unready_pg, env={"EIR_PW": "secret"}
            ).backup_all()
            self.assertEqual(report_unready.failed, 1)
            self.assertIn("Unreachable", report_unready.results[0].message)
            self.assertEqual(notifier.alerts[-1].severity, "warning")

            failing_pg = _FakePostgresRunner(ready=True, fail_dump=True)
            report_fail_dump = SnapshotLifecycle(
                config, notifier, postgres=failing_pg, env={"EIR_PW": "secret"}
            ).backup_all()
            self.assertEqual(report_fail_dump.failed, 1)
            self.assertEqual(list((backup_dir / "eir").glob("*.dump")), [])

    def test_postgres_verify_and_restore_fail_loud(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            targets_file = _write_targets(
                root / "targets.json",
                [{"name": "kratos", "type": "postgres", "node": "gaia", "password_env": "KRATOS_PW"}],
            )
            backup_dir = root / "backups"
            config = SnapshotConfig(targets_path=targets_file, backup_dir=backup_dir, node="gaia")
            pg = _FakePostgresRunner(toc_entries=22)
            lifecycle = SnapshotLifecycle(config, _RecordingNotifier(), postgres=pg, env={"KRATOS_PW": "pw"})

            lifecycle.backup_all()
            verify_res = lifecycle.verify("kratos")
            self.assertTrue(verify_res[0].ok)
            self.assertEqual(verify_res[0].detail_count, 22)

            restored = lifecycle.restore("kratos", confirm=True)
            self.assertTrue(restored.name.startswith("kratos_db_"))

            pg.fail_restore = True
            with self.assertRaises(RuntimeError):
                lifecycle.restore("kratos", confirm=True)

            pg.ready = False
            with self.assertRaises(RuntimeError):
                lifecycle.restore("kratos", confirm=True)

    def test_cancelled_confirmation_and_zero_targets_skips_iris(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live_db = root / "db.sqlite"
            _create_sqlite_db(live_db, [(1, "keep")])
            targets_file = _write_targets(
                root / "targets.json",
                [{"name": "lldap", "type": "sqlite", "node": "gaia", "source": str(live_db)}],
            )
            notifier = _RecordingNotifier()
            gaia_cfg = SnapshotConfig(targets_path=targets_file, backup_dir=root / "backups", node="gaia")
            lifecycle = SnapshotLifecycle(gaia_cfg, notifier, confirm_fn=lambda _: False)
            lifecycle.backup_all()

            _create_sqlite_db(live_db, [(2, "untouched")])
            with self.assertRaises(RestoreCancelledError):
                lifecycle.restore("lldap", confirm=False)
            self.assertEqual(_read_sqlite_rows(live_db), [(2, "untouched")])

            other_notifier = _RecordingNotifier()
            musp_cfg = SnapshotConfig(targets_path=targets_file, backup_dir=root / "backups", node="muspelheim")
            empty_report = SnapshotLifecycle(musp_cfg, other_notifier).backup_all()
            self.assertEqual(empty_report.total, 0)
            self.assertEqual(other_notifier.alerts, [])

    def test_subprocess_postgres_runner_passes_pgpassword_and_flags(self) -> None:
        target = SnapshotTarget(
            name="eir",
            kind="postgres",
            host="eir-db",
            port=5432,
            user="eir",
            database="eir",
        )
        runner = SubprocessPostgresRunner(base_env={"PATH": "/usr/bin"})
        recorded: list[tuple[list[str], dict[str, str]]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            recorded.append((list(cmd), dict(kwargs["env"])))  # type: ignore[arg-type]
            stdout = "123; 1 TABLE\n; comment\n456; 2 INDEX\n" if "--list" in cmd else ""
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

        with patch("charon_runtime.snapshot_lifecycle.subprocess.run", side_effect=fake_run):
            self.assertTrue(runner.is_ready(target, "s3cr3t"))
            runner.dump(target, "s3cr3t", Path("/tmp/eir.dump"))
            runner.restore(target, "s3cr3t", Path("/tmp/eir.dump"))
            toc_count = runner.list_toc_entries(Path("/tmp/eir.dump"))

        self.assertEqual(toc_count, 2)
        self.assertEqual(recorded[0][0][0], "pg_isready")
        self.assertEqual(recorded[0][1]["PGPASSWORD"], "s3cr3t")
        self.assertIn("-F", recorded[1][0])
        self.assertIn("c", recorded[1][0])
        self.assertIn("--clean", recorded[2][0])
        self.assertIn("--if-exists", recorded[2][0])
        self.assertEqual(recorded[3][0][:2], ["pg_restore", "--list"])

    def test_cli_subcommands_backup_list_verify_and_restore_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live_db = root / "users.db"
            _create_sqlite_db(live_db, [(1, "initial")])
            targets_file = _write_targets(
                root / "targets.json",
                [{"name": "lldap", "type": "sqlite", "node": "gaia", "source": str(live_db)}],
            )
            backup_dir = root / "backups"
            heartbeat = root / "hb"
            env = {
                "NODE_HOSTNAME": "gaia",
                "TARGETS_CONFIG": str(targets_file),
                "BACKUP_DIR": str(backup_dir),
                "HEARTBEAT_PATH": str(heartbeat),
                "BACKUP_KEEP_DAYS": "30",
            }
            with patch.dict(os.environ, env, clear=True):
                with patch("charon_runtime.snapshot_lifecycle.HttpIrisNotifier.send", return_value=True):
                    out = io.StringIO()
                    with redirect_stdout(out):
                        self.assertEqual(snapshot_main(["backup"]), 0)
                    self.assertIn("1 succeeded, 0 failed", out.getvalue())

                list_out = io.StringIO()
                with redirect_stdout(list_out):
                    self.assertEqual(snapshot_main(["list"]), 0)
                self.assertIn("lldap", list_out.getvalue())

                verify_out = io.StringIO()
                with redirect_stdout(verify_out):
                    self.assertEqual(snapshot_main(["verify", "--all"]), 0)
                self.assertIn("[PASS]", verify_out.getvalue())

                _create_sqlite_db(live_db, [(2, "overwritten")])
                restore_out = io.StringIO()
                with redirect_stdout(restore_out):
                    self.assertEqual(snapshot_main(["restore", "lldap", "--confirm"]), 0)
                self.assertIn("SUCCESS:", restore_out.getvalue())
                self.assertEqual(_read_sqlite_rows(live_db), [(1, "initial")])

            with patch.dict(os.environ, {"TARGETS_CONFIG": str(root / "missing.json")}, clear=True):
                err = io.StringIO()
                with redirect_stderr(err):
                    self.assertEqual(snapshot_main(["list"]), 1)
                self.assertIn("not found", err.getvalue())


if __name__ == "__main__":
    unittest.main()
