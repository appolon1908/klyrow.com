"""No-network tests for signed SES/SNS feedback and tenant-bound outcomes."""

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.gateway.app.ses_feedback import (
    SESFeedback,
    SESFeedbackDenied,
    reconcile_feedback,
    verified_ses_event,
)

TOPIC = "arn:aws:sns:us-east-1:123456789012:codestra-klyrow-ses-events"
CERT_URL = "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-a1b2c3d4.pem"
SEND_ID = "0100000000000000000012345abc"
SNS_ID = "08a1b2c3-d4e5-4f67-b890-123456789abc"


@pytest.fixture
def signer():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SNS fixture")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(priv.public_key())
        .serial_number(123456789)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(priv, hashes.SHA256())
    )
    return priv, cert.public_bytes(serialization.Encoding.PEM)


def signed_body(
    signer, *, event="Delivery", subject=False, changes=None, envelope_changes=None, version="2"
):
    private, _ = signer
    content = {
        "eventType": event,
        "mail": {
            "messageId": SEND_ID,
            "source": "noreply@codestra.agency",
            "destination": ["recipient@example.net"],
            "tags": {"ses:configuration-set": ["codestra-klyrow-transactional"]},
        },
    }
    if event == "Bounce":
        content["bounce"] = {
            "bounceType": "Permanent",
            "bouncedRecipients": [{"emailAddress": "recipient@example.net", "status": "5.1.1"}],
        }
    if event == "Complaint":
        content["complaint"] = {"complainedRecipients": [{"emailAddress": "recipient@example.net"}]}
    if changes:
        changes(content)
    envelope = {
        "Type": "Notification",
        "MessageId": SNS_ID,
        "TopicArn": TOPIC,
        "Timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "Message": json.dumps(content, separators=(",", ":")),
        "SignatureVersion": version,
        "SigningCertURL": CERT_URL,
    }
    if subject:
        envelope["Subject"] = "SES Event"
    if envelope_changes:
        envelope_changes(envelope)
    # AWS's Notification string-to-sign sorts the selected field names.
    # Keep this signer independent of the verifier's CANONICAL_FIELDS tuple.
    signed_fields = {"Message", "MessageId", "Subject", "Type", "Timestamp", "TopicArn"}
    signed = "".join(
        k + "\n" + str(envelope[k]) + "\n" for k in sorted(signed_fields & envelope.keys())
    ).encode()
    signature = private.sign(
        signed, padding.PKCS1v15(), hashes.SHA256() if version == "2" else hashes.SHA1()
    )
    envelope["Signature"] = base64.b64encode(signature).decode()
    return json.dumps(envelope)


@pytest.mark.parametrize(
    "event,status",
    [
        ("Send", "provider_accepted"),
        ("Delivery", "delivered"),
        ("Bounce", "bounced"),
        ("Complaint", "complained"),
        ("Reject", "failed"),
        ("DeliveryDelay", "deferred"),
        ("Rendering Failure", "failed"),
    ],
)
def test_real_sns_signature_event_types(signer, event, status):
    feedback = verified_ses_event(
        signed_body(signer, event=event), topic_arn=TOPIC, certificate_loader=lambda _: signer[1]
    )
    assert feedback.status == status and feedback.sender == "noreply@codestra.agency"


def test_legacy_sns_v1_still_requires_valid_rsa(signer):
    event = verified_ses_event(
        signed_body(signer, version="1"), topic_arn=TOPIC, certificate_loader=lambda _: signer[1]
    )
    assert event.status == "delivered"


def test_optional_subject_has_canonical_signing(signer):
    event = verified_ses_event(
        signed_body(signer, subject=True), topic_arn=TOPIC, certificate_loader=lambda _: signer[1]
    )
    assert event.event_type == "Delivery"


