"""TOML configuration loading and validation.

The service refuses to start without a usable configuration: every problem found
is collected and reported together, then the process exits with status 2.
"""

from __future__ import annotations

import ipaddress
import os
import tomllib
import zoneinfo
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("/etc/service_status_aggregator/config.toml")
CONFIG_ENV_VAR = "SSA_CONFIG"
TOKEN_ENV_VAR = "SSA_REGISTRATION_TOKEN"  # noqa: S105 - name of the variable, not a secret
TOKEN_CREDENTIAL_NAME = "registration_token"  # noqa: S105 - systemd credential name
MIN_TOKEN_LENGTH = 16

# Tailscale CGNAT IPv4, Tailscale IPv6 ULA, IPv4 and IPv6 loopback.
DEFAULT_ALLOWED_TARGET_CIDRS = (
    "100.64.0.0/10",
    "fd7a:115c:a1e0::/48",
    "127.0.0.0/8",
    "::1/128",
)

FORBIDDEN_BINDS = {"0.0.0.0", "::", "0:0:0:0:0:0:0:0"}  # noqa: S104 - rejected, never used
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class ConfigError(Exception):
    """Raised when the configuration is missing or invalid. Carries every problem."""

    def __init__(self, problems: list[str], path: Path | None = None) -> None:
        self.problems = problems
        self.path = path
        where = f" ({path})" if path else ""
        super().__init__(f"invalid configuration{where}:\n  - " + "\n  - ".join(problems))


@dataclass(frozen=True)
class ServerConfig:
    bind: str = "auto"
    port: int = 8720


@dataclass(frozen=True)
class StorageConfig:
    db_path: Path = Path("/var/lib/service_status_aggregator/aggregator.db")


MIN_INTERVAL_SECONDS = 10.0
MAX_INTERVAL_SECONDS = 600.0


@dataclass(frozen=True)
class PollingConfig:
    interval_seconds: float = 60.0
    timeout_seconds: float = 5.0
    max_concurrent: int = 10


@dataclass(frozen=True)
class RegistrationConfig:
    staleness_seconds: float = 900.0
    require_token: bool = True
    allowed_target_cidrs: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = field(
        default_factory=lambda: tuple(ipaddress.ip_network(c) for c in DEFAULT_ALLOWED_TARGET_CIDRS)
    )
    register_self: bool = True
    self_register_interval_seconds: float = 300.0


@dataclass(frozen=True)
class HistoryConfig:
    retention_days: int = 90


@dataclass(frozen=True)
class DisplayConfig:
    history_days: int = 90
    timezone: str = "UTC"
    major_outage_minutes: float = 30.0
    degraded_response_ms: float = 1000.0


@dataclass(frozen=True)
class LoggingConfig:
    path: Path = Path("/var/log/service_status_aggregator/aggregator.log")
    level: str = "INFO"
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 5


@dataclass(frozen=True)
class Config:
    path: Path
    server: ServerConfig
    storage: StorageConfig
    polling: PollingConfig
    registration: RegistrationConfig
    history: HistoryConfig
    display: DisplayConfig
    logging: LoggingConfig
    registration_token: str | None = None

    @property
    def token_enabled(self) -> bool:
        return self.registration_token is not None


# --------------------------------------------------------------------------- helpers


class _Reader:
    """Pulls typed values out of a TOML table, recording problems instead of raising."""

    def __init__(self, data: dict[str, Any], problems: list[str]) -> None:
        self.data = data
        self.problems = problems

    def section(self, name: str, allowed: set[str]) -> dict[str, Any]:
        raw = self.data.get(name, {})
        if not isinstance(raw, dict):
            self.problems.append(f"[{name}] must be a table")
            return {}
        for key in raw:
            if key not in allowed:
                self.problems.append(f"[{name}] unknown key '{key}' (typo?)")
        return raw

    def get(
        self,
        table: dict[str, Any],
        section: str,
        key: str,
        kind: type | tuple[type, ...],
        default: Any,
    ) -> Any:
        if key not in table:
            return default
        value = table[key]
        # bool is a subclass of int; keep them distinct.
        if isinstance(value, bool) and kind is not bool:
            self.problems.append(f"[{section}].{key} must be {_kind_name(kind)}, got bool")
            return default
        if not isinstance(value, kind):
            self.problems.append(
                f"[{section}].{key} must be {_kind_name(kind)}, got {type(value).__name__}"
            )
            return default
        return value


