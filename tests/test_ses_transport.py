"""No-network tests for Klyrow's opt-in SES transport."""
import asyncio
import smtplib
from pathlib import Path

import pytest

from apps.gateway.app import ses_transport


def payload(recipient="success@simulator.amazonses.com", sender="noreply@codestra.agency"):
    return {"stream": "transactional", "from": sender, "to": [recipient],
            "subject": "Synthetic integration", "plain_body": "SES simulator only.",
            "html_body": "<p>Simulator</p>"}


def allow_simulator(monkeypatch, tmp_path):
    for name in ("KLYROW_PRODUCTION_GATE_APPROVED", "LIVE_EMAIL_DELIVERY",
                 "EXTERNAL_EMAIL_DELIVERY", "PRODUCTION_PROVIDER_ROUTING"):
        monkeypatch.setenv(name, "true")
    monkeypatch.setenv("KLYROW_SAFE_MODE", "false")
    monkeypatch.setenv("KLYROW_EMAIL_TRANSPORT", "ses")
    monkeypatch.setenv("KLYROW_SES_CANARY_ONLY", "true")
    username = tmp_path / "username"
    password = tmp_path / "password"
    username.write_text("SYNTHETICKEY00000001")
    password.write_text("synthetic-test-password-only")
    username.chmod(0o600)
    password.chmod(0o600)
    monkeypatch.setenv("KLYROW_SES_SMTP_USERNAME_FILE", str(username))
    monkeypatch.setenv("KLYROW_SES_SMTP_PASSWORD_FILE", str(password))
    return username, password


@pytest.mark.parametrize("blocked", [
    "KLYROW_SAFE_MODE", "KLYROW_PRODUCTION_GATE_APPROVED", "LIVE_EMAIL_DELIVERY",
    "EXTERNAL_EMAIL_DELIVERY", "PRODUCTION_PROVIDER_ROUTING",
])
def test_all_five_delivery_gates(monkeypatch, tmp_path, blocked):
    allow_simulator(monkeypatch, tmp_path)
    monkeypatch.setenv(blocked, "true" if blocked == "KLYROW_SAFE_MODE" else "false")
    with pytest.raises(RuntimeError, match="ses_live_delivery_gate_closed"):
        ses_transport._submit_smtp(payload(), correlation_id="synthetic")


@pytest.mark.parametrize(("update", "expected"), [
    ({"KLYROW_EMAIL_TRANSPORT": "postal"}, "not_selected"),
    ({"KLYROW_SES_REGION": "us-east-2"}, "region_not_approved"),
    ({"KLYROW_SES_CONFIGURATION_SET": "other"}, "configuration_set_not_approved"),
    ({"KLYROW_SES_CANARY_ONLY": "invalid"}, "canary_flag_invalid"),
    ({"KLYROW_SES_CANARY_ONLY": "false"}, "live_release_not_approved"),
])
def test_bad_configuration_denied(monkeypatch, tmp_path, update, expected):
    allow_simulator(monkeypatch, tmp_path)
    for name, value in update.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=expected):
        ses_transport._submit_smtp(payload(), correlation_id="synthetic")


@pytest.mark.parametrize(("item", "expected"), [
    ({"to": ["customer@example.com"]}, "ses_simulator_only"),
    ({"from": "attacker@example.com"}, "sender_domain_not_approved"),
    ({"stream": "marketing"}, "marketing_not_authorized"),
    ({"to": ["success@simulator.amazonses.com", "other@example.com"]}, "single_recipient_required"),
    ({"subject": "Bad\nHeader"}, "subject_invalid"),
    ({"plain_body": "", "html_body": ""}, "body_required"),
])
def test_unauthorized_payload_denied_before_network(monkeypatch, tmp_path, item, expected):
    allow_simulator(monkeypatch, tmp_path)
    data = payload()
    data.update(item)
    with pytest.raises(RuntimeError, match=expected):
        ses_transport._submit_smtp(data, correlation_id="synthetic")


def test_symlink_credentials_refused(monkeypatch, tmp_path):
    username, _ = allow_simulator(monkeypatch, tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(username)
    monkeypatch.setenv("KLYROW_SES_SMTP_USERNAME_FILE", str(alias))
    with pytest.raises(RuntimeError, match="ses_credential_symlink_denied"):
        ses_transport._submit_smtp(payload(), correlation_id="synthetic")


def test_world_readable_credentials_refused(monkeypatch, tmp_path):
    username, _ = allow_simulator(monkeypatch, tmp_path)
    username.chmod(0o644)
    with pytest.raises(RuntimeError, match="ses_credential_permissions_invalid"):
        ses_transport._submit_smtp(payload(), correlation_id="synthetic")


def test_valid_simulator_uses_tls_auth_and_configuration_header(monkeypatch, tmp_path):
    allow_simulator(monkeypatch, tmp_path)
    events = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            events.append(("open", host, port, timeout))
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            events.append(("close",))
        def ehlo(self):
            events.append(("ehlo",))
        def starttls(self, context):
            assert context is not None
            events.append(("starttls",))
        def login(self, user, password):
            assert user == "SYNTHETICKEY00000001"
            assert password == "synthetic-test-password-only"
            events.append(("login",))
        def mail(self, sender):
            events.append(("mail", sender))
            return 250, b"Ok"
        def rcpt(self, recipient):
            events.append(("rcpt", recipient))
            return 250, b"Ok"
        def data(self, body):
            text = body.decode("utf-8")
            assert "X-SES-CONFIGURATION-SET: codestra-klyrow-transactional" in text
            assert "environment=staging" in text
            assert "SES simulator only." in text
            events.append(("data",))
            return 250, b"Ok synthetic-provider-id-123456"

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    provider_id = asyncio.run(
        ses_transport.send_ses_message(payload(), correlation_id="synthetic"))
    assert provider_id == "synthetic-provider-id-123456"
    assert events[0] == ("open", "email-smtp.us-east-1.amazonaws.com", 587, 12)
    assert [x[0] for x in events].index("starttls") < [x[0] for x in events].index("login")
    assert [x[0] for x in events].index("login") < [x[0] for x in events].index("mail")
    assert events[-1] == ("close",)


def test_rejected_ses_data_not_marked_accepted(monkeypatch, tmp_path):
    allow_simulator(monkeypatch, tmp_path)

    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def ehlo(self):
            pass
        def starttls(self, context):
            pass
        def login(self, username, password):
            pass
        def mail(self, sender):
            return 250, b"Ok"
        def rcpt(self, recipient):
            return 250, b"Ok"
        def data(self, body):
            return 451, b"Temporary failure"

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    with pytest.raises(smtplib.SMTPDataError):
        ses_transport._submit_smtp(payload(), correlation_id="synthetic")


def test_refuse_invalid_payload_type():
    with pytest.raises(RuntimeError, match="ses_payload_invalid"):
        asyncio.run(ses_transport.send_ses_message(None, correlation_id="synthetic"))


def test_postal_default_retained():
    main = Path("apps/gateway/app/main.py").read_text()
    assert 'os.getenv("KLYROW_EMAIL_TRANSPORT","postal")' in main
    assert 'elif transport=="postal"' in main
    assert 'raise RuntimeError("unknown_email_transport")' in main


def test_ses_event_provider_attribution():
    source = Path("apps/gateway/app/main.py").read_text()
    assert '"provider":os.getenv("KLYROW_EMAIL_TRANSPORT","postal")' in source
    assert '"provider":str(payload.get("provider") or os.getenv("KLYROW_EMAIL_TRANSPORT","postal"))' in source
