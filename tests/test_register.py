from __future__ import annotations

import ipaddress
import json

import httpx
import pytest

from service_status_aggregator.config import Config
from service_status_aggregator.storage import Storage
from service_status_aggregator.web.app import RuntimeState, create_app

PAYLOAD = {
    "name": "media",
    "host": "100.100.100.100",
    "port": 8080,
    "health_url": "http://100.100.100.100:8080/health",
    "log_path": "/var/log/media.log",
    "config_page_url": "http://100.100.100.100:8080/settings",
}


def fake_resolver(host: str):
    table = {
        "good.ts.net": [ipaddress.ip_address("100.64.9.9")],
        "evil.example": [ipaddress.ip_address("93.184.216.34")],
    }
    return table.get(host, [])


@pytest.fixture
def state(config: Config) -> RuntimeState:
    storage = Storage(config.storage.db_path)
    storage.migrate()
    return RuntimeState(cfg=config, storage=storage, resolver=fake_resolver)


@pytest.fixture
async def client(state: RuntimeState):
    app = create_app(state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_register_creates_then_updates(client: httpx.AsyncClient, token: str) -> None:
    r = await client.post("/register", json=PAYLOAD, headers=auth(token))
    assert r.status_code == 201, r.text
    assert r.json()["registration_count"] == 1
    r = await client.post("/register", json=PAYLOAD | {"port": 8081}, headers=auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["port"] == 8081 and body["registration_count"] == 2
    assert body["first_registered_at"] is not None


async def test_register_requires_token(client: httpx.AsyncClient) -> None:
    r = await client.post("/register", json=PAYLOAD)
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == "Bearer"
    r = await client.post("/register", json=PAYLOAD, headers=auth("wrong-token-000000000000"))
    assert r.status_code == 401


async def test_register_validation_errors_are_field_level(
    client: httpx.AsyncClient, token: str
) -> None:
    r = await client.post("/register", json=PAYLOAD | {"name": "Bad Name"}, headers=auth(token))
    assert r.status_code == 422
    assert r.json()["detail"][0]["field"] == "name"


async def test_register_rejects_health_url_on_other_host(
    client: httpx.AsyncClient, token: str
) -> None:
    r = await client.post(
        "/register",
        json=PAYLOAD | {"health_url": "http://100.100.100.101/health"},
        headers=auth(token),
    )
    assert r.status_code == 422
    assert "must match the registered host" in r.text


async def test_register_rejects_target_outside_allowlist(
    client: httpx.AsyncClient, token: str
) -> None:
    r = await client.post(
        "/register",
        json=PAYLOAD | {"host": "8.8.8.8", "health_url": "http://8.8.8.8/health"},
        headers=auth(token),
    )
    assert r.status_code == 422 and "outside the allowlist" in r.text
    r = await client.post(
        "/register",
        json=PAYLOAD | {"host": "evil.example", "health_url": "http://evil.example/health"},
        headers=auth(token),
    )
    assert r.status_code == 422 and "outside the allowlist" in r.text
    r = await client.post(
        "/register",
        json=PAYLOAD | {"host": "good.ts.net", "health_url": "http://good.ts.net/health"},
        headers=auth(token),
    )
    assert r.status_code == 201


async def test_register_body_limits_and_content_type(client: httpx.AsyncClient, token: str) -> None:
    big = PAYLOAD | {"log_path": "/" + "x" * 20000}
    r = await client.post("/register", json=big, headers=auth(token))
    assert r.status_code == 413
    r = await client.post(
        "/register", content=b"{", headers=auth(token) | {"Content-Type": "application/json"}
    )
    assert r.status_code == 400
    r = await client.post(
        "/register",
        content=json.dumps(PAYLOAD),
        headers=auth(token) | {"Content-Type": "text/plain"},
    )
    assert r.status_code == 415
    r = await client.post(
        "/register", content=b"[1,2]", headers=auth(token) | {"Content-Type": "application/json"}
    )
    assert r.status_code == 400


async def test_security_headers_on_every_response(client: httpx.AsyncClient) -> None:
    r = await client.post("/register", json=PAYLOAD)
    assert r.headers["Content-Security-Policy"].startswith("default-src 'self'")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Referrer-Policy"] == "no-referrer"
