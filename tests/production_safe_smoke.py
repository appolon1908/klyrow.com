"""Controlled deployed smoke test. No Postal submission occurs while safe mode is true."""

import hashlib
import hmac
import json
import os
import time
import uuid

import httpx
from app.main import DB, Domain, Tenant, User, select

base = "http://127.0.0.1:8000"
with DB() as session:
    user = session.scalar(select(User).where(User.role == "platform_admin"))
    tenant = session.get(Tenant, user.tenant_id)
    domain = session.scalar(
        select(Domain).where(Domain.tenant_id == tenant.id, Domain.domain == "klyrow.com")
    )
    if not domain:
        domain = Domain(
            id=str(uuid.uuid4()),
            tenant_id=tenant.id,
            domain="klyrow.com",
            token="controlled-smoke",
            verified=True,
        )
        session.add(domain)
    else:
        domain.verified = True
    session.commit()
    tenant_id = tenant.id

login = httpx.post(
    base + "/v1/auth/login",
    json={
        "email": os.environ["KLYROW_ADMIN_EMAIL"],
        "password": os.environ["KLYROW_ADMIN_PASSWORD"],
    },
)
assert login.status_code == 200, login.text
headers = {
    "Authorization": "Bearer " + login.json()["access_token"],
    "Idempotency-Key": "controlled-smoke-" + str(uuid.uuid4()),
}
send = httpx.post(
    base + "/v1/email/send",
    headers=headers,
    json={
        "to": "controlled@example.net",
        "sender": "noreply@klyrow.com",
        "subject": "Controlled safe-mode test",
        "html": "<p>No external delivery</p>",
        "text": "No external delivery",
    },
)
assert send.status_code == 202, send.text
assert send.json()["safe_mode"] is True
body = json.dumps(
    {
        "event": "email.delivered",
        "message_id": send.json()["id"],
        "tenant_id": tenant_id,
        "recipient": "controlled@example.net",
        "provider": "mock",
        "status": "delivered",
    },
    separators=(",", ":"),
)
timestamp = str(int(time.time()))
event_id = str(uuid.uuid4())
signature = hmac.new(
    os.environ["KLYROW_WEBHOOK_SECRET"].encode(),
    f"{timestamp}.{event_id}.{body}".encode(),
    hashlib.sha256,
).hexdigest()
hook_headers = {
    "X-Klyrow-Timestamp": timestamp,
    "X-Klyrow-Event-Id": event_id,
    "X-Klyrow-Signature": signature,
    "Content-Type": "application/json",
}
good = httpx.post(base + "/v1/webhooks/postal", headers=hook_headers, content=body)
replay = httpx.post(base + "/v1/webhooks/postal", headers=hook_headers, content=body)
bad = httpx.post(
    base + "/v1/webhooks/postal",
    headers={**hook_headers, "X-Klyrow-Event-Id": str(uuid.uuid4()), "X-Klyrow-Signature": "bad"},
    content=body,
)
assert (
    good.status_code == 202
    and replay.status_code == 202
    and replay.json().get("duplicate") is True
    and bad.status_code == 401
), (good.text, replay.text, bad.text)
print(
    json.dumps(
        {
            "login": "PASS",
            "safe_send": "PASS",
            "carrier_submission": False,
            "signed_event": "PASS",
            "replay": "PASS",
            "bad_hmac": "PASS",
            "message_id": send.json()["id"],
        }
    )
)
