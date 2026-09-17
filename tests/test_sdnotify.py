from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from service_status_aggregator import sdnotify


def test_noop_without_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert sdnotify.notify("READY=1") is False


def test_sends_datagram(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "notify.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(str(path))
        server.settimeout(2)
        monkeypatch.setenv("NOTIFY_SOCKET", str(path))
        assert sdnotify.notify("READY=1") is True
        assert server.recv(64) == b"READY=1"


def test_watchdog_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    assert sdnotify.watchdog_interval_seconds() is None
    monkeypatch.setenv("WATCHDOG_USEC", "90000000")
    assert sdnotify.watchdog_interval_seconds() == 90.0
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid() + 1))
    assert sdnotify.watchdog_interval_seconds() is None
