"""Process wiring for `run` and `remove`: config, logging, storage, bind, server and tasks."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Iterator
from datetime import timedelta

import httpx
import uvicorn

from service_status_aggregator import __version__, sdnotify
from service_status_aggregator.config import (
    Config,
    ConfigError,
    find_config_path,
    load_config,
    load_registration_token,
)
from service_status_aggregator.logging_setup import setup_logging
from service_status_aggregator.netbind import BindError, port_in_use, resolve_bind
from service_status_aggregator.poller import Poller
from service_status_aggregator.storage import SOURCE_SELF, Storage, utcnow
from service_status_aggregator.web.app import RuntimeState, create_app

log = logging.getLogger("service_status_aggregator")

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_BIND = 3
SELF_NAME = "service-status-aggregator"
JANITOR_INTERVAL_S = 3600.0


# --------------------------------------------------------------------------- background tasks


async def _wait(stop: asyncio.Event, seconds: float) -> bool:
    """Sleep up to *seconds*; return True if *stop* was set meanwhile."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
        return True
    except TimeoutError:
        return False


async def self_register_task(state: RuntimeState, stop: asyncio.Event) -> None:
    cfg = state.cfg
    host = state.bind_ip or "127.0.0.1"
    url_host = f"[{host}]" if ":" in host else host
    base = f"http://{url_host}:{cfg.server.port}"
    record = {
        "name": SELF_NAME,
        "host": host,
        "port": cfg.server.port,
        "health_url": f"{base}/health",
        "log_path": str(cfg.logging.path),
        "config_page_url": f"{base}/config",
    }
    while not stop.is_set():
        try:
            await asyncio.to_thread(state.storage.upsert_registration, record, source=SOURCE_SELF)
        except Exception:  # noqa: BLE001
            log.exception("self-registration failed")
        if await _wait(stop, cfg.registration.self_register_interval_seconds):
            return


async def janitor_task(state: RuntimeState, stop: asyncio.Event) -> None:
    retention = timedelta(days=state.cfg.history.retention_days)
    while not stop.is_set():
        try:
            pruned = await asyncio.to_thread(state.storage.prune_events, utcnow() - retention)
            if pruned:
                log.info("pruned %d status events older than %d days", pruned, retention.days)
        except Exception:  # noqa: BLE001
            log.exception("janitor failed")
        if await _wait(stop, JANITOR_INTERVAL_S):
            return


async def watchdog_task(state: RuntimeState, poller: Poller, stop: asyncio.Event) -> None:
    period = sdnotify.watchdog_interval_seconds()
    if period is None:
        return
    tick = max(1.0, period / 3)
    log.info("systemd watchdog enabled: pinging every %.0fs (WatchdogSec=%.0f)", tick, period)
    while not stop.is_set():
        ok, detail = poller.healthy()
        if ok:
            sdnotify.notify("WATCHDOG=1")
        else:
            log.error("withholding watchdog ping: poller unhealthy (%s)", detail)
        if await _wait(stop, tick):
            return


async def ready_task(server: uvicorn.Server, state: RuntimeState) -> None:
    while not server.started:
        await asyncio.sleep(0.05)
    cfg = state.cfg
    host = state.bind_ip or "?"
    url_host = f"[{host}]" if ":" in host else host
    log.info("ready: http://%s:%d/ (bind=%s)", url_host, cfg.server.port, state.bind_kind)
    sdnotify.notify(f"READY=1\nSTATUS=listening on {url_host}:{cfg.server.port}")


# --------------------------------------------------------------------------- serve


