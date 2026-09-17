from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from service_status_aggregator import runtime
from service_status_aggregator.config import load_config
from service_status_aggregator.storage import Storage

TOKEN = "e2e-token-0123456789abcdef"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_cfg(tmp_path: Path, port: int) -> Path:
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f"""
[server]
bind = "127.0.0.1"
port = {port}
[storage]
db_path = "{tmp_path}/data/aggregator.db"
[polling]
interval_seconds = 10
timeout_seconds = 0.5
[registration]
self_register_interval_seconds = 1
[logging]
path = "{tmp_path}/logs/aggregator.log"
level = "DEBUG"
"""
    )
    return cfg


@pytest.mark.timeout(30)
def test_end_to_end_run(tmp_path: Path) -> None:
    port = free_port()
    cfg = write_cfg(tmp_path, port)
    env = os.environ | {"SSA_REGISTRATION_TOKEN": TOKEN, "SSA_LOG_STDERR": "0"}
    env.pop("NOTIFY_SOCKET", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "service_status_aggregator", "run", "--config", str(cfg)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 15
        health = None
        while time.time() < deadline:
            try:
                health = httpx.get(f"{base}/health", timeout=1)
                if health.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        assert health is not None and health.status_code == 200, (
            proc.stderr.read().decode() if proc.poll() is not None else "no response"
        )
        body = health.json()
        assert body["checks"]["bind"] == "explicit"

        # self-registration shows the aggregator on its own dashboard and it gets polled up
        deadline = time.time() + 10
        while time.time() < deadline:
            data = httpx.get(f"{base}/api/services", timeout=1).json()
            me = next(
                (s for s in data["services"] if s["name"] == "service-status-aggregator"), None
            )
            if me and me["last_status"] == "up":
                break
            time.sleep(0.2)
        assert me is not None and me["last_status"] == "up" and me["source"] == "self"

        r = httpx.post(
            f"{base}/register",
            json={
                "name": "e2e",
                "host": "127.0.0.1",
                "port": port,
                "health_url": f"{base}/health",
                "log_path": "",
                "config_page_url": "",
            },
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=2,
        )
        assert r.status_code == 201, r.text
        page = httpx.get(f"{base}/", timeout=2)
        assert page.status_code == 200 and "e2e" in page.text
        assert page.headers["Content-Security-Policy"].startswith("default-src 'self'")
        assert "server" not in page.headers  # server header disabled
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    assert proc.returncode == 0, proc.stderr.read().decode()
    log_text = (tmp_path / "logs" / "aggregator.log").read_text()
    assert "ready: http://127.0.0.1" in log_text
    assert "poller started" in log_text
    assert "stopped" in log_text
    assert TOKEN not in log_text


def test_run_exits_2_without_config(tmp_path: Path, capsys) -> None:
    assert runtime.cmd_run(str(tmp_path / "missing.toml")) == 2
    assert "not found" in capsys.readouterr().err


def test_run_exits_3_when_port_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    port = free_port()
    cfg = write_cfg(tmp_path, port)
    monkeypatch.setenv("SSA_REGISTRATION_TOKEN", TOKEN)
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))
        s.listen()
        assert runtime.cmd_run(str(cfg)) == 3
    assert "already in use" in (tmp_path / "logs" / "aggregator.log").read_text()


def test_remove_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    cfg = write_cfg(tmp_path, 1)
    monkeypatch.setenv("SSA_REGISTRATION_TOKEN", TOKEN)
    c = load_config(cfg)
    st = Storage(c.storage.db_path)
    st.migrate()
    st.upsert_registration(
        {
            "name": "old",
            "host": "127.0.0.1",
            "port": 1,
            "health_url": "http://127.0.0.1/h",
            "log_path": "",
            "config_page_url": "",
        }
    )
    st.close()
    assert runtime.cmd_remove(str(cfg), "OLD", "127.0.0.1") == 0
    assert "removed old" in capsys.readouterr().out
    assert runtime.cmd_remove(str(cfg), "old", "127.0.0.1") == 1


def test_healthcheck_command_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    cfg = write_cfg(tmp_path, free_port())
    monkeypatch.setenv("SSA_REGISTRATION_TOKEN", TOKEN)
    assert runtime.cmd_healthcheck(str(cfg)) == 1
    assert "unreachable" in capsys.readouterr().err


def test_detect_monitoring_gap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import timedelta

    from service_status_aggregator.storage import META_LAST_CYCLE, to_iso, utcnow
    from service_status_aggregator.web.app import RuntimeState

    cfg = write_cfg(tmp_path, 1)
    monkeypatch.setenv("SSA_REGISTRATION_TOKEN", TOKEN)
    c = load_config(cfg)
    st = Storage(c.storage.db_path)
    st.migrate()
    row, _ = st.upsert_registration(
        {
            "name": "svc",
            "host": "127.0.0.1",
            "port": 1,
            "health_url": "http://127.0.0.1/h",
            "log_path": "",
            "config_page_url": "",
        }
    )
    st.record_check(
        row.id,
        status="up",
        checked_at=utcnow() - timedelta(hours=3),
        response_ms=1,
        failure_reason=None,
    )
    state = RuntimeState(cfg=c, storage=st)
    assert runtime.detect_monitoring_gap(state) == 0  # no meta yet
    st.set_meta(META_LAST_CYCLE, to_iso(utcnow() - timedelta(seconds=15)))
    assert runtime.detect_monitoring_gap(state) == 0  # within 3 intervals
    st.set_meta(META_LAST_CYCLE, to_iso(utcnow() - timedelta(hours=2)))
    assert runtime.detect_monitoring_gap(state) == 1
    events = st.list_events(row.id)
    assert events[0].to_status == "unknown" and "aggregator not running" in (events[0].reason or "")
    st.close()