def _kind_name(kind: type | tuple[type, ...]) -> str:
    if isinstance(kind, tuple):
        return " or ".join(k.__name__ for k in kind)
    return kind.__name__


def _check_writable_dir(path: Path, what: str, problems: list[str]) -> None:
    parent = path.parent
    if not parent.is_dir():
        problems.append(f"{what} directory does not exist: {parent}")
    elif not os.access(parent, os.W_OK | os.X_OK):
        problems.append(f"{what} directory is not writable: {parent}")
    elif path.exists() and not os.access(path, os.W_OK):
        problems.append(f"{what} exists but is not writable: {path}")


def find_config_path(cli_path: str | None) -> Path:
    """Resolve the config path: --config, then $SSA_CONFIG, then the system default."""
    if cli_path:
        return Path(cli_path).expanduser()
    env = os.environ.get(CONFIG_ENV_VAR)
    if env:
        return Path(env).expanduser()
    return DEFAULT_CONFIG_PATH


def load_registration_token(environ: dict[str, str] | None = None) -> tuple[str | None, str]:
    """Return (token, source). Precedence: systemd credential, then environment variable.

    The token is never read from the TOML file, so a public config never carries it.
    """
    env = os.environ if environ is None else environ
    cred_dir = env.get("CREDENTIALS_DIRECTORY")
    if cred_dir:
        cred = Path(cred_dir) / TOKEN_CREDENTIAL_NAME
        if cred.is_file():
            value = cred.read_text(encoding="utf-8").strip()
            if value:
                return value, f"systemd credential {cred}"
    value = env.get(TOKEN_ENV_VAR, "").strip()
    if value:
        return value, f"environment variable {TOKEN_ENV_VAR}"
    return None, "not configured"


