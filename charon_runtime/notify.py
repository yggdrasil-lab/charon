"""Iris Discord notification port, HTTP adapter, and CLI entrypoint."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import json
import sys
from typing import Any, Protocol
import urllib.error
import urllib.request

DEFAULT_IRIS_URL = "http://iris:8000/notify/discord"
DEFAULT_USER_AGENT = "Yggdrasil-Charon/1.0 (DiscordBot)"


@dataclass(frozen=True)
class IrisAlert:
    """Immutable alert payload sent to Iris."""

    service: str
    severity: str
    title: str
    details: str
    bot_name: str = "Charon"
    node: str | None = None


class IrisNotifier(Protocol):
    """Port for sending alert notifications to Iris."""

    def send(self, alert: IrisAlert) -> bool:
        """Send an alert and return True on success, False on failure."""
        ...


class HttpIrisNotifier:
    """Stdlib urllib adapter for Iris Discord notification endpoint."""

    def __init__(
        self,
        url: str = DEFAULT_IRIS_URL,
        timeout: float = 5.0,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self._url = url
        self._timeout = timeout
        self._opener = opener or urllib.request.urlopen

    def send(self, alert: IrisAlert) -> bool:
        """POST JSON alert payload to Iris, logging and returning False on error."""
        payload = _encode_alert(alert)
        request = urllib.request.Request(
            self._url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": DEFAULT_USER_AGENT,
            },
        )
        try:
            response = self._opener(request, timeout=self._timeout)
            return _consume_response(response)
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
            print(
                f"Warning: Iris notification failed for {alert.service}: {exc}",
                file=sys.stderr,
            )
            return False


def _encode_alert(alert: IrisAlert) -> bytes:
    body: dict[str, str] = {
        "service": alert.service,
        "bot_name": alert.bot_name,
        "severity": alert.severity,
        "title": alert.title,
        "details": alert.details,
    }
    if alert.node is not None:
        body["node"] = alert.node
    return json.dumps(body).encode("utf-8")


def _consume_response(response: Any) -> bool:
    if hasattr(response, "__enter__"):
        with response as ctx:
            status = getattr(ctx, "status", 200)
            return int(status) < 400
    status = getattr(response, "status", 200)
    return int(status) < 400


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m charon_runtime.notify",
        description="Send a Discord alert via the Iris notification API.",
    )
    parser.add_argument("--service", required=True)
    parser.add_argument("--severity", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--details", required=True)
    parser.add_argument("--bot-name", default="Charon")
    parser.add_argument("--node", default=None)
    parser.add_argument("--url", default=DEFAULT_IRIS_URL)
    parser.add_argument("--timeout", type=float, default=5.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for sending an Iris alert."""
    args = _build_parser().parse_args(argv)
    notifier = HttpIrisNotifier(url=args.url, timeout=args.timeout)
    alert = IrisAlert(
        service=args.service,
        severity=args.severity,
        title=args.title,
        details=args.details,
        bot_name=args.bot_name,
        node=args.node,
    )
    return 0 if notifier.send(alert) else 1


if __name__ == "__main__":
    raise SystemExit(main())
