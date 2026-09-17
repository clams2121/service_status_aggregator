from __future__ import annotations

import ipaddress
from pathlib import Path

import pytest

from service_status_aggregator import cli
from service_status_aggregator.config import (
    ConfigError,
    load_config,
    load_registration_token,
    redacted_view,
)
from tests.conftest import write_config

TOKEN = "test-token-0123456789abcdef"
ENV = {"SSA_REGISTRATION_TOKEN": TOKEN}


def test_missing_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path / "nope.toml", environ=ENV)
    assert "not found" in exc.value.problems[0]


def test_invalid_toml(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text("[server\nport = 1")
    with pytest.raises(ConfigError) as exc:
        load_config(p, environ=ENV)
    assert "not valid TOML" in exc.value.problems[0]


def test_valid_config_loads(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path), environ=ENV)
    assert cfg.server.bind == "127.0.0.1"
    assert cfg.server.port == 8720
    assert cfg.registration.require_token is True
    assert cfg.registration_token == TOKEN
    assert ipaddress.ip_network("100.64.0.0/10") in cfg.registration.allowed_target_cidrs


def test_all_problems_are_reported_together(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        extra="""
[registration]
staleness_seconds = -1
allowed_target_cidrs = ["not-a-cidr", "0.0.0.0/0"]
typo_key = 1
""",
    )
    with pytest.raises(ConfigError) as exc:
        load_config(path, environ=ENV)
    joined = "\n".join(exc.value.problems)
    assert "staleness_seconds" in joined
    assert "not-a-cidr" in joined
    assert "allows every address" in joined
    assert "unknown key 'typo_key'" in joined
    assert len(exc.value.problems) == 4


def test_bind_all_interfaces_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    path.write_text(path.read_text().replace('bind = "127.0.0.1"', 'bind = "0.0.0.0"'))
    with pytest.raises(ConfigError) as exc:
        load_config(path, environ=ENV)
    assert any("never binds to all interfaces" in p for p in exc.value.problems)


def test_timeout_must_be_below_interval(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    path.write_text(path.read_text().replace("timeout_seconds = 5", "timeout_seconds = 30"))
    with pytest.raises(ConfigError) as exc:
        load_config(path, environ=ENV)
    assert any("less than interval_seconds" in p for p in exc.value.problems)


def test_missing_directories_are_reported(tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    path.write_text(
        f"""
[storage]
db_path = "{tmp_path}/missing/db.sqlite"
[logging]
path = "{tmp_path}/missing2/app.log"
"""
    )
    with pytest.raises(ConfigError) as exc:
        load_config(path, environ=ENV)
    assert sum("directory does not exist" in p for p in exc.value.problems) == 2


def test_token_required_by_default(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path), environ={})
    assert any("no token was found" in p for p in exc.value.problems)


def test_token_optional_when_disabled(tmp_path: Path) -> None:
    cfg = load_config(
        write_config(tmp_path, extra="[registration]\nrequire_token = false\n"), environ={}
    )
    assert cfg.token_enabled is False


def test_short_token_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path), environ={"SSA_REGISTRATION_TOKEN": "short"})
    assert any("shorter than" in p for p in exc.value.problems)


def test_systemd_credential_takes_precedence(tmp_path: Path) -> None:
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "registration_token").write_text("cred-token-0123456789abcdef\n")
    token, source = load_registration_token(
        {"CREDENTIALS_DIRECTORY": str(creds), "SSA_REGISTRATION_TOKEN": TOKEN}
    )
    assert token == "cred-token-0123456789abcdef"
    assert "systemd credential" in source


def test_redacted_view_never_contains_token(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path), environ=ENV)
    view = redacted_view(cfg, bind_resolved="127.0.0.1")
    assert TOKEN not in repr(view)
    assert view["registration"]["token"] == "***"


def test_cli_check_config_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("SSA_REGISTRATION_TOKEN", TOKEN)
    assert cli.main(["--config", str(tmp_path / "missing.toml"), "check-config"]) == 2
    assert "not found" in capsys.readouterr().err
    assert cli.main(["--config", str(write_config(tmp_path)), "check-config"]) == 0
    out = capsys.readouterr().out
    assert "config OK" in out
    assert TOKEN not in out


def test_cli_version(capsys) -> None:
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out.strip() == "0.1.0"