@pytest.mark.parametrize(
    "change,matcher",
    [
        (
            lambda e: e.update(
                {"TopicArn": "arn:aws:sns:us-west-2:123456789012:codestra-klyrow-ses-events"}
            ),
            "topic",
        ),
        (
            lambda e: e.update({"SigningCertURL": "https://evil.example.com/cert.pem"}),
            "certificate",
        ),
        (lambda e: e.update({"Message": "{}"}), "signature"),
        (lambda e: e.update({"Signature": "QUFBQQ=="}), "signature"),
        (lambda e: e.update({"SignatureVersion": "9"}), "version"),
        (lambda e: e.update({"Type": "SubscriptionConfirmation"}), "topic"),
    ],
)
def test_modified_envelope_is_rejected(signer, change, matcher):
    body = signed_body(signer)
    env = json.loads(body)
    change(env)
    with pytest.raises(SESFeedbackDenied, match=matcher):
        verified_ses_event(json.dumps(env), topic_arn=TOPIC, certificate_loader=lambda _: signer[1])


def test_unrelated_sns_topic_denied_even_when_correctly_signed(signer):
    body = signed_body(
        signer,
        envelope_changes=lambda e: e.update(
            {"TopicArn": "arn:aws:sns:us-east-1:123456789012:other-topic"}
        ),
    )
    with pytest.raises(SESFeedbackDenied, match="topic"):
        verified_ses_event(body, topic_arn=TOPIC, certificate_loader=lambda _: signer[1])


def test_sns_subject_nested_json_untrusted(signer):
    def unexpected(p):
        p["mail"]["tags"] = {"ses:configuration-set": ["another-config"]}

    body = signed_body(signer, changes=unexpected)
    with pytest.raises(SESFeedbackDenied, match="config_set"):
        verified_ses_event(body, topic_arn=TOPIC, certificate_loader=lambda _: signer[1])


def test_ses_sender_and_recipient_mismatch(signer):
    for fn in (
        lambda e: e["mail"].update({"source": "attacker@evil.example"}),
        lambda e: e["mail"].update(
            {"destination": ["victim@example.net", "recipient@example.net"]}
        ),
        lambda e: e.update({"eventType": "Untracked"}),
    ):
        body = signed_body(signer, changes=fn)
        with pytest.raises(SESFeedbackDenied):
            verified_ses_event(body, topic_arn=TOPIC, certificate_loader=lambda _: signer[1])


def test_transient_bounce_must_not_suppress(signer):
    body = signed_body(
        signer, event="Bounce", changes=lambda e: e["bounce"].update({"bounceType": "Transient"})
    )
    assert (
        verified_ses_event(body, topic_arn=TOPIC, certificate_loader=lambda _: signer[1]).status
        == "deferred"
    )


def _session_db():
    from apps.gateway.app.main import Base

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _seed_db(factory):
    from apps.gateway.app.main import EmailOutbox, Message, Tenant

    with factory() as s:
        s.add(Tenant(id="tenant-a", name="A", quota=100))
        s.add(Tenant(id="tenant-b", name="B", quota=100))
        s.add(
            Message(
                id="local-message",
                tenant_id="tenant-a",
                recipient="recipient@example.net",
                sender="noreply@codestra.agency",
                subject="Example",
                status="provider_accepted",
            )
        )
        s.add(
            EmailOutbox(
                id="outbox",
                tenant_id="tenant-a",
                message_id="local-message",
                provider_message_id=SEND_ID,
                payload="{}",
                state="delivered",
            )
        )
        s.commit()


def _event(
    status, *, sns_id=SNS_ID, sender="noreply@codestra.agency", recipient="recipient@example.net"
):
    return SESFeedback(
        sns_message_id=sns_id,
        provider_message_id=SEND_ID,
        event_type=status,
        status=status,
        sender=sender,
        recipient=recipient,
        raw_status="",
        permanent_bounce=status == "bounced",
    )


def test_unmatched_event_does_not_mutate_other_tenant():
    from apps.gateway.app.main import Event, Message

    factory = _session_db()
    _seed_db(factory)
    assert (
        reconcile_feedback(
            _event("bounced", recipient="other@example.net"), session_factory=factory
        )
        == "unmatched"
    )
    with factory() as s:
        assert s.get(Message, "local-message").status == "provider_accepted"
        assert s.query(Event).count() == 0


