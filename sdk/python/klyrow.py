"""Small dependency-light Klyrow API reference client."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


@dataclass
class KlyrowError(Exception):
    status: int
    error_code: str
    message: str
    request_id: str | None = None
    details: Any = None

    def __str__(self) -> str:
        return f"{self.status} {self.error_code}: {self.message}"


Transport = Callable[
    [str, str, Mapping[str, str], bytes | None], tuple[int, Mapping[str, str], bytes]
]


def _urllib_transport(method: str, url: str, headers: Mapping[str, str], body: bytes | None):
    request = Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - caller chooses base URL
            return response.status, dict(response.headers), response.read()
    except HTTPError as error:
        return error.code, dict(error.headers), error.read()


class Klyrow:
    def __init__(
        self,
        token: str,
        tenant_id: str | None = None,
        base_url: str = "https://api.klyrow.com",
        transport: Transport = _urllib_transport,
    ):
        if not token:
            raise ValueError("token is required")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.tenant_id = tenant_id
        self.transport = transport

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if self.tenant_id:
            headers["X-Klyrow-Tenant-Id"] = self.tenant_id
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        body = None
        if json_body is not None:
            body = json.dumps(json_body, separators=(",", ":"), sort_keys=True).encode()
            headers["Content-Type"] = "application/json"
        status, response_headers, raw = self.transport(method, self.base_url + path, headers, body)
        payload = json.loads(raw or b"{}")
        if status >= 400:
            detail = payload.get("detail", payload)
            if isinstance(detail, dict):
                code = str(detail.get("error_code", "request_failed"))
                message = str(detail.get("message", code))
                details = detail.get("details")
            else:
                code = str(detail)
                message = str(detail)
                details = None
            raise KlyrowError(status, code, message, response_headers.get("X-Request-Id"), details)
        return payload

    def send(self, message: dict, idempotency_key: str) -> dict:
        return self.request(
            "POST", "/v1/messages", json_body=message, idempotency_key=idempotency_key
        )

    def message(self, message_id: str) -> dict:
        return self.request("GET", f"/v1/messages/{message_id}")

    def messages(self, *, limit: int = 50, cursor: str | None = None) -> dict:
        suffix = f"?limit={limit}" + (f"&cursor={cursor}" if cursor else "")
        return self.request("GET", "/v1/messages" + suffix)

    def create_webhook(self, url: str, events: list[str]) -> dict:
        return self.request(
            "POST", "/v1/webhook-subscriptions", json_body={"url": url, "events": events}
        )


def verify_webhook(
    secret: str,
    timestamp: str,
    event_id: str,
    body: bytes,
    signature: str,
    *,
    now: int | None = None,
    tolerance_seconds: int = 300,
) -> bool:
    try:
        issued = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = int(time.time()) if now is None else now
    if abs(current - issued) > tolerance_seconds:
        return False
    signed = timestamp.encode() + b"." + event_id.encode() + b"." + body
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.removeprefix("sha256="))
