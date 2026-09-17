"""FastAPI application: registration endpoint, status page, readiness."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from service_status_aggregator import __version__
from service_status_aggregator.config import Config, redacted_view
from service_status_aggregator.models import (
    MAX_BODY_BYTES,
    RegistrationIn,
    Resolver,
    check_target,
    system_resolver,
)
from service_status_aggregator.storage import SOURCE_REGISTER, Storage, to_iso, utcnow
from service_status_aggregator.web.history import build_histories
from service_status_aggregator.web.views import banner, build_views, rendered_at, summarise

_HERE = Path(__file__).resolve().parent

log = logging.getLogger("service_status_aggregator.web")

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


@dataclass
class RuntimeState:
    """Mutable process state shared between the web app and background tasks."""

    cfg: Config
    storage: Storage
    started_at: datetime = field(default_factory=utcnow)
    bind_ip: str | None = None
    bind_kind: str = "unresolved"  # tailscale | loopback | explicit | unresolved
    poller_last_cycle_at: datetime | None = None
    poller_started: bool = False
    resolver: Resolver = system_resolver
    poller: Any = None  # Poller instance once started; typed loosely to avoid an import cycle

    @property
    def uptime_seconds(self) -> int:
        return int((utcnow() - self.started_at).total_seconds())


def _error(status: int, error: str, detail: object = None, **headers: str) -> JSONResponse:
    body: dict[str, object] = {"error": error}
    if detail is not None:
        body["detail"] = detail
    return JSONResponse(body, status_code=status, headers=headers or None)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _token_ok(request: Request, cfg: Config) -> bool:
    if not cfg.token_enabled:
        return True
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented.strip():
        return False
    expected = cfg.registration_token or ""
    return hmac.compare_digest(presented.strip().encode(), expected.encode())


async def _read_limited_body(request: Request, limit: int) -> bytes | None:
    """Read at most *limit* bytes; return None if the body is larger."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(state: RuntimeState) -> FastAPI:
    app = FastAPI(
        title="Service Status Aggregator",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.runtime = state
    cfg = state.cfg
    templates = Jinja2Templates(directory=str(_HERE / "templates"))
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

    def base_context(page: str) -> dict[str, Any]:
        warnings: list[str] = []
        if not cfg.token_enabled:
            warnings.append(
                "Registration token is disabled: anyone who can reach this service can "
                "register or overwrite entries. Set require_token = true and provide a token."
            )
        if state.bind_kind == "loopback":
            warnings.append(
                "No Tailscale IP was found; bound to 127.0.0.1 only. Reach this page through "
                f"an SSH tunnel: ssh -L {cfg.server.port}:127.0.0.1:{cfg.server.port} <host>"
            )
        return {
            "page": page,
            "version": __version__,
            "warnings": warnings,
            "bind_ip": state.bind_ip,
            "bind_kind": state.bind_kind,
            "port": cfg.server.port,
            "poll_interval": cfg.polling.interval_seconds,
            "staleness": cfg.registration.staleness_seconds,
            "now_local": rendered_at(utcnow(), ZoneInfo(cfg.display.timezone)),
            "timezone": cfg.display.timezone,
        }

    def load_views() -> list[Any]:
        """Rows plus reconstructed history; runs in a worker thread."""
        now = utcnow()
        rows = state.storage.list_services()
        histories = build_histories(
            state.storage,
            rows,
            days=cfg.display.history_days,
            tz=ZoneInfo(cfg.display.timezone),
            major_outage_s=cfg.display.major_outage_minutes * 60,
            now=now,
        )
        return build_views(rows, cfg, now, histories)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response: Response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.post("/register")
    async def register(request: Request) -> Response:
        client = _client_ip(request)
        if not _token_ok(request, cfg):
            log.warning("register rejected from %s: missing or invalid token", client)
            return _error(401, "unauthorized", **{"WWW-Authenticate": "Bearer"})

        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            return _error(415, "unsupported_media_type", "send application/json")

        raw = await _read_limited_body(request, MAX_BODY_BYTES)
        if raw is None:
            log.warning("register rejected from %s: body exceeds %d bytes", client, MAX_BODY_BYTES)
            return _error(413, "payload_too_large", f"body must be <= {MAX_BODY_BYTES} bytes")
        try:
            payload = json.loads(raw)
        except ValueError:
            return _error(400, "invalid_json")
        if not isinstance(payload, dict):
            return _error(400, "invalid_json", "body must be a JSON object")

        try:
            reg = RegistrationIn.model_validate(payload)
        except ValidationError as exc:
            problems = [
                {"field": ".".join(str(p) for p in e["loc"]) or "body", "message": e["msg"]}
                for e in exc.errors()
            ]
            log.warning(
                "register rejected from %s: %s",
                client,
                "; ".join(f"{p['field']}: {p['message']}" for p in problems),
            )
            return _error(422, "validation_failed", problems)

        decision = await asyncio.to_thread(
            check_target, reg.host, cfg.registration.allowed_target_cidrs, state.resolver
        )
        if not decision.allowed:
            log.warning("register rejected from %s for %s: %s", client, reg.name, decision.reason)
            return _error(422, "validation_failed", [{"field": "host", "message": decision.reason}])

        row, created = await asyncio.to_thread(
            state.storage.upsert_registration, reg.as_record(), source=SOURCE_REGISTER
        )
        if created:
            log.info(
                "registered new service %s at %s:%d from %s", row.name, row.host, row.port, client
            )
        else:
            log.debug(
                "re-registered %s at %s (count=%d)", row.name, row.host, row.registration_count
            )
        return JSONResponse(row.to_dict(), status_code=201 if created else 200)

    @app.get("/")
    async def index(request: Request) -> Response:
        views = await asyncio.to_thread(load_views)
        counts = summarise(views)
        ctx = base_context("index") | {
            "services": views,
            "counts": counts,
            "banner": banner(counts),
            "history_days": cfg.display.history_days,
            "major_outage_minutes": cfg.display.major_outage_minutes,
            "degraded_response_ms": cfg.display.degraded_response_ms,
            "refresh_seconds": max(10, int(cfg.polling.interval_seconds)),
        }
        return templates.TemplateResponse(request, "index.html", ctx)

    @app.get("/api/services")
    async def api_services() -> Response:
        views = await asyncio.to_thread(load_views)
        counts = summarise(views)
        return JSONResponse(
            {
                "generated_at": to_iso(utcnow()),
                "timezone": cfg.display.timezone,
                "staleness_seconds": cfg.registration.staleness_seconds,
                "history_days": cfg.display.history_days,
                "counts": counts,
                "overall": banner(counts).text,
                "services": [v.to_dict() for v in views],
            }
        )

    @app.get("/config")
    async def config_page(request: Request) -> Response:
        ctx = base_context("config") | {"view": redacted_view(cfg, state.bind_ip)}
        return templates.TemplateResponse(request, "config.html", ctx)

    @app.get("/health")
    async def health() -> Response:
        checks: dict[str, str] = {}
        ok = True

        db_ok = await asyncio.to_thread(state.storage.ping)
        db_dir = cfg.storage.db_path.parent
        writable = os.access(db_dir, os.W_OK | os.X_OK)
        if db_ok and writable:
            checks["db"] = "ok"
        else:
            ok = False
            checks["db"] = "unreachable" if not db_ok else f"directory not writable: {db_dir}"

        if state.poller is not None:
            poller_ok, detail = state.poller.healthy()
        else:
            poller_ok, detail = _poller_grace(state)
        checks["poller"] = detail
        ok = ok and poller_ok

        checks["bind"] = state.bind_kind
        body = {
            "status": "ok" if ok else "degraded",
            "version": __version__,
            "uptime_seconds": state.uptime_seconds,
            "checks": checks,
        }
        return JSONResponse(body, status_code=200 if ok else 503)

    return app


def _poller_grace(state: RuntimeState) -> tuple[bool, str]:
    """Readiness verdict when no poller has been attached (early startup)."""
    limit = 2 * state.cfg.polling.interval_seconds
    if state.uptime_seconds <= limit:
        return True, "starting"
    return False, "poller not started"
