"""Choose the local bind address: Tailscale first, loopback as a loud fallback, never 0.0.0.0."""

from __future__ import annotations

import fcntl
import ipaddress
import json
import logging
import shutil
import socket
import struct
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

from service_status_aggregator.config import FORBIDDEN_BINDS

log = logging.getLogger("service_status_aggregator.netbind")

TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_IFACE = "tailscale0"
SIOCGIFADDR = 0x8915

KIND_TAILSCALE = "tailscale"
KIND_LOOPBACK = "loopback"
KIND_EXPLICIT = "explicit"

Detector = Callable[[], str | None]


class BindError(Exception):
    pass


@dataclass(frozen=True)
class BindResult:
    ip: str
    kind: str

    @property
    def url_host(self) -> str:
        return f"[{self.ip}]" if ":" in self.ip else self.ip


def _in_tailscale_range(value: str) -> str | None:
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    return str(ip) if ip in TAILSCALE_V4 else None


def detect_via_cli() -> str | None:
    exe = shutil.which("tailscale")
    if not exe:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, resolved binary
            [exe, "ip", "-4"], capture_output=True, text=True, timeout=3, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("tailscale cli failed: %s", exc)
        return None
    for line in proc.stdout.splitlines():
        found = _in_tailscale_range(line)
        if found:
            return found
    return None


def detect_via_interface(name: str = TAILSCALE_IFACE) -> str | None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            packed = struct.pack("256s", name.encode()[:15])
            res = fcntl.ioctl(sock.fileno(), SIOCGIFADDR, packed)
        except OSError:
            return None
    return _in_tailscale_range(socket.inet_ntoa(res[20:24]))


def detect_via_ip_json() -> str | None:
    exe = shutil.which("ip")
    if not exe:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, resolved binary
            [exe, "-j", "-4", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        data = json.loads(proc.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.debug("ip -j failed: %s", exc)
        return None
    for iface in data:
        for addr in iface.get("addr_info", []):
            found = _in_tailscale_range(str(addr.get("local", "")))
            if found:
                return found
    return None


DEFAULT_DETECTORS: tuple[Detector, ...] = (detect_via_cli, detect_via_interface, detect_via_ip_json)


def detect_tailscale_ipv4(detectors: tuple[Detector, ...] = DEFAULT_DETECTORS) -> str | None:
    for detector in detectors:
        try:
            found = detector()
        except Exception as exc:  # noqa: BLE001 - a detector must never take the process down
            log.debug("detector %s raised: %s", getattr(detector, "__name__", detector), exc)
            continue
        if found:
            log.debug("tailscale ip %s via %s", found, getattr(detector, "__name__", detector))
            return found
    return None


def is_local_address(ip: str) -> bool:
    """True if *ip* is assigned to a local interface (we can bind it)."""
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return False
    family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((ip, 0))
        except OSError:
            return False
    return True


def port_in_use(ip: str, port: int) -> bool:
    parsed = ipaddress.ip_address(ip)
    family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((ip, port))
        except OSError:
            return True
    return False


def resolve_bind(
    bind: str,
    *,
    attempts: int = 10,
    delay_seconds: float = 3.0,
    detect: Callable[[], str | None] = detect_tailscale_ipv4,
    sleep: Callable[[float], None] = time.sleep,
) -> BindResult:
    """Turn the configured bind setting into a concrete local address.

    "auto": Tailscale IPv4 if found (retrying, since tailscaled may still be starting),
            else 127.0.0.1 with a loud warning.
    "tailscale": same detection, but a BindError instead of the loopback fallback.
    explicit IP: must be assigned locally and must not be a wildcard.
    """
    if bind in FORBIDDEN_BINDS:
        raise BindError(f"refusing to bind {bind}: this service never listens on all interfaces")
    if bind not in {"auto", "tailscale"}:
        if not is_local_address(bind):
            raise BindError(
                f"configured bind address {bind} is not assigned to any local interface"
            )
        return BindResult(bind, KIND_EXPLICIT)

    for attempt in range(1, attempts + 1):
        found = detect()
        if found:
            return BindResult(found, KIND_TAILSCALE)
        if attempt < attempts:
            log.info(
                "no Tailscale IPv4 found yet (attempt %d/%d); retrying in %gs",
                attempt,
                attempts,
                delay_seconds,
            )
            sleep(delay_seconds)

    if bind == "tailscale":
        raise BindError('no Tailscale IPv4 address found and bind = "tailscale" forbids fallback')
    log.warning(
        "no Tailscale IPv4 address found after %d attempts; falling back to 127.0.0.1. "
        "The dashboard is only reachable locally or through an SSH tunnel.",
        attempts,
    )
    return BindResult("127.0.0.1", KIND_LOOPBACK)