def test_verified_delivery_ack_and_duplicate():
    from apps.gateway.app.main import Event, Message, Replay

    factory = _session_db()
    _seed_db(factory)
    e = _event("delivered")
    assert reconcile_feedback(e, session_factory=factory) == "ack"
    assert reconcile_feedback(e, session_factory=factory) == "duplicate"
    with factory() as s:
        assert s.get(Message, "local-message").status == "delivered"
        assert s.query(Event).count() == 1 and s.query(Replay).count() == 1


def test_bounce_suppresses_only_matched_tenant():
    from apps.gateway.app.main import Message, Suppression

    factory = _session_db()
    _seed_db(factory)
    assert reconcile_feedback(_event("bounced"), session_factory=factory) == "ack"
    with factory() as s:
        suppression = s.query(Suppression).all()
        assert len(suppression) == 1 and suppression[0].tenant_id == "tenant-a"
        assert s.get(Message, "local-message").status == "bounced"


def test_late_send_never_overwrites_delivery():
    from apps.gateway.app.main import Message

    factory = _session_db()
    _seed_db(factory)
    assert reconcile_feedback(_event("delivered"), session_factory=factory) == "ack"
    assert (
        reconcile_feedback(
            _event("provider_accepted", sns_id="99887766-5544-4eea-aaaa-111122223333"),
            session_factory=factory,
        )
        == "ack"
    )
    with factory() as s:
        assert s.get(Message, "local-message").status == "delivered"


@pytest.mark.parametrize(
    "first,later",
    [
        ("complained", "delivered"),
        ("bounced", "delivered"),
        ("failed", "delivered"),
        ("complained", "bounced"),
        ("complained", "failed"),
        ("bounced", "failed"),
    ],
)
def test_late_event_preserves_stronger_terminal_outcome(first, later):
    from apps.gateway.app.main import Message, Suppression

    factory = _session_db()
    _seed_db(factory)
    assert reconcile_feedback(_event(first), session_factory=factory) == "ack"
    assert (
        reconcile_feedback(
            _event(later, sns_id="late-terminal-event-12345"), session_factory=factory
        )
        == "ack"
    )
    with factory() as session:
        assert session.get(Message, "local-message").status == first
        if first in {"bounced", "complained"}:
            assert session.query(Suppression).one().reason == first


def test_duplicate_provider_id_across_tenants_fail_closed():
    from apps.gateway.app.main import EmailOutbox, Event, Message

    factory = _session_db()
    _seed_db(factory)
    with factory() as s:
        s.add(
            Message(
                id="other-message",
                tenant_id="tenant-b",
                recipient="other@example.net",
                sender="noreply@codestra.agency",
                subject="Other",
                status="provider_accepted",
            )
        )
        s.add(
            EmailOutbox(
                id="other-outbox",
                tenant_id="tenant-b",
                message_id="other-message",
                provider_message_id=SEND_ID,
                payload="{}",
                state="delivered",
            )
        )
        s.commit()
    assert reconcile_feedback(_event("complained"), session_factory=factory) == "unmatched"
    with factory() as s:
        assert s.query(Event).count() == 0
        assert s.get(Message, "local-message").status == "provider_accepted"


@pytest.mark.parametrize(
    "event,change",
    [
        ("Complaint", lambda p: p.update({"complaint": None})),
        ("Bounce", lambda p: p["bounce"].update({"bounceType": "Ambiguous"})),
    ],
)
def test_malformed_event_content_denied(signer, event, change):
    body = signed_body(signer, event=event, changes=change)
    with pytest.raises(SESFeedbackDenied):
        verified_ses_event(body, topic_arn=TOPIC, certificate_loader=lambda _: signer[1])


