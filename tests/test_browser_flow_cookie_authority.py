from __future__ import annotations

from http.cookies import SimpleCookie

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from apps.gateway.app.platform import app
from apps.gateway.app import browser_security_fixes as legacy
from apps.gateway.app.browser_flow_cookie_authority import (
    FLOW_COOKIE_PATH,
    FLOW_COOKIE_PREFIX,
    flow_cookie_name,
    remove_flow_cookie,
    require_flow_cookie,
    set_flow_cookie,
)


def _request(cookies: dict[str, str] | None = None) -> Request:
    headers = [(b"host", b"app.klyrow.test")]
    if cookies:
        headers.append(
            (
                b"cookie",
                "; ".join(f"{name}={value}" for name, value in cookies.items()).encode("utf-8"),
            )
        )
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/auth/callback",
            "raw_path": b"/auth/callback",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("app.klyrow.test", 443),
            "state": {},
        }
    )


def _set_cookie_lines(response: Response) -> list[str]:
    return [
        value.decode("utf-8")
        for name, value in response.raw_headers
        if name.lower() == b"set-cookie"
    ]


def _cookie_value(response: Response, name: str) -> str:
    line = next(value for value in _set_cookie_lines(response) if value.startswith(name + "="))
    parsed = SimpleCookie()
    parsed.load(line)
    return parsed[name].value


def test_canonical_composition_installs_per_flow_cookie_authority() -> None:
    assert app.state.klyrow_per_flow_cookie_authority_installed is True
    assert legacy.set_flow_cookie is set_flow_cookie
    assert legacy.require_flow_cookie is require_flow_cookie
    assert legacy.remove_flow_cookie is remove_flow_cookie


def test_new_flow_uses_a_state_specific_host_only_cookie() -> None:
    state = "state-that-must-not-appear-in-cookie-name"
    response = Response()

    set_flow_cookie(response, _request(), state)

    name = flow_cookie_name(state)
    lines = _set_cookie_lines(response)
    exact_line = next(line for line in lines if line.startswith(name + "="))
    mirror_line = next(line for line in lines if line.startswith(legacy.FLOW_COOKIE + "="))
    assert name.startswith(FLOW_COOKIE_PREFIX)
    assert state not in name
    assert _cookie_value(response, name) == legacy.flow_binding(state)
    assert legacy.parse_flow_cookie(_cookie_value(response, legacy.FLOW_COOKIE)) == [
        legacy.flow_binding(state)
    ]
    for line in (exact_line, mirror_line):
        assert "Secure" in line
        assert "HttpOnly" in line
        assert "SameSite=lax" in line
        assert f"Path={FLOW_COOKIE_PATH}" in line
        assert "Domain=" not in line


def test_each_concurrent_flow_has_an_independent_cookie_name() -> None:
    first = flow_cookie_name("first-state")
    second = flow_cookie_name("second-state")

    assert first != second
    assert first.startswith("__Host-")
    assert second.startswith("__Host-")


def test_rollout_mirror_remains_bounded_while_exact_cookies_are_independent() -> None:
    mirror = ""
    states = [f"tab-{index}" for index in range(10)]
    for state in states:
        response = Response()
        request = _request({legacy.FLOW_COOKIE: mirror} if mirror else None)
        set_flow_cookie(response, request, state)
        mirror = _cookie_value(response, legacy.FLOW_COOKIE)
        assert _cookie_value(response, flow_cookie_name(state)) == (legacy.flow_binding(state))

    assert legacy.parse_flow_cookie(mirror) == [
        legacy.flow_binding(state) for state in states[-legacy.FLOW_COOKIE_MAX_BINDINGS :]
    ]


def test_late_callback_cannot_clear_a_newer_flow_binding() -> None:
    old_state = "old-callback-state"
    new_state = "newer-tab-state"
    old_name = flow_cookie_name(old_state)
    new_name = flow_cookie_name(new_state)

    # The old callback request began before the newer tab installed its cookie,
    # so its request contains only the old exact binding. Its response must be
    # incapable of writing or deleting the newer cookie name.
    request = _request({old_name: legacy.flow_binding(old_state)})
    response = Response()
    remove_flow_cookie(response, request, old_state)

    lines = _set_cookie_lines(response)
    assert len(lines) == 1
    assert lines[0].startswith(old_name + "=")
    assert "Max-Age=0" in lines[0]
    assert all(new_name not in line for line in lines)


def test_new_flow_callback_leaves_shared_rollout_mirror_to_ttl() -> None:
    state = "new-flow-state"
    exact_name = flow_cookie_name(state)
    mirror = legacy.encode_flow_cookie([legacy.flow_binding(state)])
    request = _request(
        {
            exact_name: legacy.flow_binding(state),
            legacy.FLOW_COOKIE: mirror,
        }
    )
    response = Response()

    remove_flow_cookie(response, request, state)

    lines = _set_cookie_lines(response)
    assert len(lines) == 1
    assert lines[0].startswith(exact_name + "=")
    assert all(not line.startswith(legacy.FLOW_COOKIE + "=") for line in lines)


def test_exact_per_flow_binding_is_required() -> None:
    state = "required-state"
    name = flow_cookie_name(state)

    require_flow_cookie(
        _request({name: legacy.flow_binding(state)}),
        state,
    )

    with pytest.raises(HTTPException) as denied:
        require_flow_cookie(
            _request(
                {
                    name: legacy.flow_binding("other"),
                    legacy.FLOW_COOKIE: legacy.encode_flow_cookie([legacy.flow_binding(state)]),
                }
            ),
            state,
        )
    assert denied.value.status_code == 403
    assert denied.value.detail == "oidc_flow_cookie_mismatch"
    assert denied.value.headers == {"Cache-Control": "no-store"}


def test_pre_rollout_shared_cookie_remains_read_compatible() -> None:
    state = "legacy-in-flight-state"
    legacy_value = legacy.encode_flow_cookie([legacy.flow_binding(state)])
    request = _request({legacy.FLOW_COOKIE: legacy_value})

    require_flow_cookie(request, state)
    response = Response()
    remove_flow_cookie(response, request, state)

    lines = _set_cookie_lines(response)
    assert any(line.startswith(legacy.FLOW_COOKIE + "=") for line in lines)
    assert all(flow_cookie_name(state) not in line for line in lines)
