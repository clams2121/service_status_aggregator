from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from service_status_aggregator.storage import (
    STATUS_DOWN,
    STATUS_UNKNOWN,
    STATUS_UP,
    ServiceRow,
    StatusEvent,
    Storage,
    utcnow,
)
from service_status_aggregator.web.history import (
    CELL_MAJOR,
    CELL_MINOR,
    CELL_NODATA,
    CELL_OK,
    build_history,
    fmt_duration,
)

UTC = ZoneInfo("UTC")
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def svc(first_registered: datetime) -> ServiceRow:
    return ServiceRow(
        id=1,
        name="s",
        host="h",
        port=1,
        health_url="http://h/health",
        log_path="",
        config_page_url="",
        first_registered_at=first_registered,
        last_registered_at=NOW,
        registration_count=1,
        source="register",
        last_status="up",
        last_checked_at=NOW,
        last_response_ms=1.0,
        last_failure_reason=None,
        last_status_change_at=None,
    )


def ev(at: datetime, frm: str, to: str, reason: str | None = None) -> StatusEvent:
    return StatusEvent(0, 1, at, frm, to, reason)


def test_clean_history_is_all_green_after_registration() -> None:
    registered = NOW - timedelta(days=10)
    events = [ev(registered + timedelta(seconds=30), STATUS_UNKNOWN, STATUS_UP)]
    h = build_history(svc(registered), events, None, days=30, tz=UTC, major_outage_s=1800, now=NOW)
    assert len(h.cells) == 30
    statuses = [c.status for c in h.cells]
    assert statuses[:19] == [CELL_NODATA] * 19
    assert statuses[19:] == [CELL_OK] * 11
    assert h.cells[-1].is_today
    assert h.uptime_pct == 100.0
    assert "No data" in h.cells[0].tooltip()
    assert "No incidents" in h.cells[-1].tooltip()


def test_minor_and_major_outages_and_midnight_crossing() -> None:
    registered = NOW - timedelta(days=40)
    d = NOW.replace(hour=0, minute=0)
    events = [
        ev(registered, STATUS_UNKNOWN, STATUS_UP),
        # 10 minutes down five days ago -> minor
        ev(d - timedelta(days=5, hours=-3), STATUS_UP, STATUS_DOWN, "http_503"),
        ev(d - timedelta(days=5, hours=-3, minutes=-10), STATUS_DOWN, STATUS_UP),
        # 23:30 two days ago until 01:00 yesterday -> 30 min (major) + 60 min (major), one incident
        ev(
            d - timedelta(days=2) + timedelta(hours=23, minutes=30),
            STATUS_UP,
            STATUS_DOWN,
            "timeout_after_5s",
        ),
        ev(d - timedelta(days=1) + timedelta(hours=1), STATUS_DOWN, STATUS_UP),
    ]
    h = build_history(svc(registered), events, None, days=10, tz=UTC, major_outage_s=1800, now=NOW)
    cells = {c.day: c for c in h.cells}
    minor = cells[(d - timedelta(days=5)).date()]
    assert minor.status == CELL_MINOR and round(minor.down_s) == 600 and minor.incidents == 1
    assert "http_503" in minor.tooltip() and "Down 10 min (1 incident)" in minor.tooltip()
    two_ago = cells[(d - timedelta(days=2)).date()]
    yesterday = cells[(d - timedelta(days=1)).date()]
    assert two_ago.status == CELL_MAJOR and round(two_ago.down_s) == 1800 and two_ago.incidents == 1
    assert (
        yesterday.status == CELL_MAJOR
        and round(yesterday.down_s) == 3600
        and yesterday.incidents == 0
    )
    assert cells[d.date()].status == CELL_OK
    assert h.uptime_pct is not None and 99.0 < h.uptime_pct < 100.0


def test_initial_status_before_window_and_unknown_gap() -> None:
    registered = NOW - timedelta(days=100)
    d = NOW.replace(hour=0, minute=0)
    # down since before the window until 3 days ago, then a monitoring gap yesterday
    events = [
        ev(d - timedelta(days=3), STATUS_DOWN, STATUS_UP),
        ev(d - timedelta(days=1), STATUS_UP, STATUS_UNKNOWN, "aggregator not running"),
        ev(d - timedelta(days=1, hours=-6), STATUS_UNKNOWN, STATUS_UP),
    ]
    h = build_history(
        svc(registered), events, STATUS_DOWN, days=7, tz=UTC, major_outage_s=1800, now=NOW
    )
    statuses = [c.status for c in h.cells]
    assert statuses[:3] == [CELL_MAJOR] * 3  # 6, 5 and 4 days ago fully down
    assert statuses[3] == CELL_OK
    assert h.cells[0].incidents == 0  # began before the window
    yesterday = h.cells[-2]
    assert yesterday.status == CELL_OK and round(yesterday.unknown_s) == 6 * 3600
    assert "6 h 0 min unmonitored" in yesterday.tooltip()


def test_timezone_day_boundaries() -> None:
    tz = ZoneInfo("America/New_York")
    registered = NOW - timedelta(days=5)
    # 02:00 UTC today is still "yesterday" in New York
    events = [
        ev(registered, STATUS_UNKNOWN, STATUS_UP),
        ev(NOW.replace(hour=2), STATUS_UP, STATUS_DOWN, "http_500"),
        ev(NOW.replace(hour=2, minute=5), STATUS_DOWN, STATUS_UP),
    ]
    h = build_history(svc(registered), events, None, days=3, tz=tz, major_outage_s=1800, now=NOW)
    assert h.cells[-2].status == CELL_MINOR and h.cells[-1].status == CELL_OK


def test_fmt_duration() -> None:
    assert fmt_duration(42) == "42 s"
    assert fmt_duration(600) == "10 min"
    assert fmt_duration(5400) == "1 h 30 min"
    assert fmt_duration(90000) == "1 d 1 h"


def test_storage_history_queries_and_gap_marking(tmp_path: Path) -> None:
    st = Storage(tmp_path / "db.sqlite")
    st.migrate()
    row, _ = st.upsert_registration(
        {
            "name": "a",
            "host": "h",
            "port": 1,
            "health_url": "http://h/",
            "log_path": "",
            "config_page_url": "",
        }
    )
    t0 = utcnow() - timedelta(days=2)
    st.record_check(row.id, status=STATUS_UP, checked_at=t0, response_ms=1, failure_reason=None)
    st.record_check(
        row.id,
        status=STATUS_DOWN,
        checked_at=t0 + timedelta(hours=1),
        response_ms=None,
        failure_reason="x",
    )
    assert st.status_before(t0 + timedelta(minutes=30)) == {row.id: STATUS_UP}
    assert st.status_before(t0 - timedelta(minutes=1)) == {}
    since = st.events_since(t0 + timedelta(minutes=30))
    assert [e.to_status for e in since[row.id]] == [STATUS_DOWN]
    assert st.mark_unknown(row.id, at=utcnow(), reason="gap") is True
    assert st.mark_unknown(row.id, at=utcnow(), reason="gap") is False
    assert st.get_service("a", "h").last_status == STATUS_UNKNOWN
    assert st.get_meta("last_cycle_at") is None
    st.set_meta("last_cycle_at", "x")
    st.set_meta("last_cycle_at", "y")
    assert st.get_meta("last_cycle_at") == "y"
