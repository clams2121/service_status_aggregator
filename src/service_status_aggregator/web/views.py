"""Presentation helpers: live status, staleness, ordering and relative times."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from service_status_aggregator.config import Config
from service_status_aggregator.storage import STATUS_DOWN, STATUS_UP, ServiceRow, to_iso, utcnow
from service_status_aggregator.web.history import History

LIVE_UP = "up"
LIVE_DEGRADED = "degraded"
LIVE_DOWN = "down"
LIVE_UNKNOWN = "unknown"

# Lower sorts first.
_ORDER = {LIVE_DOWN: 0, LIVE_DEGRADED: 1, LIVE_UNKNOWN: 2, LIVE_UP: 3}
_LABEL = {
    LIVE_UP: "Operational",
    LIVE_DEGRADED: "Degraded",
    LIVE_DOWN: "Down",
    LIVE_UNKNOWN: "Unchecked",
}


def humanize_age(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} s ago"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h {(s % 3600) // 60} min ago"
    return f"{s // 86400} d {(s % 86400) // 3600} h ago"


@dataclass(frozen=True)
class ServiceView:
    row: ServiceRow
    stale: bool
    registered_age_s: float
    checked_age_s: float | None
    degraded_response_ms: float
    tz: ZoneInfo
    history: History | None = None

    # -- live status --------------------------------------------------------

    @property
    def slow(self) -> bool:
        return (
            self.row.last_status == STATUS_UP
            and self.row.last_response_ms is not None
            and self.row.last_response_ms >= self.degraded_response_ms
        )

    @property
    def live(self) -> str:
        if self.row.last_status == STATUS_DOWN:
            return LIVE_DOWN
        if self.row.last_status == STATUS_UP:
            return LIVE_DEGRADED if (self.slow or self.stale) else LIVE_UP
        return LIVE_UNKNOWN

    @property
    def live_label(self) -> str:
        return _LABEL[self.live]

    @property
    def live_detail(self) -> str:
        """One line explaining the live status; shown on hover and beside the label."""
        parts: list[str] = []
        if self.row.last_status == STATUS_DOWN:
            parts.append(self.row.last_failure_reason or "health check failed")
        elif self.row.last_status == STATUS_UP:
            if self.slow:
                parts.append(f"slow response: {self.row.last_response_ms:.0f} ms")
        else:
            parts.append(self.row.last_failure_reason or "no health check yet")
        if self.stale and self.row.last_status != STATUS_DOWN:
            parts.append(f"registration stale: last seen {self.registered_ago}")
        return "; ".join(parts)

    @property
    def registration(self) -> str:
        return "stale" if self.stale else "fresh"

    @property
    def sort_key(self) -> tuple[int, str, str]:
        return (_ORDER[self.live], self.row.name, self.row.host)

    # -- times ---------------------------------------------------------------

    @property
    def registered_ago(self) -> str:
        return humanize_age(self.registered_age_s)

    @property
    def checked_ago(self) -> str:
        return humanize_age(self.checked_age_s)

    def _local(self, dt: datetime | None) -> str:
        return dt.astimezone(self.tz).strftime("%Y-%m-%d %H:%M:%S %Z") if dt else ""

    @property
    def checked_at_local(self) -> str:
        return self._local(self.row.last_checked_at)

    @property
    def registered_at_local(self) -> str:
        return self._local(self.row.last_registered_at)

    @property
    def address(self) -> str:
        host = f"[{self.row.host}]" if ":" in self.row.host else self.row.host
        return f"{host}:{self.row.port}"

    def to_dict(self) -> dict[str, Any]:
        data = self.row.to_dict()
        data["stale"] = self.stale
        data["live"] = self.live
        data["live_detail"] = self.live_detail
        if self.history is not None:
            data["uptime_pct"] = (
                None if self.history.uptime_pct is None else round(self.history.uptime_pct, 3)
            )
            data["history"] = [c.to_dict() for c in self.history.cells]
        return data


def build_views(
    rows: list[ServiceRow],
    cfg: Config,
    now: datetime | None = None,
    histories: dict[int, History] | None = None,
) -> list[ServiceView]:
    now = now or utcnow()
    tz = ZoneInfo(cfg.display.timezone)
    views: list[ServiceView] = []
    for row in rows:
        reg_age = (now - row.last_registered_at).total_seconds()
        chk_age = (now - row.last_checked_at).total_seconds() if row.last_checked_at else None
        views.append(
            ServiceView(
                row=row,
                stale=reg_age > cfg.registration.staleness_seconds,
                registered_age_s=reg_age,
                checked_age_s=chk_age,
                degraded_response_ms=cfg.display.degraded_response_ms,
                tz=tz,
                history=(histories or {}).get(row.id),
            )
        )
    return sorted(views, key=lambda v: v.sort_key)


def summarise(views: list[ServiceView]) -> dict[str, int]:
    counts = {"total": len(views), "up": 0, "degraded": 0, "down": 0, "unknown": 0, "stale": 0}
    for v in views:
        counts[v.live] += 1
        if v.stale:
            counts["stale"] += 1
    return counts


@dataclass(frozen=True)
class Banner:
    level: str  # ok | warn | bad | none
    text: str


def banner(counts: dict[str, int]) -> Banner:
    if counts["total"] == 0:
        return Banner("none", "No services registered yet")
    if counts["down"]:
        n = counts["down"]
        return Banner("bad", f"{n} service{'s' if n != 1 else ''} down")
    if counts["degraded"]:
        n = counts["degraded"]
        return Banner("warn", f"{n} service{'s' if n != 1 else ''} degraded")
    if counts["unknown"]:
        n = counts["unknown"]
        return Banner("warn", f"{n} service{'s' if n != 1 else ''} awaiting first check")
    return Banner("ok", "All systems operational")


def rendered_at(now: datetime, tz: ZoneInfo) -> str:
    return now.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z")


__all__ = [
    "Banner",
    "ServiceView",
    "banner",
    "build_views",
    "humanize_age",
    "rendered_at",
    "summarise",
    "to_iso",
]