class _Server(uvicorn.Server):
    """uvicorn.Server that shuts down cleanly on SIGTERM/SIGINT without re-raising the signal.

    Stock uvicorn re-raises the captured signal after a graceful shutdown, which kills the
    process with a signal exit status and skips our cleanup. systemd treats that as a failure.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        loop = asyncio.get_running_loop()

        def on_signal(sig: signal.Signals) -> None:
            if self.should_exit:
                log.warning("second %s received; forcing exit", sig.name)
                self.force_exit = True
            else:
                log.info("%s received; shutting down", sig.name)
                self.should_exit = True

        installed: list[signal.Signals] = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, on_signal, sig)
                installed.append(sig)
            except (NotImplementedError, RuntimeError):  # not the main thread
                pass
        try:
            yield
        finally:
            for sig in installed:
                loop.remove_signal_handler(sig)


async def serve(cfg: Config, state: RuntimeState) -> int:
    app = create_app(state)
    server = _Server(
        uvicorn.Config(
            app,
            host=state.bind_ip or "127.0.0.1",
            port=cfg.server.port,
            log_config=None,
            access_log=False,
            server_header=False,
            date_header=False,
            timeout_graceful_shutdown=5,
        )
    )
    stop = asyncio.Event()
    poller = Poller(state)
    state.poller = poller
    tasks = [
        asyncio.create_task(poller.run(stop), name="poller"),
        asyncio.create_task(janitor_task(state, stop), name="janitor"),
        asyncio.create_task(watchdog_task(state, poller, stop), name="watchdog"),
        asyncio.create_task(ready_task(server, state), name="ready"),
    ]
    if cfg.registration.register_self:
        tasks.append(asyncio.create_task(self_register_task(state, stop), name="self-register"))

    try:
        await server.serve()
    finally:
        sdnotify.notify("STOPPING=1")
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        state.storage.close()
        log.info("stopped")
    return EXIT_OK if server.started else EXIT_BIND


# --------------------------------------------------------------------------- commands


def _load(config_arg: str | None) -> Config | None:
    try:
        return load_config(find_config_path(config_arg))
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return None


def cmd_run(config_arg: str | None) -> int:
    cfg = _load(config_arg)
    if cfg is None:
        return EXIT_CONFIG
    setup_logging(cfg.logging)
    _token, token_source = load_registration_token()
    log.info("service-status-aggregator %s starting (config %s)", __version__, cfg.path)
    log.info(
        "registration token: %s (%s)", "enabled" if cfg.token_enabled else "DISABLED", token_source
    )
    if not cfg.token_enabled:
        log.warning("registration is unauthenticated; anyone reaching this port can register")
    log.info(
        "poll every %gs (timeout %gs); stale after %gs; allowed targets %s",
        cfg.polling.interval_seconds,
        cfg.polling.timeout_seconds,
        cfg.registration.staleness_seconds,
        ", ".join(str(n) for n in cfg.registration.allowed_target_cidrs),
    )

    try:
        storage = Storage(cfg.storage.db_path)
        version = storage.migrate()
    except Exception as exc:  # noqa: BLE001
        log.critical("cannot open database %s: %s", cfg.storage.db_path, exc)
        return EXIT_CONFIG
    log.info("database %s (schema v%d)", cfg.storage.db_path, version)

    try:
        bind = resolve_bind(cfg.server.bind)
    except BindError as exc:
        log.critical("%s", exc)
        storage.close()
        return EXIT_BIND
    if port_in_use(bind.ip, cfg.server.port):
        log.critical("port %d on %s is already in use", cfg.server.port, bind.ip)
        storage.close()
        return EXIT_BIND
    if bind.kind == "loopback":
        log.warning(
            "reach the dashboard with: ssh -L %d:127.0.0.1:%d <this-host>",
            cfg.server.port,
            cfg.server.port,
        )
    log.info("binding %s:%d (%s)", bind.url_host, cfg.server.port, bind.kind)

    state = RuntimeState(cfg=cfg, storage=storage, bind_ip=bind.ip, bind_kind=bind.kind)
    try:
        return asyncio.run(serve(cfg, state))
    except SystemExit as exc:  # uvicorn exits on a bind failure
        log.critical("server failed to start (exit %s)", exc.code)
        return EXIT_BIND


def cmd_remove(config_arg: str | None, name: str, host: str) -> int:
    cfg = _load(config_arg)
    if cfg is None:
        return EXIT_CONFIG
    name, host = name.strip().lower(), host.strip().lower()
    storage = Storage(cfg.storage.db_path)
    try:
        storage.migrate()
        removed = storage.remove(name, host)
    finally:
        storage.close()
    if removed:
        print(f"removed {name} @ {host}")
        return EXIT_OK
    print(f"no service named {name} @ {host}", file=sys.stderr)
    return 1


def cmd_healthcheck(config_arg: str | None) -> int:
    """Probe /health like an external checker. Used by Docker HEALTHCHECK and installers."""
    cfg = _load(config_arg)
    if cfg is None:
        return EXIT_CONFIG
    try:
        bind = resolve_bind(cfg.server.bind, attempts=1)
    except BindError as exc:
        print(f"healthcheck: {exc}", file=sys.stderr)
        return 1
    url = f"http://{bind.url_host}:{cfg.server.port}/health"
    try:
        response = httpx.get(url, timeout=5, trust_env=False)
    except httpx.HTTPError as exc:
        print(f"healthcheck: {url} unreachable: {exc}", file=sys.stderr)
        return 1
    print(response.text)
    return EXIT_OK if response.status_code == 200 else 1
