"""Presentation helpers: staleness, ordering and relative times for the status page."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from service_status_aggregator.storage import STATUS_DOWN, STATUS_UP, ServiceRow, to_iso, utcnow

# Lower sorts first: down, then stale, then never checked, then up.
_ORDER = {"down": 0, "stale": 1, "unknown": 2, "up": 3}


def humanize_age(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    s = int(seconds)
    if s < 0:
        s = 0
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

    @property
    def health(self) -> str:
        return self.row.last_status

    @property
    def registration(self) -> str:
        return "stale" if self.stale else "fresh"

    @property
    def group(self) -> str:
        if self.row.last_status == STATUS_DOWN:
            return "down"
        if self.stale:
            return "stale"
        if self.row.last_status == STATUS_UP:
            return "up"
        return "unknown"

    @property
    def sort_key(self) -> tuple[int, str, str]:
        return (_ORDER[self.group], self.row.name, self.row.host)

    @property
    def registered_ago(self) -> str:
        return humanize_age(self.registered_age_s)

    @property
    def checked_ago(self) -> str:
        return humanize_age(self.checked_age_s)

    @property
    def checked_at_iso(self) -> str:
        return to_iso(self.row.last_checked_at) if self.row.last_checked_at else ""

    @property
    def registered_at_iso(self) -> str:
        return to_iso(self.row.last_registered_at)

    @property
    def address(self) -> str:
        host = f"[{self.row.host}]" if ":" in self.row.host else self.row.host
        return f"{host}:{self.row.port}"

    def to_dict(self) -> dict[str, Any]:
        data = self.row.to_dict()
        data["stale"] = self.stale
        data["group"] = self.group
        return data


def build_views(
    rows: list[ServiceRow], staleness_seconds: float, now: datetime | None = None
) -> list[ServiceView]:
    now = now or utcnow()
    views: list[ServiceView] = []
    for row in rows:
        reg_age = (now - row.last_registered_at).total_seconds()
        chk_age = (now - row.last_checked_at).total_seconds() if row.last_checked_at else None
        views.append(
            ServiceView(
                row=row,
                stale=reg_age > staleness_seconds,
                registered_age_s=reg_age,
                checked_age_s=chk_age,
            )
        )
    return sorted(views, key=lambda v: v.sort_key)


def summarise(views: list[ServiceView]) -> dict[str, int]:
    counts = {"total": len(views), "up": 0, "down": 0, "unknown": 0, "stale": 0}
    for v in views:
        counts[v.row.last_status] += 1
        if v.stale:
            counts["stale"] += 1
    return counts
