"""Per-day uptime history reconstructed from status transitions.

Each service gets one cell per calendar day (in the configured timezone). A cell is
green when the service was up all day, yellow when it was down for less than the
major-outage threshold, red at or above it, and grey when there is no data (before the
service registered, or while the aggregator itself was not running).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from service_status_aggregator.storage import (
    STATUS_DOWN,
    STATUS_UNKNOWN,
    STATUS_UP,
    ServiceRow,
    StatusEvent,
    Storage,
)

CELL_OK = "ok"
CELL_MINOR = "minor"
CELL_MAJOR = "major"
CELL_NODATA = "nodata"


@dataclass
class DayCell:
    day: date
    status: str = CELL_NODATA
    down_s: float = 0.0
    unknown_s: float = 0.0
    monitored_s: float = 0.0
    incidents: int = 0
    reasons: list[str] = field(default_factory=list)
    is_today: bool = False

    @property
    def uptime_pct(self) -> float | None:
        if self.monitored_s <= 0:
            return None
        return max(0.0, 100.0 * (1 - self.down_s / self.monitored_s))

    def tooltip(self) -> str:
        head = self.day.strftime("%a %d %b %Y") + (" (today)" if self.is_today else "")
        if self.status == CELL_NODATA:
            return f"{head}\nNo data"
        if self.down_s <= 0:
            note = "No incidents"
            if self.unknown_s > 0:
                note += f" ({fmt_duration(self.unknown_s)} unmonitored)"
            return f"{head}\n{note}"
        n = self.incidents
        lines = [head, f"Down {fmt_duration(self.down_s)} ({n} incident{'s' if n != 1 else ''})"]
        if self.unknown_s > 0:
            lines.append(f"{fmt_duration(self.unknown_s)} unmonitored")
        lines.extend(f"• {r}" for r in self.reasons[:3])
        if len(self.reasons) > 3:
            lines.append(f"• and {len(self.reasons) - 3} more")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, object]:
        return {
            "day": self.day.isoformat(),
            "status": self.status,
            "down_seconds": round(self.down_s),
            "unknown_seconds": round(self.unknown_s),
            "incidents": self.incidents,
            "reasons": self.reasons[:5],
        }


@dataclass(frozen=True)
class History:
    cells: list[DayCell]
    uptime_pct: float | None  # over the monitored part of the window

    @property
    def uptime_label(self) -> str:
        if self.uptime_pct is None:
            return "no data yet"
        return f"{self.uptime_pct:.2f} % uptime"


def fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s} s"
    h, rem = divmod(s, 3600)
    m = rem // 60
    if h == 0:
        return f"{m} min"
    d, h = divmod(h, 24)
    if d == 0:
        return f"{h} h {m} min"
    return f"{d} d {h} h"


Segment = tuple[datetime, datetime, str, str | None, bool]


def _segments(
    initial: str,
    events: list[StatusEvent],
    window_start: datetime,
    window_end: datetime,
) -> list[Segment]:
    """Piecewise-constant status over the window.

    Each segment is (start, end, status, reason, began_at_event); the last flag is False
    only for the carried-over state at the start of the window.
    """
    segs: list[Segment] = []
    cursor, status, reason, began = window_start, initial, None, False
    for ev in events:
        at = max(ev.at, window_start)
        if at >= window_end:
            break
        if at > cursor:
            segs.append((cursor, at, status, reason, began))
        cursor, status, began = at, ev.to_status, True
        reason = ev.reason if ev.to_status != STATUS_UP else None
    if cursor < window_end:
        segs.append((cursor, window_end, status, reason, began))
    return segs


def build_history(
    service: ServiceRow,
    events: list[StatusEvent],
    initial_status: str | None,
    *,
    days: int,
    tz: ZoneInfo,
    major_outage_s: float,
    now: datetime,
) -> History:
    """Reconstruct per-day cells for one service.

    *initial_status* is the status in force just before the window (from the last event
    before it), or None if the service has no events before the window.
    """
    local_now = now.astimezone(tz)
    today = local_now.date()
    first_day = today - timedelta(days=days - 1)
    window_start = datetime.combine(first_day, datetime.min.time(), tzinfo=tz)
    window_end = now

    # Before the service registered there is nothing to say; before its first poll, unknown.
    registered_at = service.first_registered_at
    initial = initial_status or STATUS_UNKNOWN
    segs = _segments(initial, events, max(window_start, registered_at), window_end)

    cells = [DayCell(first_day + timedelta(days=i)) for i in range(days)]
    by_day = {c.day: c for c in cells}
    cells[-1].is_today = True

    for seg_start, seg_end, status, reason, began_at_event in segs:
        cur = seg_start
        while cur < seg_end:
            local = cur.astimezone(tz)
            day_end = datetime.combine(
                local.date() + timedelta(days=1), datetime.min.time(), tzinfo=tz
            )
            piece_end = min(seg_end, day_end)
            cell = by_day.get(local.date())
            if cell is not None:
                span = (piece_end - cur).total_seconds()
                if status == STATUS_DOWN:
                    cell.down_s += span
                    cell.monitored_s += span
                    if began_at_event and cur == seg_start:  # once, on the day it began
                        cell.incidents += 1
                    if reason and reason not in cell.reasons:
                        cell.reasons.append(reason)
                elif status == STATUS_UP:
                    cell.monitored_s += span
                else:
                    cell.unknown_s += span
            cur = piece_end

    total_monitored = 0.0
    total_down = 0.0
    for cell in cells:
        if cell.monitored_s <= 0:
            cell.status = CELL_NODATA
        elif cell.down_s >= major_outage_s:
            cell.status = CELL_MAJOR
        elif cell.down_s > 0:
            cell.status = CELL_MINOR
        else:
            cell.status = CELL_OK
        total_monitored += cell.monitored_s
        total_down += cell.down_s

    uptime = None if total_monitored <= 0 else max(0.0, 100.0 * (1 - total_down / total_monitored))
    return History(cells=cells, uptime_pct=uptime)


def window_start(now: datetime, days: int, tz: ZoneInfo) -> datetime:
    """Local midnight at the start of the first day shown."""
    first_day = now.astimezone(tz).date() - timedelta(days=days - 1)
    return datetime.combine(first_day, datetime.min.time(), tzinfo=tz)


def build_histories(
    storage: Storage,
    services: list[ServiceRow],
    *,
    days: int,
    tz: ZoneInfo,
    major_outage_s: float,
    now: datetime,
) -> dict[int, History]:
    """Load events once and build a History for every service."""
    start = window_start(now, days, tz)
    events = storage.events_since(start)
    before = storage.status_before(start)
    return {
        svc.id: build_history(
            svc,
            events.get(svc.id, []),
            before.get(svc.id),
            days=days,
            tz=tz,
            major_outage_s=major_outage_s,
            now=now,
        )
        for svc in services
    }
