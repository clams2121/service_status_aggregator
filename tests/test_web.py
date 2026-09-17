from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from service_status_aggregator.config import load_config
from service_status_aggregator.storage import STATUS_DOWN, STATUS_UP, Storage, utcnow
from service_status_aggregator.web.app import RuntimeState, create_app
from service_status_aggregator.web.views import banner, build_views, humanize_age, summarise
from tests.conftest import write_config

TOKEN = "test-token-0123456789abcdef"


def record(name: str, **extra: object) -> dict:
    return {
        "name": name,
        "host": "100.100.100.100",
        "port": 80,
        "health_url": "http://100.100.100.100/health",
        "log_path": "/var/log/x.log",
        "config_page_url": "http://100.100.100.100/settings",
    } | extra


@pytest.fixture
def state(tmp_path: Path) -> RuntimeState:
    cfg = load_config(
        write_config(tmp_path, extra='[display]\ntimezone = "Europe/London"\n'),
        environ={"SSA_REGISTRATION_TOKEN": TOKEN},
    )
    storage = Storage(cfg.storage.db_path)
    storage.migrate()
    now = utcnow()
    up, _ = storage.upsert_registration(record("alpha"), now=now - timedelta(days=3))
    storage.upsert_registration(record("alpha"), now=now)  # fresh again, first seen 3 days ago
    down, _ = storage.upsert_registration(record("bravo"), now=now)
    slow, _ = storage.upsert_registration(record("echo"), now=now)
    storage.upsert_registration(record("charlie"), now=now - timedelta(hours=2))  # stale
    storage.upsert_registration(record("delta", log_path="", config_page_url=""), now=now)
    # alpha: up for 3 days with a 20-minute blip yesterday
    storage.record_check(
        up.id,
        status=STATUS_UP,
        checked_at=now - timedelta(days=3),
        response_ms=4,
        failure_reason=None,
    )
    storage.record_check(
        up.id,
        status=STATUS_DOWN,
        checked_at=now - timedelta(days=1, hours=2),
        response_ms=None,
        failure_reason="http_502",
    )
    storage.record_check(
        up.id,
        status=STATUS_UP,
        checked_at=now - timedelta(days=1, hours=2) + timedelta(minutes=20),
        response_ms=4.2,
        failure_reason=None,
    )
    storage.record_check(
        down.id, status=STATUS_DOWN, checked_at=now, response_ms=None, failure_reason="http_503"
    )
    storage.record_check(
        slow.id, status=STATUS_UP, checked_at=now, response_ms=2500.0, failure_reason=None
    )
    st = RuntimeState(cfg=cfg, storage=storage)
    st.bind_ip, st.bind_kind = "100.100.100.1", "tailscale"
    return st


@pytest.fixture
async def client(state: RuntimeState):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(state)), base_url="http://t"
    ) as c:
        yield c


async def test_index_rows_order_and_history(client: httpx.AsyncClient) -> None:
    r = await client.get("/")
    assert r.status_code == 200
    html = r.text
    for name in ("alpha", "bravo", "charlie", "delta", "echo"):
        assert html.count(f'<span class="svc-name">{name}</span>') == 1
    # down, then degraded (slow echo), then unchecked (charlie, delta), then up alpha
    order = [
        html.index(f'<span class="svc-name">{n}</span>')
        for n in ("bravo", "echo", "charlie", "delta", "alpha")
    ]
    assert order == sorted(order)
    assert "1 service down" in html
    assert 'class="status-word down"' in html and "http_503" in html
    assert "registration stale" in html and "slow response: 2500 ms" in html
    assert html.count('class="cell ') >= 5 * 90
    assert 'class="cell minor"' in html  # alpha's blip yesterday
    assert "Down 20 min (1 incident)" in html and "http_502" in html
    assert "% uptime" in html
    assert 'rel="noopener noreferrer"' in html
    assert r.headers["Content-Security-Policy"].startswith("default-src 'self'")
    assert 'http-equiv="refresh" content="30"' in html
    assert "Europe/London" in html
    assert "127.0.0.1" not in html  # no loopback warning when bound to tailscale


async def test_index_escapes_untrusted_fields(
    state: RuntimeState, client: httpx.AsyncClient
) -> None:
    state.storage.upsert_registration(record("xss", log_path="/tmp/<script>alert(1)</script>"))
    html = (await client.get("/")).text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


async def test_api_services(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/services")).json()
    assert body["counts"] == {
        "total": 5,
        "up": 1,
        "degraded": 1,
        "down": 1,
        "unknown": 2,
        "stale": 1,
    }
    assert body["overall"] == "1 service down"
    names = [s["name"] for s in body["services"]]
    assert names[0] == "bravo" and names[-1] == "alpha"
    alpha = next(s for s in body["services"] if s["name"] == "alpha")
    assert len(alpha["history"]) == 90
    assert alpha["history"][-2]["status"] == "minor" and alpha["history"][-2]["incidents"] == 1
    assert alpha["history"][0]["status"] == "nodata"
    assert 99.0 < alpha["uptime_pct"] < 100.0
    charlie = next(s for s in body["services"] if s["name"] == "charlie")
    assert charlie["stale"] is True and charlie["live"] == "unknown"


async def test_config_page_redacts_token(client: httpx.AsyncClient) -> None:
    r = await client.get("/config")
    assert r.status_code == 200
    assert TOKEN not in r.text
    assert "***" in r.text
    assert "100.64.0.0/10" in r.text
    assert "Europe/London" in r.text


async def test_warning_banners(state: RuntimeState, client: httpx.AsyncClient) -> None:
    state.bind_kind, state.bind_ip = "loopback", "127.0.0.1"
    html = (await client.get("/")).text
    assert "ssh -L 8720:127.0.0.1:8720" in html


async def test_static_css_served(client: httpx.AsyncClient) -> None:
    r = await client.get("/static/style.css")
    assert r.status_code == 200 and "text/css" in r.headers["content-type"]


def test_humanize_age() -> None:
    assert humanize_age(None) == "never"
    assert humanize_age(5) == "5 s ago"
    assert humanize_age(125) == "2 min ago"
    assert humanize_age(3700) == "1 h 1 min ago"
    assert humanize_age(90000) == "1 d 1 h ago"


def test_views_live_states_and_banner(state: RuntimeState) -> None:
    views = build_views(state.storage.list_services(), state.cfg)
    live = {v.row.name: v.live for v in views}
    assert live == {
        "bravo": "down",
        "charlie": "unknown",
        "echo": "degraded",
        "delta": "unknown",
        "alpha": "up",
    }
    assert [v.live for v in views] == ["down", "degraded", "unknown", "unknown", "up"]
    counts = summarise(views)
    assert banner(counts).level == "bad"
    assert (
        banner({"total": 0, "up": 0, "degraded": 0, "down": 0, "unknown": 0, "stale": 0}).level
        == "none"
    )
    assert (
        banner({"total": 2, "up": 2, "degraded": 0, "down": 0, "unknown": 0, "stale": 0}).text
        == "All systems operational"
    )
    assert (
        banner({"total": 2, "up": 1, "degraded": 1, "down": 0, "unknown": 0, "stale": 0}).level
        == "warn"
    )
