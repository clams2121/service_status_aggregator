from __future__ import annotations

from pathlib import Path

import pytest

from service_status_aggregator.config import Config, load_config


def write_config(tmp_path: Path, extra: str = "") -> Path:
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "logs").mkdir(exist_ok=True)
    body = f"""
[server]
bind = "127.0.0.1"
port = 8720

[storage]
db_path = "{tmp_path / "data" / "aggregator.db"}"

[polling]
interval_seconds = 30
timeout_seconds = 5

[history]
retention_days = 90

[logging]
path = "{tmp_path / "logs" / "aggregator.log"}"
{extra}
"""
    path = tmp_path / "config.toml"
    path.write_text(body)
    return path


@pytest.fixture
def token() -> str:
    return "test-token-0123456789abcdef"


@pytest.fixture
def config(tmp_path: Path, token: str) -> Config:
    path = write_config(tmp_path)
    return load_config(path, environ={"SSA_REGISTRATION_TOKEN": token})
