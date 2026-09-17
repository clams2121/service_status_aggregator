"""Background health polling of every registered service."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime

import httpx

from service_status_aggregator import __version__
from service_status_aggregator.models import check_target
from service_status_aggregator.storage import (
    META_LAST_CYCLE,
    STATUS_DOWN,
    STATUS_UP,
    ServiceRow,
    to_iso,
    utcnow,
)
from service_status_aggregator.web.app import RuntimeState

log = logging.getLogger("service_status_aggregator.poller")

MAX_RESPONSE_BYTES = 64 * 1024
USER_AGENT = f"service-status-aggregator/{__version__}"


@dataclass(frozen=True)
class CheckResult:
    service: ServiceRow
    status: str
    response_ms: float | None
    failure_reason: str | None
    checked_at: datetime


def make_client(timeout_seconds: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
        trust_env=False,  # never pick up HTTP(S)_PROXY from the environment
    )


class Poller:
    def __init__(self, state: RuntimeState) -> None:
        self.state = state
        self.cfg = state.cfg
        self._sem = asyncio.Semaphore(self.cfg.polling.max_concurrent)

    # -- single check -------------------------------------------------------

    async def check_one(self, client: httpx.AsyncClient, svc: ServiceRow) -> CheckResult:
        checked_at = utcnow()
        decision = await asyncio.to_thread(
            check_target, svc.host, self.cfg.registration.allowed_target_cidrs, self.state.resolver
        )
        if not decision.allowed:
            return CheckResult(svc, STATUS_DOWN, None, decision.reason, checked_at)

        started = time.perf_counter()
        try:
            async with self._sem, client.stream("GET", svc.health_url) as resp:
                elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
                received = 0
                async for chunk in resp.aiter_bytes():
                    received += len(chunk)
                    if received > MAX_RESPONSE_BYTES:
                        break
                if resp.status_code == 200:
                    return CheckResult(svc, STATUS_UP, elapsed_ms, None, checked_at)
                return CheckResult(
                    svc, STATUS_DOWN, elapsed_ms, f"http_{resp.status_code}", checked_at
                )
        except httpx.TimeoutException:
            reason = f"timeout_after_{self.cfg.polling.timeout_seconds:g}s"
        except httpx.ConnectError as exc:
            reason = f"connection_error: {exc}"
        except httpx.HTTPError as exc:
            reason = f"error: {type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 - one bad target must not kill the cycle
            log.exception("unexpected error polling %s (%s)", svc.name, svc.health_url)
            reason = f"error: {type(exc).__name__}: {exc}"
        return CheckResult(svc, STATUS_DOWN, None, reason[:500], checked_at)

    # -- one cycle ----------------------------------------------------------

    async def run_cycle(self, client: httpx.AsyncClient) -> list[CheckResult]:
        services = await asyncio.to_thread(self.state.storage.list_services)
        results = await asyncio.gather(*(self.check_one(client, s) for s in services))
        for r in results:
            previous = await asyncio.to_thread(
                self.state.storage.record_check,
                r.service.id,
                status=r.status,
                checked_at=r.checked_at,
                response_ms=r.response_ms,
                failure_reason=r.failure_reason,
            )
            self._log_result(r, previous)
        finished = utcnow()
        self.state.poller_last_cycle_at = finished
        await asyncio.to_thread(self.state.storage.set_meta, META_LAST_CYCLE, to_iso(finished))
        return list(results)

    @staticmethod
    def _log_result(r: CheckResult, previous: str | None) -> None:
        where = f"{r.service.name} ({r.service.health_url})"
        if previous is not None:
            if r.status == STATUS_UP:
                log.info("%s is UP (was %s), %.1f ms", where, previous, r.response_ms or 0.0)
            else:
                log.warning("%s is DOWN (was %s): %s", where, previous, r.failure_reason)
            return
        if r.status == STATUS_UP:
            log.debug("%s up, %.1f ms", where, r.response_ms or 0.0)
        else:
            log.warning("%s still down: %s", where, r.failure_reason)

    # -- loop ---------------------------------------------------------------

    def healthy(self, now: datetime | None = None) -> tuple[bool, str]:
        """(ok, detail) for the readiness check."""
        now = now or utcnow()
        interval = self.cfg.polling.interval_seconds
        last = self.state.poller_last_cycle_at
        if last is not None:
            age = (now - last).total_seconds()
            if age <= 2 * interval:
                return True, "ok"
            return False, f"last cycle {age:.0f}s ago (limit {2 * interval:.0f}s)"
        age = (now - self.state.started_at).total_seconds()
        if age <= 2 * interval:
            return True, "starting"
        return False, "no cycle completed since start"

    async def run(self, stop: asyncio.Event) -> None:
        self.state.poller_started = True
        interval = self.cfg.polling.interval_seconds
        log.info(
            "poller started: interval %gs, timeout %gs", interval, self.cfg.polling.timeout_seconds
        )
        async with make_client(self.cfg.polling.timeout_seconds) as client:
            while not stop.is_set():
                started = time.monotonic()
                try:
                    await self.run_cycle(client)
                except Exception:  # noqa: BLE001 - the loop must survive
                    log.exception("poll cycle failed; will retry next interval")
                delay = max(0.0, interval - (time.monotonic() - started))
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
        log.info("poller stopped")
