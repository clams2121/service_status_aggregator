from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from service_status_aggregator.config import load_config
from service_status_aggregator.storage import STATUS_DOWN, STATUS_UP, Storage, utcnow
from service_status_aggregator.web.app import RuntimeState, create_app
from service_status_aggregator.web.views import build_views, humanize_age
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
    cfg = load_config(write_config(tmp_path), environ={"SSA_REGISTRATION_TOKEN": TOKEN})
    storage = Storage(cfg.storage.db_path)
    storage.migrate()
    now = utcnow()
    up, _ = storage.upsert_registration(record("alpha"), now=now)
    down, _ = storage.upsert_registration(record("bravo"), now=now)
    storage.upsert_registration(record("charlie"), now=now - timedelta(hours=2))  # stale
    storage.upsert_registration(record("delta", log_path="", config_page_url=""), now=now)
    storage.record_check(
        up.id, status=STATUS_UP, checked_at=now, response_ms=4.2, failure_reason=None
    )
    storage.record_check(
        down.id, status=STATUS_DOWN, checked_at=now, response_ms=None, failure_reason="http_503"
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


async def test_index_lists_each_service_once_in_order(client: httpx.AsyncClient) -> None:
    r = await client.get("/")
    assert r.status_code == 200
    html = r.text
    for name in ("alpha", "bravo", "charlie", "delta"):
        assert html.count(f"\n        {name}\n") == 1
    assert html.index("bravo") < html.index("charlie") < html.index("delta") < html.index("alpha")
    assert 'class="badge stale"' in html
    assert "http_503" in html
    assert 'rel="noopener noreferrer"' in html
    assert r.headers["Content-Security-Policy"].startswith("default-src 'self'")
    assert 'http-equiv="refresh"' in html
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
    assert body["counts"] == {"total": 4, "up": 1, "down": 1, "unknown": 2, "stale": 1}
    names = [s["name"] for s in body["services"]]
    assert names == ["bravo", "charlie", "delta", "alpha"]
    charlie = next(s for s in body["services"] if s["name"] == "charlie")
    assert charlie["stale"] is True and charlie["group"] == "stale"


async def test_config_page_redacts_token(client: httpx.AsyncClient) -> None:
    r = await client.get("/config")
    assert r.status_code == 200
    assert TOKEN not in r.text
    assert "***" in r.text
    assert "100.64.0.0/10" in r.text


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


def test_build_views_sort_and_stale(state: RuntimeState) -> None:
    views = build_views(state.storage.list_services(), staleness_seconds=900)
    assert [v.group for v in views] == ["down", "stale", "unknown", "up"]
