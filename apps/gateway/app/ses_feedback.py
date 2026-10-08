"""Verified SNS -> SQS -> tenant-bound SES delivery event reconciliation.

No sending. No queue reads/deletes are performed unless the separate worker is
explicitly enabled and supplied an AWS-authenticated SQS client.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit

import requests
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

MAX_SQS_BYTES = 256_000
TOPIC_SUFFIX = ":codestra-klyrow-ses-events"
REGION = "us-east-1"
CONFIGURATION_SET = "codestra-klyrow-transactional"
SOURCE_DOMAINS = frozenset({"codestra.agency", "klyrow.com"})
CANONICAL_FIELDS = ("Message", "MessageId", "Subject", "Type", "Timestamp", "TopicArn")
SIMPLE_TYPES = {
    "Send": "provider_accepted",
    "Delivery": "delivered",
    "Complaint": "complained",
    "Reject": "failed",
    "DeliveryDelay": "deferred",
    "Rendering Failure": "failed",
}
SOFT_STATUSES = frozenset({"provider_accepted", "deferred"})
TERMINAL_STATUSES = frozenset({"delivered", "bounced", "complained", "failed"})


class SESFeedbackDenied(ValueError):
    """Invalid or unauthenticated feedback must remain in SQS for investigation."""


@dataclass(frozen=True)
class SESFeedback:
    sns_message_id: str
    provider_message_id: str
    event_type: str
    status: str
    sender: str
    recipient: str
    raw_status: str
    permanent_bounce: bool


def _load_object(raw: str | bytes, *, limit: int) -> dict:
    if not isinstance(raw, (str, bytes)) or len(raw) > limit:
        raise SESFeedbackDenied("ses_event_size_invalid")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SESFeedbackDenied("ses_event_json_invalid") from exc
    if not isinstance(value, dict):
        raise SESFeedbackDenied("ses_event_shape_invalid")
    return value


def _sns_cert_url(raw: object) -> str:
    if not isinstance(raw, str) or len(raw) > 250:
        raise SESFeedbackDenied("sns_certificate_url_invalid")
    url = urlsplit(raw)
    if (
        url.scheme != "https"
        or url.hostname != "sns.us-east-1.amazonaws.com"
        or url.port is not None
        or url.username or url.password or url.query or url.fragment
        or not re.fullmatch(r"/SimpleNotificationService-[a-fA-F0-9]+\.pem", url.path)
    ):
        raise SESFeedbackDenied("sns_certificate_url_invalid")
    return raw


def fetch_sns_certificate(url: str) -> bytes:
    """Only contact the exact approved AWS SNS TLS origin; never follow redirects."""
    safe_url = _sns_cert_url(url)
    r = requests.get(safe_url, timeout=(3, 5), allow_redirects=False)
    if r.status_code != 200 or len(r.content) > 24_000:
        raise SESFeedbackDenied("sns_certificate_unavailable")
    return r.content


def verified_ses_event(sqs_body: str, *, topic_arn: str, certificate_loader=fetch_sns_certificate) -> SESFeedback:
    """Verify SNS RSA signature before interpreting any SES or recipient fields."""
    envelope = _load_object(sqs_body, limit=MAX_SQS_BYTES)
    if (
        envelope.get("Type") != "Notification"
        or envelope.get("TopicArn") != topic_arn
        or not re.fullmatch(r"arn:aws:sns:us-east-1:[0-9]{12}:codestra-klyrow-ses-events", topic_arn)
    ):
        raise SESFeedbackDenied("sns_topic_or_type_denied")
    version = str(envelope.get("SignatureVersion"))
    if version not in {"1", "2"}:  # AWS signed SHA1 legacy / preferred signed SHA256
        raise SESFeedbackDenied("sns_signature_version_invalid")
    if not all(isinstance(envelope.get(k), str) and envelope[k] for k in ("Message", "MessageId", "TopicArn", "Timestamp", "Signature")):
        raise SESFeedbackDenied("sns_required_field_missing")
    if not re.fullmatch(r"[A-Za-z0-9-]{15,100}", envelope["MessageId"]):
        raise SESFeedbackDenied("sns_message_id_invalid")
    try:
        when = datetime.fromisoformat(envelope["Timestamp"].replace("Z", "+00:00"))
        if when.tzinfo is None:
            raise ValueError
    except ValueError as exc:
        raise SESFeedbackDenied("sns_timestamp_invalid") from exc
    if when > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise SESFeedbackDenied("sns_future_timestamp_denied")
    parts = []
    for key in CANONICAL_FIELDS:
        if key == "Subject" and key not in envelope:
            continue
        if key != "Subject" and key not in envelope:
            raise SESFeedbackDenied("sns_field_missing")
        parts.append(key + "\n" + str(envelope[key]) + "\n")
    signed = "".join(parts).encode("utf-8")
    try:
        signature = base64.b64decode(envelope["Signature"], validate=True)
        cert_url = _sns_cert_url(envelope.get("SigningCertURL"))
        cert = x509.load_pem_x509_certificate(certificate_loader(cert_url))
        now = datetime.now(timezone.utc)
        if not (cert.not_valid_before_utc <= now <= cert.not_valid_after_utc):
            raise SESFeedbackDenied("sns_certificate_expired")
        key = cert.public_key()
        if not isinstance(key, rsa.RSAPublicKey):
            raise SESFeedbackDenied("sns_key_type_invalid")
        algorithm = hashes.SHA256() if version == "2" else hashes.SHA1()
        key.verify(signature, signed, padding.PKCS1v15(), algorithm)
    except SESFeedbackDenied:
        raise
    except (ValueError, TypeError, InvalidSignature, UnicodeError) as exc:
        raise SESFeedbackDenied("sns_signature_invalid") from exc

    payload = _load_object(envelope["Message"], limit=MAX_SQS_BYTES)
    event_type = payload.get("eventType")
    mail = payload.get("mail")
    if not isinstance(mail, dict):
        raise SESFeedbackDenied("ses_mail_missing")
    provider_id = mail.get("messageId")
    if not isinstance(provider_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{10,180}", provider_id):
        raise SESFeedbackDenied("ses_provider_id_invalid")
    sender = mail.get("source")
    destinations = mail.get("destination")
    if not isinstance(sender, str) or sender.count("@") != 1 or sender.rsplit("@",1)[1].lower() not in SOURCE_DOMAINS:
        raise SESFeedbackDenied("ses_sender_unapproved")
    if not isinstance(destinations, list) or len(destinations) != 1 or not isinstance(destinations[0], str):
        raise SESFeedbackDenied("ses_destination_invalid")
    recipient = destinations[0].lower()
    if "@" not in recipient or len(recipient) > 254:
        raise SESFeedbackDenied("ses_destination_invalid")
    tags = mail.get("tags", {})
    if not isinstance(tags, dict) or tags.get("ses:configuration-set") != [CONFIGURATION_SET]:
        raise SESFeedbackDenied("ses_config_set_mismatch")
    if event_type == "Bounce":
        bounce = payload.get("bounce", {})
        if not isinstance(bounce, dict):
            raise SESFeedbackDenied("ses_bounce_invalid")
        entries = bounce.get("bouncedRecipients", [])
        if not isinstance(entries, list) or not any(
            isinstance(e, dict) and str(e.get("emailAddress", "")).lower() == recipient for e in entries
        ):
            raise SESFeedbackDenied("ses_bounce_recipient_invalid")
        if bounce.get("bounceType") not in {"Permanent", "Transient"}:
            raise SESFeedbackDenied("ses_bounce_type_invalid")
        permanent = bounce["bounceType"] == "Permanent"
        status = "bounced" if permanent else "deferred"
        raw_status = str(next((e.get("status") for e in entries if e.get("emailAddress", "").lower() == recipient), ""))[:30]
    elif event_type in SIMPLE_TYPES:
        permanent = False
        status = SIMPLE_TYPES[event_type]
        raw_status = ""
        if event_type == "Complaint":
            complaint = payload.get("complaint")
            if not isinstance(complaint, dict):
                raise SESFeedbackDenied("ses_complaint_invalid")
            complaints = complaint.get("complainedRecipients", [])
            if not isinstance(complaints, list) or not any(
                isinstance(e,dict) and str(e.get("emailAddress","")).lower()==recipient for e in complaints
            ):
                raise SESFeedbackDenied("ses_complaint_recipient_invalid")
    else:
        raise SESFeedbackDenied("ses_event_type_unhandled")
    return SESFeedback(
        sns_message_id=envelope["MessageId"], provider_message_id=provider_id,
        event_type=str(event_type), status=status, sender=sender.lower(),
        recipient=recipient, raw_status=raw_status, permanent_bounce=permanent,
    )


def reconcile_feedback(event: SESFeedback, *, session_factory=None) -> str:
    """Commit only a matched, same-tenant, same-sender/recipient event.

    Return 'ack' or 'duplicate' only after commit. 'unmatched' remains in SQS.
    """
    from .main import Audit, DB, EmailOutbox, Event, Message, Replay, Suppression, set_core_message_status
    from .webmail import update_outbound_status
    factory = session_factory or DB
    replay_id = "ses-sns:" + hashlib.sha256(event.sns_message_id.encode()).hexdigest()
    event_id = "ses-event:" + hashlib.sha256(event.sns_message_id.encode()).hexdigest()
    with factory() as session:
        if session.get(Replay, replay_id):
            return "duplicate"
        matches = session.scalars(
            select(EmailOutbox)
            .where(EmailOutbox.provider_message_id == event.provider_message_id)
            .limit(2).with_for_update()
        ).all()
        if len(matches) != 1:
            # No arbitrary tenant selection if a provider ID was duplicated.
            return "unmatched"
        outbox = matches[0]
        message = session.get(Message, outbox.message_id)
        if (
            message is None or message.tenant_id != outbox.tenant_id
            or message.sender.lower() != event.sender
            or message.recipient.lower() != event.recipient
        ):
            return "unmatched"
        # Do not regress a delivered/bounced/complained record from a late Send/delay.
        status = event.status
        if message.status in TERMINAL_STATUSES and status in SOFT_STATUSES:
            status = message.status
        set_core_message_status(message, status)
        if status in TERMINAL_STATUSES:
            outbox.state = "delivered" if status == "delivered" else "failed"
        outbox.updated_at = datetime.now(timezone.utc)
        if status in {"bounced", "complained"}:
            existing = session.scalar(select(Suppression).where(Suppression.tenant_id == outbox.tenant_id, Suppression.email == event.recipient))
            if existing is None:
                import uuid
                session.add(Suppression(id=str(uuid.uuid4()), tenant_id=outbox.tenant_id, email=event.recipient, reason=status))
            else:
                existing.reason = status
        payload = json.dumps({"provider":"ses", "status":status, "event_type":event.event_type, "provider_id_hash":hashlib.sha256(event.provider_message_id.encode()).hexdigest()},separators=(",",":"))
        session.add(Event(id=event_id, tenant_id=outbox.tenant_id, message_id=outbox.message_id, kind="klyrow.email."+status, payload=payload))
        import uuid
        session.add(Audit(id=str(uuid.uuid4()), tenant_id=outbox.tenant_id, actor="provider:ses", action="email.status."+status))
        session.add(Replay(id=replay_id))
        update_outbound_status(session, outbox.tenant_id, outbox.message_id, status)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            if session.get(Replay,replay_id):
                return "duplicate"
            raise
    return "ack"
