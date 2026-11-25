"""Tests for charon_runtime.heartbeat and charon_runtime.notify."""

from __future__ import annotations

from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error

_REPO_ROOT = Path(__file__).resolve().parent.parent
if (_REPO_ROOT / "charon_runtime").is_dir() and str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from charon_runtime.heartbeat import touch_heartbeat
from charon_runtime.notify import HttpIrisNotifier, IrisAlert, main as notify_main


class _FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class HeartbeatTests(unittest.TestCase):
    """Verify heartbeat file creation and mtime refresh."""

    def test_touch_heartbeat_creates_parents_and_updates_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            heartbeat = Path(tmp) / "nested" / "dir" / "charon_git_heartbeat"
            touch_heartbeat(heartbeat)
            self.assertTrue(heartbeat.is_file())

            os.utime(heartbeat, (1000, 1000))
            before = heartbeat.stat().st_mtime
            touch_heartbeat(heartbeat)
            after = heartbeat.stat().st_mtime
            self.assertGreater(after, before)


class NotifyTests(unittest.TestCase):
    """Verify HttpIrisNotifier JSON encoding, headers, error handling, and CLI."""

    def test_send_encodes_json_headers_and_optional_node(self) -> None:
        captured: list[tuple[dict[str, str], dict[str, object], float]] = []

        def fake_opener(request: object, timeout: float = 0.0) -> _FakeResponse:
            headers = {k.lower(): v for k, v in request.header_items()}  # type: ignore[attr-defined]
            body = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
            captured.append((headers, body, timeout))
            return _FakeResponse(200)

        notifier = HttpIrisNotifier(url="http://iris.test/notify", timeout=3.5, opener=fake_opener)
        ok_without_node = notifier.send(
            IrisAlert(
                service="git-backup",
                severity="error",
                title='Backup "failed"\nline2',
                details='Target \'vault\' had \\ issues & "quotes"',
            )
        )
        ok_with_node = notifier.send(
            IrisAlert(
                service="dumper",
                severity="warning",
                title="Dump delay",
                details="Took longer",
                node="gaia",
            )
        )

        self.assertTrue(ok_without_node)
        self.assertTrue(ok_with_node)
        self.assertEqual(len(captured), 2)

        headers_1, body_1, timeout_1 = captured[0]
        self.assertEqual(headers_1["content-type"], "application/json")
        self.assertEqual(headers_1["user-agent"], "Yggdrasil-Charon/1.0 (DiscordBot)")
        self.assertEqual(timeout_1, 3.5)
        self.assertNotIn("node", body_1)
        self.assertEqual(body_1["title"], 'Backup "failed"\nline2')
        self.assertEqual(body_1["details"], 'Target \'vault\' had \\ issues & "quotes"')

        _, body_2, _ = captured[1]
        self.assertEqual(body_2["node"], "gaia")

    def test_send_returns_false_and_logs_warning_on_transport_error(self) -> None:
        def failing_opener(request: object, timeout: float = 0.0) -> object:
            del request, timeout
            raise urllib.error.URLError("connection refused")

        notifier = HttpIrisNotifier(url="http://iris.test/notify", opener=failing_opener)
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            ok = notifier.send(
                IrisAlert(service="git-backup", severity="error", title="Fail", details="Boom")
            )

        self.assertFalse(ok)
        self.assertIn("Warning: Iris notification failed", stderr_buf.getvalue())

    def test_cli_main_posts_to_http_server_and_handles_unreachable_port(self) -> None:
        received: list[tuple[dict[str, str], dict[str, object]]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                headers = {k.lower(): v for k, v in self.headers.items()}
                received.append((headers, json.loads(raw)))
                self.send_response(200)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            exit_ok = notify_main(
                [
                    "--service",
                    "git-backup",
                    "--severity",
                    "error",
                    "--title",
                    "CLI Test",
                    "--details",
                    "Details payload",
                    "--node",
                    "gaia",
                    "--url",
                    f"http://127.0.0.1:{port}/notify/discord",
                ]
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(exit_ok, 0)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0]["user-agent"], "Yggdrasil-Charon/1.0 (DiscordBot)")
        self.assertEqual(received[0][1]["node"], "gaia")
        self.assertEqual(received[0][1]["title"], "CLI Test")

        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            exit_fail = notify_main(
                [
                    "--service",
                    "git-backup",
                    "--severity",
                    "error",
                    "--title",
                    "Down",
                    "--details",
                    "Closed port",
                    "--url",
                    f"http://127.0.0.1:{port}/notify/discord",
                    "--timeout",
                    "1.0",
                ]
            )
        self.assertEqual(exit_fail, 1)


if __name__ == "__main__":
    unittest.main()
