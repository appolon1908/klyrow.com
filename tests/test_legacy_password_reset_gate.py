"""M1-03 (H3): legacy password reset is gated like legacy login and rate limited."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.gateway.app.main import DB, Base, Tenant, User, app, engine, ph, rate_buckets

client = TestClient(app)
EMAIL = "reset-gate@example.com"
OLD_PASSWORD = "old-password-long-enough"
NEW_PASSWORD = "new-password-long-enough"


def _fresh_user() -> None:
    with DB() as session:
        session.query(User).filter(User.email == EMAIL).delete()
        if not session.get(Tenant, "reset-gate"):
            session.add(Tenant(id="reset-gate", name="reset-gate", quota=1000))
        session.add(
            User(
                id="reset-gate-user",
                tenant_id="reset-gate",
                email=EMAIL,
                password_hash=ph.hash(OLD_PASSWORD),
                role="tenant_admin",
            )
        )
        session.commit()


def _user() -> User:
    with DB() as session:
        user = session.scalar(select(User).where(User.email == EMAIL))
        session.expunge(user)
        return user


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    Base.metadata.create_all(engine)
    rate_buckets.clear()
    monkeypatch.delenv("KLYROW_ENV", raising=False)
    monkeypatch.delenv("KLYROW_LOCAL_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("KLYROW_AUTH_RATE_PER_MINUTE", raising=False)
    _fresh_user()
    yield
    rate_buckets.clear()


@pytest.mark.parametrize(
    "env", [("KLYROW_ENV", "production"), ("KLYROW_LOCAL_AUTH_ENABLED", "false")]
)
def test_forgot_and_reset_are_gated_like_legacy_login(monkeypatch, env):
    monkeypatch.setenv(*env)
    login = client.post("/v1/auth/login", json={"email": EMAIL, "password": OLD_PASSWORD})
    forgot = client.post("/v1/auth/forgot-password", json={"email": EMAIL})
    reset = client.post("/v1/auth/reset-password", json={"token": "x", "password": NEW_PASSWORD})
    assert login.status_code == 410 and login.json()["detail"] == "use_canonical_oidc"
    for response in (forgot, reset):
        assert response.status_code == login.status_code
        assert response.json() == login.json()
    assert _user().reset_hash is None


def test_gated_forgot_writes_nothing_for_unknown_accounts(monkeypatch):
    monkeypatch.setenv("KLYROW_ENV", "production")
    known = client.post("/v1/auth/forgot-password", json={"email": EMAIL})
    unknown = client.post("/v1/auth/forgot-password", json={"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 410
    assert known.json() == unknown.json()


def test_repeated_forgot_keeps_pending_token_valid():
    first = client.post("/v1/auth/forgot-password", json={"email": EMAIL})
    assert first.status_code == 202
    user_after_first = _user()
    assert user_after_first.reset_hash and user_after_first.reset_expires
    # Nothing delivers the raw token, so the test takes it over by writing a
    # known hash with the same expiry the handler would have produced.
    from apps.gateway.app.main import sha

    raw = "known-first-token"
    with DB() as session:
        user = session.scalar(select(User).where(User.email == EMAIL))
        user.reset_hash = sha(raw)
        session.commit()

    second = client.post("/v1/auth/forgot-password", json={"email": EMAIL})
    assert second.status_code == 202 and second.json() == first.json()
    assert _user().reset_hash == sha(raw), "second forgot must not clobber a pending reset"

    unknown = client.post("/v1/auth/forgot-password", json={"email": "nobody@example.com"})
    assert unknown.status_code == 202 and unknown.json() == first.json()

    reset = client.post("/v1/auth/reset-password", json={"token": raw, "password": NEW_PASSWORD})
    assert reset.status_code == 200 and reset.json() == {"status": "reset"}
    assert ph.verify(_user().password_hash, NEW_PASSWORD)


def test_forgot_rotates_token_once_pending_reset_expired():
    from apps.gateway.app.main import sha

    with DB() as session:
        user = session.scalar(select(User).where(User.email == EMAIL))
        user.reset_hash = sha("expired-token")
        user.reset_expires = datetime.now(UTC) - timedelta(minutes=1)
        session.commit()
    assert client.post("/v1/auth/forgot-password", json={"email": EMAIL}).status_code == 202
    refreshed = _user()
    assert refreshed.reset_hash != sha("expired-token")
    assert refreshed.reset_expires.replace(tzinfo=UTC) > datetime.now(UTC)


def test_reset_password_is_rate_limited(monkeypatch):
    monkeypatch.setenv("KLYROW_AUTH_RATE_PER_MINUTE", "3")
    payload = {"token": "guess", "password": NEW_PASSWORD}
    statuses = [client.post("/v1/auth/reset-password", json=payload).status_code for _ in range(4)]
    assert statuses == [400, 400, 400, 429]
    assert ("auth", "reset-password", "testclient") in rate_buckets
