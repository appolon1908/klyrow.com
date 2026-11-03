"""M1-04: the in-process rate limiter must not grow without bound and the
gateway image must not trust X-Forwarded-For from arbitrary peers."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from apps.gateway.app import main

ROOT = Path(__file__).parents[1]


def _request(host: str):
    return SimpleNamespace(client=SimpleNamespace(host=host))


@pytest.fixture(autouse=True)
def _clean_buckets():
    main.rate_buckets.clear()
    yield
    main.rate_buckets.clear()


def test_auth_rate_still_limits_within_the_window(monkeypatch):
    monkeypatch.setenv("KLYROW_AUTH_RATE_PER_MINUTE", "3")
    monkeypatch.setattr(main.time, "time", lambda: 1_000.0)
    for _ in range(3):
        main.auth_rate(_request("203.0.113.9"), "login")
    with pytest.raises(HTTPException) as excinfo:
        main.auth_rate(_request("203.0.113.9"), "login")
    assert excinfo.value.status_code == 429
    # Another identity and another action are independent buckets.
    main.auth_rate(_request("203.0.113.10"), "login")
    main.auth_rate(_request("203.0.113.9"), "forgot-password")


def test_expired_buckets_are_evicted_instead_of_lingering_forever(monkeypatch):
    monkeypatch.setenv("KLYROW_AUTH_RATE_PER_MINUTE", "10")
    clock = {"now": 1_000.0}
    monkeypatch.setattr(main.time, "time", lambda: clock["now"])

    main.auth_rate(_request("198.51.100.1"), "login")
    stale_key = ("auth", "login", "198.51.100.1")
    assert stale_key in main.rate_buckets

    # The window passes and one more request arrives (from anyone).
    clock["now"] += main.RATE_LIMIT_WINDOW_SECONDS + 1
    main.auth_rate(_request("198.51.100.2"), "login")

    assert stale_key not in main.rate_buckets
    assert all(bucket for bucket in main.rate_buckets.values())


def test_tracked_keys_are_capped_and_oldest_touched_dropped_first(monkeypatch):
    monkeypatch.setenv("KLYROW_AUTH_RATE_PER_MINUTE", "10")
    monkeypatch.setattr(main, "RATE_LIMIT_MAX_KEYS", 500)
    monkeypatch.setattr(main.time, "time", lambda: 1_000.0)
    cap = main.RATE_LIMIT_MAX_KEYS

    for index in range(cap + 100):
        main.auth_rate(_request(f"10.0.{index // 256}.{index % 256}"), "login")

    assert len(main.rate_buckets) <= cap
    assert ("auth", "login", "10.0.0.0") not in main.rate_buckets
    newest = cap + 99
    assert ("auth", "login", f"10.0.{newest // 256}.{newest % 256}") in main.rate_buckets


def test_cap_defaults_to_ten_thousand_keys():
    assert main.RATE_LIMIT_MAX_KEYS == 10_000


def test_gateway_image_does_not_trust_forwarded_headers_from_everyone():
    dockerfile = (ROOT / "apps/gateway/Dockerfile").read_text(encoding="utf-8")
    assert "forwarded-allow-ips=*" not in dockerfile
    assert "--proxy-headers" in dockerfile
    assert "${KLYROW_FORWARDED_ALLOW_IPS:-127.0.0.1}" in dockerfile
    assert "USER 10001" in dockerfile

    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "KLYROW_FORWARDED_ALLOW_IPS=" in env_example
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    # Inside Compose the gateway's peer is the web container on the frontend
    # network, so the default allow-list must cover that (pinned) subnet.
    assert "KLYROW_FORWARDED_ALLOW_IPS: " in compose
    assert ':-127.0.0.1,${KLYROW_FRONTEND_SUBNET:-172.28.0.0/24}}"' in compose
    assert 'subnet: "${KLYROW_FRONTEND_SUBNET:-172.28.0.0/24}"' in compose
    assert 'KLYROW_FORWARDED_ALLOW_IPS: "${KLYROW_FORWARDED_ALLOW_IPS:-*' not in compose


def test_host_nginx_template_has_dedicated_auth_limit_zones():
    source = (ROOT / "docker/proxy/klyrow.conf").read_text(encoding="utf-8")
    assert "zone=klyrow_web:10m rate=10r/s;" in source
    assert "zone=klyrow_api:10m rate=10r/s;" in source
    assert "zone=klyrow_web_auth:10m rate=5r/m;" in source
    assert "zone=klyrow_api_auth:10m rate=5r/m;" in source
    assert "limit_req_status 429;" in source
    assert "limit_req zone=klyrow_web_auth burst=10 nodelay;" in source
    assert "limit_req zone=klyrow_api_auth burst=10 nodelay;" in source
    assert "location ~ ^/v1/auth/(login|forgot-password|reset-password)$" in source