def load_config(path: Path, environ: dict[str, str] | None = None) -> Config:
    """Load and validate the TOML config at *path*.

    Raises ConfigError listing every problem found. Never returns a partially valid config.
    """
    problems: list[str] = []
    if not path.is_file():
        raise ConfigError([f"config file not found: {path}"], path)
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"config file is not valid TOML: {exc}"], path) from exc
    except OSError as exc:
        raise ConfigError([f"config file could not be read: {exc}"], path) from exc

    for key in data:
        if key not in {
            "server",
            "storage",
            "polling",
            "registration",
            "history",
            "display",
            "logging",
        }:
            problems.append(f"unknown top-level table '{key}'")

    r = _Reader(data, problems)

    # [server]
    t = r.section("server", {"bind", "port"})
    bind = str(r.get(t, "server", "bind", str, "auto")).strip()
    port = r.get(t, "server", "port", int, 8720)
    if bind in FORBIDDEN_BINDS:
        problems.append(
            f"[server].bind = '{bind}' is not allowed: this service never binds to all interfaces"
        )
    elif bind not in {"auto", "tailscale"}:
        try:
            ipaddress.ip_address(bind)
        except ValueError:
            problems.append(
                f"[server].bind must be 'auto', 'tailscale' or an IP address, got '{bind}'"
            )
    if not 1 <= port <= 65535:
        problems.append(f"[server].port must be 1-65535, got {port}")

    # [storage]
    t = r.section("storage", {"db_path"})
    db_path = Path(r.get(t, "storage", "db_path", str, str(StorageConfig().db_path))).expanduser()
    if not db_path.is_absolute():
        problems.append(f"[storage].db_path must be absolute, got {db_path}")
    else:
        _check_writable_dir(db_path, "[storage].db_path", problems)

    # [polling]
    t = r.section("polling", {"interval_seconds", "timeout_seconds", "max_concurrent"})
    interval = float(r.get(t, "polling", "interval_seconds", (int, float), 60))
    timeout = float(r.get(t, "polling", "timeout_seconds", (int, float), 5))
    max_conc = r.get(t, "polling", "max_concurrent", int, 10)
    if not MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS:
        problems.append(
            f"[polling].interval_seconds must be between {MIN_INTERVAL_SECONDS:g} and "
            f"{MAX_INTERVAL_SECONDS:g} (1-5 minutes recommended), got {interval:g}"
        )
    if timeout <= 0:
        problems.append(f"[polling].timeout_seconds must be > 0, got {timeout}")
    if interval > 0 and timeout > 0 and timeout >= interval:
        problems.append(
            f"[polling].timeout_seconds ({timeout}) must be less than interval_seconds ({interval})"
        )
    if max_conc < 1:
        problems.append(f"[polling].max_concurrent must be >= 1, got {max_conc}")

    # [registration]
    t = r.section(
        "registration",
        {
            "staleness_seconds",
            "require_token",
            "allowed_target_cidrs",
            "register_self",
            "self_register_interval_seconds",
        },
    )
    staleness = float(r.get(t, "registration", "staleness_seconds", (int, float), 900))
    require_token = r.get(t, "registration", "require_token", bool, True)
    register_self = r.get(t, "registration", "register_self", bool, True)
    self_interval = float(
        r.get(t, "registration", "self_register_interval_seconds", (int, float), 300)
    )
    raw_cidrs = r.get(
        t, "registration", "allowed_target_cidrs", list, list(DEFAULT_ALLOWED_TARGET_CIDRS)
    )
    cidrs: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in raw_cidrs:
        if not isinstance(item, str):
            problems.append(
                f"[registration].allowed_target_cidrs entries must be strings, got {item!r}"
            )
            continue
        try:
            net = ipaddress.ip_network(item, strict=False)
        except ValueError:
            problems.append(f"[registration].allowed_target_cidrs entry is not a CIDR: '{item}'")
            continue
        if net.prefixlen == 0:
            problems.append(
                f"[registration].allowed_target_cidrs entry '{item}' allows every address; "
                "list specific networks instead"
            )
            continue
        cidrs.append(net)
    if not cidrs and not problems:
        problems.append("[registration].allowed_target_cidrs must list at least one network")
    if staleness <= 0:
        problems.append(f"[registration].staleness_seconds must be > 0, got {staleness}")
    if self_interval <= 0:
        problems.append(
            f"[registration].self_register_interval_seconds must be > 0, got {self_interval}"
        )

    # [history]
    t = r.section("history", {"retention_days"})
    retention = r.get(t, "history", "retention_days", int, 90)
    if retention < 1:
        problems.append(f"[history].retention_days must be >= 1, got {retention}")

    # [display]
    t = r.section(
        "display", {"history_days", "timezone", "major_outage_minutes", "degraded_response_ms"}
    )
    history_days = r.get(t, "display", "history_days", int, 90)
    tz_name = str(r.get(t, "display", "timezone", str, "UTC")).strip()
    major_minutes = float(r.get(t, "display", "major_outage_minutes", (int, float), 30))
    degraded_ms = float(r.get(t, "display", "degraded_response_ms", (int, float), 1000))
    if not 1 <= history_days <= 365:
        problems.append(f"[display].history_days must be 1-365, got {history_days}")
    elif history_days > retention:
        problems.append(
            f"[display].history_days ({history_days}) cannot exceed "
            f"[history].retention_days ({retention})"
        )
    try:
        zoneinfo.ZoneInfo(tz_name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        problems.append(f"[display].timezone is not a known IANA zone: '{tz_name}'")
    if major_minutes <= 0:
        problems.append(f"[display].major_outage_minutes must be > 0, got {major_minutes:g}")
    if degraded_ms <= 0:
        problems.append(f"[display].degraded_response_ms must be > 0, got {degraded_ms:g}")

    # [logging]
    t = r.section("logging", {"path", "level", "max_bytes", "backup_count"})
    log_path = Path(r.get(t, "logging", "path", str, str(LoggingConfig().path))).expanduser()
    level = str(r.get(t, "logging", "level", str, "INFO")).upper()
    max_bytes = r.get(t, "logging", "max_bytes", int, LoggingConfig().max_bytes)
    backup_count = r.get(t, "logging", "backup_count", int, 5)
    if not log_path.is_absolute():
        problems.append(f"[logging].path must be absolute, got {log_path}")
    else:
        _check_writable_dir(log_path, "[logging].path", problems)
    if level not in LOG_LEVELS:
        problems.append(f"[logging].level must be one of {', '.join(LOG_LEVELS)}, got '{level}'")
    if max_bytes < 65536:
        problems.append(f"[logging].max_bytes must be >= 65536, got {max_bytes}")
    if backup_count < 1:
        problems.append(f"[logging].backup_count must be >= 1, got {backup_count}")

    # Secret
    token, _source = load_registration_token(environ)
    if token is not None and len(token) < MIN_TOKEN_LENGTH:
        problems.append(
            f"registration token is shorter than {MIN_TOKEN_LENGTH} characters; refusing to use it"
        )
        token = None
    if require_token and token is None:
        problems.append(
            "[registration].require_token is true but no token was found "
            f"(set {TOKEN_ENV_VAR} or provide the systemd credential '{TOKEN_CREDENTIAL_NAME}')"
        )

    if problems:
        raise ConfigError(problems, path)

    return Config(
        path=path,
        server=ServerConfig(bind=bind, port=port),
        storage=StorageConfig(db_path=db_path),
        polling=PollingConfig(interval, timeout, max_conc),
        registration=RegistrationConfig(
            staleness_seconds=staleness,
            require_token=require_token,
            allowed_target_cidrs=tuple(cidrs),
            register_self=register_self,
            self_register_interval_seconds=self_interval,
        ),
        history=HistoryConfig(retention_days=retention),
        display=DisplayConfig(history_days, tz_name, major_minutes, degraded_ms),
        logging=LoggingConfig(log_path, level, max_bytes, backup_count),
        registration_token=token,
    )


def redacted_view(cfg: Config, bind_resolved: str | None = None) -> dict[str, Any]:
    """A dict of the effective configuration safe to render; the token is never included."""
    return {
        "config_path": str(cfg.path),
        "server": {
            "bind": cfg.server.bind,
            "bind_resolved": bind_resolved,
            "port": cfg.server.port,
        },
        "storage": {"db_path": str(cfg.storage.db_path)},
        "polling": {
            "interval_seconds": cfg.polling.interval_seconds,
            "timeout_seconds": cfg.polling.timeout_seconds,
            "max_concurrent": cfg.polling.max_concurrent,
        },
        "registration": {
            "staleness_seconds": cfg.registration.staleness_seconds,
            "require_token": cfg.registration.require_token,
            "token": "***" if cfg.token_enabled else "(disabled)",
            "allowed_target_cidrs": [str(n) for n in cfg.registration.allowed_target_cidrs],
            "register_self": cfg.registration.register_self,
            "self_register_interval_seconds": cfg.registration.self_register_interval_seconds,
        },
        "history": {"retention_days": cfg.history.retention_days},
        "display": {
            "history_days": cfg.display.history_days,
            "timezone": cfg.display.timezone,
            "major_outage_minutes": cfg.display.major_outage_minutes,
            "degraded_response_ms": cfg.display.degraded_response_ms,
        },
        "logging": {
            "path": str(cfg.logging.path),
            "level": cfg.logging.level,
            "max_bytes": cfg.logging.max_bytes,
            "backup_count": cfg.logging.backup_count,
        },
    }
