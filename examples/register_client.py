"""Self-registration client for services that want to appear on the Service Status Aggregator.

Standard library only; copy this file into your project. Registration is best-effort:
failures are logged as warnings and never raised, so your service keeps running when the
aggregator is unreachable.

Usage (call once at startup; it registers immediately, then every 5 minutes in a daemon thread):

    from register_client import start_registration

    start_registration(
        aggregator_url="http://100.100.100.100:8720",   # the aggregator's Tailscale address
        token=os.environ["SSA_REGISTRATION_TOKEN"],     # from your own secret store
        name="my-service",
        host="100.100.100.101",                         # this machine's Tailscale IP or name
        port=8080,
        health_url="http://100.100.100.101:8080/health",
        log_path="/var/log/my-service/app.log",
        config_page_url="http://100.100.100.101:8080/settings",  # or ""
    )
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request

log = logging.getLogger("register_client")

REGISTER_INTERVAL_SECONDS = 300
REQUEST_TIMEOUT_SECONDS = 5


def register_once(
    aggregator_url: str,
    token: str,
    *,
    name: str,
    host: str,
    port: int,
    health_url: str,
    log_path: str = "",
    config_page_url: str = "",
) -> bool:
    """POST one registration. Returns True on 200/201, False otherwise.

    Never raises for network or server problems. Raises ValueError only for a
    programming error: an aggregator URL that is not http(s).
    """
    if not aggregator_url.startswith(("http://", "https://")):
        raise ValueError("aggregator_url must start with http:// or https://")
    payload = {
        "name": name,
        "host": host,
        "port": port,
        "health_url": health_url,
        "log_path": log_path,
        "config_page_url": config_page_url,
    }
    request = urllib.request.Request(  # noqa: S310 - scheme checked above
        aggregator_url.rstrip("/") + "/register",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": f"{name}/register-client",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read(2000).decode(errors="replace")
        log.warning("aggregator rejected registration (%s): %s", exc.code, body)
        return False
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        log.warning("aggregator unreachable at %s: %s", aggregator_url, exc)
        return False
    if status in (200, 201):
        log.debug("registered with aggregator (%s)", status)
        return True
    log.warning("unexpected status %s from aggregator", status)
    return False


def start_registration(aggregator_url: str, token: str, **fields: object) -> threading.Thread:
    """Register now and every REGISTER_INTERVAL_SECONDS in a daemon thread."""
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            register_once(aggregator_url, token, **fields)  # type: ignore[arg-type]
            stop.wait(REGISTER_INTERVAL_SECONDS)

    thread = threading.Thread(target=loop, name="aggregator-registration", daemon=True)
    thread.stop = stop  # type: ignore[attr-defined]
    thread.start()
    return thread
