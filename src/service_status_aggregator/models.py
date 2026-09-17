"""Request validation for /register and the poll-target allowlist check."""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
HOST_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
MAX_URL_LEN = 2048
MAX_PATH_LEN = 1024
MAX_BODY_BYTES = 16 * 1024

Resolver = Callable[[str], list[IPAddress]]


def parse_ip(value: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def is_valid_hostname(value: str) -> bool:
    if not value or len(value) > 253:
        return False
    labels = value.rstrip(".").split(".")
    return all(HOST_LABEL_RE.match(label) for label in labels)


def normalise_host(value: str) -> str:
    """Lowercase, strip, canonicalise IP literals. Raises ValueError if invalid."""
    host = value.strip().lower().strip("[]")
    if not host:
        raise ValueError("host must not be empty")
    ip = parse_ip(host)
    if ip is not None:
        return str(ip)
    if not is_valid_hostname(host):
        raise ValueError("host must be a valid hostname or IP address")
    return host


def _check_http_url(value: str, field: str) -> str:
    if len(value) > MAX_URL_LEN:
        raise ValueError(f"{field} is longer than {MAX_URL_LEN} characters")
    if any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValueError(f"{field} contains whitespace or control characters")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"}:
        raise ValueError(f"{field} must start with http:// or https://")
    if not parts.hostname:
        raise ValueError(f"{field} must include a host")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{field} must not contain credentials")
    try:
        parts.port  # noqa: B018 - property access validates the port
    except ValueError as exc:
        raise ValueError(f"{field} has an invalid port") from exc
    return value


class RegistrationIn(BaseModel):
    """The POST /register payload. Every field is required; unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=64)
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    health_url: str = Field(max_length=MAX_URL_LEN)
    log_path: str = Field(max_length=MAX_PATH_LEN)
    config_page_url: str = Field(max_length=MAX_URL_LEN)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.lower()
        if not NAME_RE.match(v):
            raise ValueError(
                "name must be 1-64 characters of lowercase letters, digits, '-' or '_' "
                "and start with a letter or digit"
            )
        return v

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        return normalise_host(v)

    @field_validator("health_url")
    @classmethod
    def _health_url(cls, v: str) -> str:
        return _check_http_url(v, "health_url")

    @field_validator("config_page_url")
    @classmethod
    def _config_page_url(cls, v: str) -> str:
        if v == "":
            return v
        return _check_http_url(v, "config_page_url")

    @field_validator("log_path")
    @classmethod
    def _log_path(cls, v: str) -> str:
        if v == "":
            return v
        if not v.startswith("/"):
            raise ValueError("log_path must be an absolute path or empty")
        if any(ord(ch) < 32 for ch in v):
            raise ValueError("log_path contains control characters")
        return v

    @model_validator(mode="after")
    def _health_url_matches_host(self) -> RegistrationIn:
        url_host = urlsplit(self.health_url).hostname or ""
        try:
            url_host = normalise_host(url_host)
        except ValueError as exc:
            raise ValueError(f"health_url host is invalid: {exc}") from exc
        if url_host != self.host:
            raise ValueError(
                f"health_url host '{url_host}' must match the registered host '{self.host}'"
            )
        return self

    def as_record(self) -> dict[str, Any]:
        return self.model_dump()


# --------------------------------------------------------------------------- target allowlist


@dataclass(frozen=True)
class TargetDecision:
    allowed: bool
    reason: str
    addresses: tuple[str, ...] = ()


def system_resolver(host: str) -> list[IPAddress]:
    """Resolve *host* to every address the system resolver returns."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []
    found: list[IPAddress] = []
    for info in infos:
        ip = parse_ip(info[4][0])
        if ip is not None and ip not in found:
            found.append(ip)
    return found


def check_target(
    host: str,
    allowed: Sequence[IPNetwork],
    resolver: Resolver = system_resolver,
) -> TargetDecision:
    """Decide whether the aggregator may make an HTTP request to *host*.

    IP literals are checked directly. Hostnames are resolved and every returned
    address must fall inside the allowlist; a name that resolves to nothing is
    rejected. This runs at registration time and again before every poll.
    """
    ip = parse_ip(host)
    if ip is not None:
        addresses: list[IPAddress] = [ip]
    else:
        addresses = resolver(host)
        if not addresses:
            return TargetDecision(False, f"dns_unresolvable: {host}")
    outside = [str(a) for a in addresses if not any(a in net for net in allowed)]
    if outside:
        return TargetDecision(
            False,
            f"target_rejected: {host} resolves to {', '.join(outside)} outside the allowlist",
            tuple(str(a) for a in addresses),
        )
    return TargetDecision(True, "ok", tuple(str(a) for a in addresses))
