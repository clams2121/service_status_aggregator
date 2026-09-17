"""Minimal sd_notify(3) client. No-op when not running under systemd."""

from __future__ import annotations

import logging
import os
import socket

log = logging.getLogger("service_status_aggregator.sdnotify")


def notify(state: str) -> bool:
    """Send *state* (e.g. "READY=1") to $NOTIFY_SOCKET. Returns True if sent."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(state.encode())
        return True
    except OSError as exc:
        log.warning("sd_notify(%r) failed: %s", state, exc)
        return False


def watchdog_interval_seconds() -> float | None:
    """The watchdog period systemd expects, or None if no watchdog is configured for us."""
    usec = os.environ.get("WATCHDOG_USEC")
    if not usec or not usec.isdigit():
        return None
    pid = os.environ.get("WATCHDOG_PID")
    if pid and pid.isdigit() and int(pid) != os.getpid():
        return None
    return int(usec) / 1_000_000