@pytest.mark.parametrize("status", ["delivered", "bounced", "complained", "failed", "deferred"])
def test_feedback_persists_correlated_callback_before_ack(status, monkeypatch):
    from apps.gateway.app.main import EmailOutbox
    from apps.gateway.app.provider import ProviderEvent

    monkeypatch.setenv("KLYROW_EMAIL_TRANSPORT", "ses")
    factory = _session_db()
    _seed_db(factory)
    with factory() as session:
        outbox = session.get(EmailOutbox, "outbox")
        outbox.operation_id = "middleware-operation"
        outbox.correlation_id = "middleware-correlation"
        outbox.attempts = 3
        session.commit()
    assert reconcile_feedback(_event(status), session_factory=factory) == "ack"
    assert reconcile_feedback(_event(status), session_factory=factory) == "duplicate"
    with factory() as session:
        callback = session.query(ProviderEvent).one()
        payload = json.loads(callback.payload_json)
        assert (callback.tenant_id, callback.message_id, callback.kind, callback.state) == (
            "tenant-a",
            "local-message",
            "email." + status,
            "PENDING",
        )
        assert payload["operation_id"] == "middleware-operation"
        assert payload["correlation_id"] == "middleware-correlation"
        assert payload["provider_message_id"] == SEND_ID
        assert payload["attempt"] == 3
        assert payload["provider"] == "ses"
        assert "recipient@example.net" not in callback.payload_json


@pytest.mark.parametrize(
    "first,later",
    [
        ("delivered", "provider_accepted"),
        ("delivered", "deferred"),
        ("delivered", "delivered"),
        ("bounced", "delivered"),
        ("complained", "bounced"),
        ("failed", "delivered"),
    ],
)
def test_ignored_feedback_does_not_duplicate_outcomes(first, later):
    from apps.gateway.app.main import Event, Message, Replay
    from apps.gateway.app.provider import ProviderEvent

    factory = _session_db()
    _seed_db(factory)
    assert reconcile_feedback(_event(first), session_factory=factory) == "ack"
    ignored = _event(later, sns_id="distinct-delayed-notification-12345")
    assert reconcile_feedback(ignored, session_factory=factory) == "ack"
    assert reconcile_feedback(ignored, session_factory=factory) == "duplicate"
    with factory() as session:
        assert session.get(Message, "local-message").status == first
        assert session.query(Event).one().kind == "klyrow.email." + first
        assert session.query(ProviderEvent).one().kind == "email." + first
        assert session.query(Replay).count() == 2


def test_stronger_terminal_feedback_still_emits_its_own_outcome():
    from apps.gateway.app.main import Event, Message
    from apps.gateway.app.provider import ProviderEvent

    factory = _session_db()
    _seed_db(factory)
    for status in ("delivered", "bounced", "complained"):
        assert (
            reconcile_feedback(_event(status, sns_id="outcome-" + status), session_factory=factory)
            == "ack"
        )
    with factory() as session:
        assert session.get(Message, "local-message").status == "complained"
        assert {row.kind for row in session.query(Event)} == {
            "klyrow.email.delivered",
            "klyrow.email.bounced",
            "klyrow.email.complained",
        }
        assert {row.kind for row in session.query(ProviderEvent)} == {
            "email.delivered",
            "email.bounced",
            "email.complained",
        }


def test_callback_failure_rolls_back_feedback_and_replay(monkeypatch):
    from apps.gateway.app import main
    from apps.gateway.app.provider import ProviderEvent

    factory = _session_db()
    _seed_db(factory)
    queue_callback = main.queue_email_lifecycle_event

    def fail_after_queue(*args, **kwargs):
        queue_callback(*args, **kwargs)
        raise RuntimeError("synthetic callback persistence failure")

    with monkeypatch.context() as patch:
        patch.setattr(main, "queue_email_lifecycle_event", fail_after_queue)
        with pytest.raises(RuntimeError, match="synthetic callback persistence failure"):
            reconcile_feedback(_event("bounced"), session_factory=factory)
    with factory() as session:
        assert session.get(main.Message, "local-message").status == "provider_accepted"
        for model in (main.Event, main.Replay, main.Suppression, ProviderEvent):
            assert session.query(model).count() == 0
    assert reconcile_feedback(_event("bounced"), session_factory=factory) == "ack"
