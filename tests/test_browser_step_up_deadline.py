from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from apps.gateway.app.platform import app
from apps.gateway.app import auth_bff, browser_security_fixes
from apps.gateway.app.auth_bff import BrowserSession
from apps.gateway.app.browser_step_up_deadline import (
    stage_new_session_with_parent_deadline,
)


class _ParentSession:
    def __init__(self, parent=None, *, forbid_get: bool = False) -> None:
        self.parent = parent
        self.forbid_get = forbid_get
        self.lookups: list[tuple[object, object]] = []

    def get(self, model, key):
        if self.forbid_get:
            raise AssertionError("non-rotation session performed a parent lookup")
        self.lookups.append((model, key))
        return self.parent


def _parent(*, expires_at, revoked_at=None):
    return SimpleNamespace(
        id="parent-session",
        expires_at=expires_at,
        revoked_at=revoked_at,
    )


def _install_fake_stage(monkeypatch, child):
    observed = []

    def fake_stage(
        session,
        request,
        identity,
        user,
        membership,
        tokens,
        rotated_from_id=None,
    ):
        observed.append(
            {
                "session": session,
                "request": request,
                "identity": identity,
                "user": user,
                "membership": membership,
                "tokens": tokens,
                "rotated_from_id": rotated_from_id,
            }
        )
        return child, "raw-session", "csrf-token"

    monkeypatch.setattr(
        "apps.gateway.app.browser_step_up_deadline._ORIGINAL_STAGE_NEW_SESSION",
        fake_stage,
    )
    return observed


def _stage(session, *, rotated_from_id="parent-session"):
    return stage_new_session_with_parent_deadline(
        session,
        object(),
        SimpleNamespace(id="identity"),
        SimpleNamespace(id="user"),
        SimpleNamespace(id="membership"),
        {"id_token": "test-token"},
        rotated_from_id=rotated_from_id,
    )


def test_canonical_composition_installs_step_up_deadline_guard() -> None:
    assert app.state.klyrow_step_up_deadline_guard_installed is True
    assert browser_security_fixes._stage_new_session is stage_new_session_with_parent_deadline


def test_rotated_child_cannot_outlive_parent(monkeypatch) -> None:
    fixed_now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    parent_expiry = fixed_now + timedelta(minutes=5)
    child = SimpleNamespace(expires_at=fixed_now + timedelta(hours=8))
    observed = _install_fake_stage(monkeypatch, child)
    monkeypatch.setattr(auth_bff, "now", lambda: fixed_now)
    session = _ParentSession(_parent(expires_at=parent_expiry))

    item, raw, csrf = _stage(session)

    assert item is child
    assert item.expires_at == parent_expiry
    assert raw == "raw-session"
    assert csrf == "csrf-token"
    assert observed[0]["rotated_from_id"] == "parent-session"
    assert session.lookups == [(BrowserSession, "parent-session")]


def test_shorter_child_deadline_is_preserved(monkeypatch) -> None:
    fixed_now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    child_expiry = fixed_now + timedelta(minutes=2)
    child = SimpleNamespace(expires_at=child_expiry)
    _install_fake_stage(monkeypatch, child)
    monkeypatch.setattr(auth_bff, "now", lambda: fixed_now)
    session = _ParentSession(_parent(expires_at=fixed_now + timedelta(minutes=5)))

    item, _raw, _csrf = _stage(session)

    assert item.expires_at == child_expiry


def test_normal_login_keeps_standard_session_ttl_without_parent_lookup(
    monkeypatch,
) -> None:
    child_expiry = datetime(2026, 9, 5, 20, 0, tzinfo=UTC)
    child = SimpleNamespace(expires_at=child_expiry)
    observed = _install_fake_stage(monkeypatch, child)
    session = _ParentSession(forbid_get=True)

    item, _raw, _csrf = _stage(session, rotated_from_id=None)

    assert item.expires_at == child_expiry
    assert observed[0]["rotated_from_id"] is None


@pytest.mark.parametrize(
    ("parent", "now"),
    [
        (None, datetime(2026, 9, 5, 12, 0, tzinfo=UTC)),
        (
            _parent(expires_at=datetime(2026, 9, 5, 11, 59, tzinfo=UTC)),
            datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
        ),
        (
            _parent(
                expires_at=datetime(2026, 9, 5, 12, 5, tzinfo=UTC),
                revoked_at=datetime(2026, 9, 5, 11, 58, tzinfo=UTC),
            ),
            datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
        ),
    ],
)
def test_missing_expired_or_revoked_parent_fails_closed(
    monkeypatch,
    parent,
    now,
) -> None:
    child = SimpleNamespace(expires_at=now + timedelta(hours=8))
    _install_fake_stage(monkeypatch, child)
    monkeypatch.setattr(auth_bff, "now", lambda: now)

    with pytest.raises(HTTPException) as denied:
        _stage(_ParentSession(parent))

    assert denied.value.status_code == 401
    assert denied.value.detail == "step_up_session_invalid"
    assert denied.value.headers == {"Cache-Control": "no-store"}


def test_naive_database_deadline_is_normalized_to_utc(monkeypatch) -> None:
    fixed_now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    naive_parent_expiry = datetime(2026, 9, 5, 12, 5)
    child = SimpleNamespace(expires_at=fixed_now + timedelta(hours=8))
    _install_fake_stage(monkeypatch, child)
    monkeypatch.setattr(auth_bff, "now", lambda: fixed_now)

    item, _raw, _csrf = _stage(_ParentSession(_parent(expires_at=naive_parent_expiry)))

    assert item.expires_at == naive_parent_expiry.replace(tzinfo=UTC)
