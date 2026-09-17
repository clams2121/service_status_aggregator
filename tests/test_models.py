from __future__ import annotations

import ipaddress

import pytest
from pydantic import ValidationError

from service_status_aggregator.models import RegistrationIn, check_target

GOOD = {
    "name": "Media-Server",
    "host": "100.100.100.100",
    "port": 8080,
    "health_url": "http://100.100.100.100:8080/health",
    "log_path": "/var/log/media/app.log",
    "config_page_url": "http://100.100.100.100:8080/settings",
}


def test_valid_payload_is_normalised() -> None:
    reg = RegistrationIn.model_validate(GOOD)
    assert reg.name == "media-server"
    assert reg.host == "100.100.100.100"


def test_hostname_host_is_lowercased_and_matched() -> None:
    reg = RegistrationIn.model_validate(
        GOOD
        | {"host": "Media.Tail1234.ts.net", "health_url": "https://media.tail1234.ts.net/health"}
    )
    assert reg.host == "media.tail1234.ts.net"


@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        ("name", "-bad", "name must be"),
        ("name", "a" * 65, "at most 64"),
        ("host", "bad_host!", "valid hostname"),
        ("port", 0, "greater than or equal to 1"),
        ("port", 70000, "less than or equal to 65535"),
        ("health_url", "ftp://100.100.100.100/health", "http:// or https://"),
        ("health_url", "http://other.host/health", "must match the registered host"),
        ("health_url", "http://user:pw@100.100.100.100/health", "credentials"),
        ("health_url", "javascript:alert(1)", "http:// or https://"),
        ("log_path", "relative/path.log", "absolute path"),
        ("log_path", "/var/log/x\x00y", "control characters"),
        ("config_page_url", "javascript:alert(1)", "http:// or https://"),
        ("config_page_url", "http://", "include a host"),
    ],
)
def test_rejections(field: str, value: object, fragment: str) -> None:
    with pytest.raises(ValidationError) as exc:
        RegistrationIn.model_validate(GOOD | {field: value})
    assert fragment in str(exc.value)


def test_empty_optional_urls_and_paths_allowed() -> None:
    reg = RegistrationIn.model_validate(GOOD | {"log_path": "", "config_page_url": ""})
    assert reg.log_path == "" and reg.config_page_url == ""


def test_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        RegistrationIn.model_validate(GOOD | {"extra": 1})
    assert "Extra inputs" in str(exc.value)


def test_missing_fields_rejected() -> None:
    payload = dict(GOOD)
    del payload["log_path"]
    with pytest.raises(ValidationError):
        RegistrationIn.model_validate(payload)


ALLOWED = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("127.0.0.0/8"))


def test_check_target_ip_literal() -> None:
    assert check_target("100.100.100.100", ALLOWED).allowed
    assert check_target("127.0.0.1", ALLOWED).allowed
    d = check_target("8.8.8.8", ALLOWED)
    assert not d.allowed and "outside the allowlist" in d.reason


def test_check_target_hostname_resolution() -> None:
    def resolver(host: str):
        return {
            "good.ts.net": [ipaddress.ip_address("100.64.1.2")],
            "mixed.ts.net": [ipaddress.ip_address("100.64.1.2"), ipaddress.ip_address("1.2.3.4")],
            "none.ts.net": [],
        }[host]

    assert check_target("good.ts.net", ALLOWED, resolver).allowed
    mixed = check_target("mixed.ts.net", ALLOWED, resolver)
    assert not mixed.allowed and "1.2.3.4" in mixed.reason
    assert "dns_unresolvable" in check_target("none.ts.net", ALLOWED, resolver).reason
