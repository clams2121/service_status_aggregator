from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from service_status_aggregator.storage import STATUS_DOWN, STATUS_UP, Storage, utcnow

RECORD = {
    "name": "svc",
    "host": "100.100.100.100",
    "port": 80,
    "health_url": "http://100.100.100.100/health",
    "log_path": "",
    "config_page_url": "",
}


def test_upsert_preserves_first_registration(tmp_path: Path) -> None:
    st = Storage(tmp_path / "db.sqlite")
    st.migrate()
    t0 = utcnow() - timedelta(minutes=10)
    row, created = st.upsert_registration(RECORD, now=t0)
    assert created and row.registration_count == 1
    row2, created2 = st.upsert_registration(RECORD | {"port": 81}, now=utcnow())
    assert not created2
    assert row2.port == 81
    assert row2.first_registered_at == t0
    assert row2.registration_count == 2
    assert len(st.list_services()) == 1


def test_record_check_logs_transitions_only(tmp_path: Path) -> None:
    st = Storage(tmp_path / "db.sqlite")
    st.migrate()
    row, _ = st.upsert_registration(RECORD)
    now = utcnow()
    assert (
        st.record_check(
            row.id, status=STATUS_UP, checked_at=now, response_ms=12.5, failure_reason=None
        )
        == "unknown"
    )
    assert (
        st.record_check(
            row.id, status=STATUS_UP, checked_at=now, response_ms=13.0, failure_reason=None
        )
        is None
    )
    assert (
        st.record_check(
            row.id, status=STATUS_DOWN, checked_at=now, response_ms=None, failure_reason="http_500"
        )
        == "up"
    )
    events = st.list_events(row.id)
    assert [(e.from_status, e.to_status) for e in events] == [("up", "down"), ("unknown", "up")]
    svc = st.get_service("svc", "100.100.100.100")
    assert svc is not None and svc.last_status == "down" and svc.last_failure_reason == "http_500"


def test_prune_and_remove(tmp_path: Path) -> None:
    st = Storage(tmp_path / "db.sqlite")
    st.migrate()
    row, _ = st.upsert_registration(RECORD)
    old = utcnow() - timedelta(days=100)
    st.record_check(row.id, status=STATUS_UP, checked_at=old, response_ms=1.0, failure_reason=None)
    assert st.prune_events(utcnow() - timedelta(days=90)) == 1
    assert st.remove("svc", "100.100.100.100") is True
    assert st.remove("svc", "100.100.100.100") is False
    assert st.list_services() == []


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    st = Storage(tmp_path / "db.sqlite")
    assert st.migrate() == 1
    assert st.migrate() == 1
    assert st.ping()
