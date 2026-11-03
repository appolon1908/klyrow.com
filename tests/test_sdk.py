import hashlib
import hmac
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "sdk" / "python"))
from klyrow import Klyrow, KlyrowError, verify_webhook


def test_python_client_preserves_tenant_and_idempotency_without_logging_secret():
    seen = {}

    def transport(method, url, headers, body):
        seen.update(method=method, url=url, headers=headers, body=body)
        return 202, {"X-Request-Id": "request-1"}, b'{"id":"message-1","status":"QUEUED"}'

    client = Klyrow("secret-token", "tenant-a", transport=transport)
    result = client.send(
        {"sender": "sender@example.com", "to": "recipient@example.net"}, "send-key-0001"
    )
    assert result["id"] == "message-1" and seen["method"] == "POST"
    assert seen["headers"]["X-Klyrow-Tenant-Id"] == "tenant-a"
    assert seen["headers"]["Idempotency-Key"] == "send-key-0001"
    assert json.loads(seen["body"])["sender"] == "sender@example.com"


def test_python_client_structured_error_contract():
    def transport(method, url, headers, body):
        return (
            409,
            {"X-Request-Id": "request-conflict"},
            b'{"detail":{"error_code":"altered_replay","message":"payload differs"}}',
        )

    try:
        Klyrow("token", transport=transport).send({}, "send-key-0002")
    except KlyrowError as error:
        assert (
            error.status == 409
            and error.error_code == "altered_replay"
            and error.request_id == "request-conflict"
        )
    else:
        raise AssertionError("structured error expected")


def test_webhook_signature_and_replay_window():
    secret = "webhook-secret"
    timestamp = "1700000000"
    event_id = "event-1"
    body = b'{"type":"message.delivered"}'
    signature = hmac.new(
        secret.encode(), timestamp.encode() + b"." + event_id.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    assert verify_webhook(secret, timestamp, event_id, body, "sha256=" + signature, now=1700000001)
    assert not verify_webhook(secret, timestamp, event_id, body, signature, now=1700001000)
    assert not verify_webhook(secret, timestamp, event_id, b"tampered", signature, now=1700000001)
