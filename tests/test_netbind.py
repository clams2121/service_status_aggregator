from __future__ import annotations

import pytest

from service_status_aggregator import netbind
from service_status_aggregator.netbind import BindError, resolve_bind


def test_explicit_local_address() -> None:
    result = resolve_bind("127.0.0.1")
    assert result.ip == "127.0.0.1" and result.kind == "explicit"


def test_explicit_foreign_address_rejected() -> None:
    with pytest.raises(BindError):
        resolve_bind("192.0.2.1")


def test_wildcard_always_rejected() -> None:
    with pytest.raises(BindError):
        resolve_bind("0.0.0.0")  # noqa: S104


def test_auto_finds_tailscale_after_retry() -> None:
    calls: list[int] = []
    slept: list[float] = []

    def detect() -> str | None:
        calls.append(1)
        return "100.100.100.5" if len(calls) == 3 else None

    result = resolve_bind("auto", attempts=5, delay_seconds=0.1, detect=detect, sleep=slept.append)
    assert result == netbind.BindResult("100.100.100.5", "tailscale")
    assert len(calls) == 3 and slept == [0.1, 0.1]


def test_auto_falls_back_to_loopback() -> None:
    result = resolve_bind(
        "auto", attempts=2, delay_seconds=0, detect=lambda: None, sleep=lambda s: None
    )
    assert result == netbind.BindResult("127.0.0.1", "loopback")


def test_strict_tailscale_raises() -> None:
    with pytest.raises(BindError):
        resolve_bind("tailscale", attempts=1, detect=lambda: None, sleep=lambda s: None)


def test_detectors_only_accept_cgnat_range(monkeypatch: pytest.MonkeyPatch) -> None:
    assert netbind._in_tailscale_range("100.64.0.1") == "100.64.0.1"
    assert netbind._in_tailscale_range("100.128.0.1") is None
    assert netbind._in_tailscale_range("garbage") is None
    assert netbind.detect_via_interface("definitely-not-an-iface") is None


def test_detect_chain_survives_exceptions() -> None:
    def boom() -> str | None:
        raise RuntimeError("x")

    assert netbind.detect_tailscale_ipv4((boom, lambda: "100.99.1.1")) == "100.99.1.1"
    assert netbind.detect_tailscale_ipv4((boom,)) is None


def test_port_in_use() -> None:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert netbind.port_in_use("127.0.0.1", port) is True
    assert netbind.port_in_use("127.0.0.1", port) is False
