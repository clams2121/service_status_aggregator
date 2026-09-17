from __future__ import annotations

import asyncio
import ipaddress
from datetime import timedelta

import httpx
import pytest

from service_status_aggregator.config import Config
from service_status_aggregator.poller import Poller
from service_status_aggregator.storage import Storage, utcnow
from service_status_aggregator.web.app import RuntimeState, create_app


def record(name: str, host: str = "100.100.100.100", path: str = "/health") -> dict:
    return {
        "name": name,
        "host": host,
        "port": 80,
        "health_url": f"http://{host}{path}",
        "log_path": "",
        "config_page_url": "",
    }


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/health":
        return httpx.Response(200, json={"status": "ok"})
    if path == "/broken":
        return httpx.Response(500, text="boom")
    if path == "/slow":
        raise httpx.ReadTimeout("read timed out", request=request)
    if path == "/refused":
        raise httpx.ConnectError("connection refused", request=request)
    if path == "/redirect":
        return httpx.Response(302, headers={"Location": "http://100.100.100.100/health"})
    if path == "/huge":
        return httpx.Response(200, content=b"x" * (200 * 1024))
    raise RuntimeError("unexpected path " + path)


@pytest.fixture
def state(config: Config) -> RuntimeState:
    storage = Storage(config.storage.db_path)
    storage.migrate()
    st = RuntimeState(cfg=config, storage=storage, resolver=lambda host: [])
    for name, path in [
        ("ok", "/health"),
        ("broken", "/broken"),
        ("slow", "/slow"),
        ("refused", "/refused"),
        ("redirect", "/redirect"),
        ("huge", "/huge"),
    ]:
        storage.upsert_registration(record(name, path=path))
    storage.upsert_registration(record("outside", host="8.8.8.8"))
    return st


async def test_cycle_classifies_every_outcome(state: RuntimeState) -> None:
    poller = Poller(state)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = {r.service.name: r for r in await poller.run_cycle(client)}
    assert results["ok"].status == "up" and results["ok"].response_ms is not None
    assert results["broken"].status == "down" and results["broken"].failure_reason == "http_500"
    assert results["slow"].failure_reason == "timeout_after_5s"
    assert results["refused"].failure_reason.startswith("connection_error")
    assert results["redirect"].failure_reason == "http_302"
    assert results["huge"].status == "up"
    assert results["outside"].failure_reason.startswith("target_rejected")
    assert state.poller_last_cycle_at is not None

    rows = {r.name: r for r in state.storage.list_services()}
    assert rows["ok"].last_status == "up"
    assert rows["broken"].last_failure_reason == "http_500"
    # every service transitioned from unknown exactly once
    assert len(state.storage.list_events()) == 7


async def test_second_identical_cycle_adds_no_events(state: RuntimeState) -> None:
    poller = Poller(state)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await poller.run_cycle(client)
        await poller.run_cycle(client)
    assert len(state.storage.list_events()) == 7


async def test_one_exploding_target_does_not_stop_the_cycle(state: RuntimeState) -> None:
    def exploding(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            raise ValueError("transport bug")
        return handler(request)

    poller = Poller(state)
    async with httpx.AsyncClient(transport=httpx.MockTransport(exploding)) as client:
        results = {r.service.name: r for r in await poller.run_cycle(client)}
    assert results["ok"].status == "down" and "ValueError" in results["ok"].failure_reason
    assert results["broken"].failure_reason == "http_500"


async def test_run_loop_stops_cleanly(state: RuntimeState, monkeypatch: pytest.MonkeyPatch) -> None:
    poller = Poller(state)
    monkeypatch.setattr(
        "service_status_aggregator.poller.make_client",
        lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(poller.run(stop))
    for _ in range(50):
        if state.poller_last_cycle_at is not None:
            break
        await asyncio.sleep(0.02)
    assert state.poller_last_cycle_at is not None
    stop.set()
    await asyncio.wait_for(task, timeout=2)


def test_healthy_verdicts(state: RuntimeState) -> None:
    poller = Poller(state)
    now = utcnow()
    assert poller.healthy(now) == (True, "starting")
    state.poller_last_cycle_at = now - timedelta(seconds=10)
    assert poller.healthy(now)[0] is True
    state.poller_last_cycle_at = now - timedelta(seconds=120)
    ok, detail = poller.healthy(now)
    assert ok is False and "last cycle" in detail
    state.poller_last_cycle_at = None
    state.started_at = now - timedelta(seconds=120)
    assert poller.healthy(now)[0] is False


async def test_health_endpoint(state: RuntimeState) -> None:
    app = create_app(state)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/health")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["checks"] == {"db": "ok", "poller": "starting", "bind": "unresolved"}
        assert "version" in body

        state.poller = Poller(state)
        state.poller_last_cycle_at = utcnow() - timedelta(seconds=500)
        r = await c.get("/health")
        assert r.status_code == 503
        assert r.json()["status"] == "degraded"
        assert "last cycle" in r.json()["checks"]["poller"]


def test_allowlist_networks_used_by_poller(config: Config) -> None:
    assert ipaddress.ip_network("100.64.0.0/10") in config.registration.allowed_target_cidrs
