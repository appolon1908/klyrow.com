"""Fail-closed Amazon SES SMTP transport behind the canonical Klyrow outbox."""
from __future__ import annotations

import asyncio
import os
import re
import smtplib
import ssl
import stat
from email.message import EmailMessage
from pathlib import Path

from .delivery_safety import live_email_delivery_enabled
from .provider_outcome import ProviderSubmissionIndeterminate

SES_HOST = "email-smtp.us-east-1.amazonaws.com"
SES_PORT = 587
SES_REGION = "us-east-1"
SES_CONFIG_SET = "codestra-klyrow-transactional"
SIMULATOR_RECIPIENT = "success@simulator.amazonses.com"
ALLOWED_DOMAINS = frozenset({"codestra.agency", "klyrow.com"})


def _secret_file(key: str) -> str:
    value = os.getenv(key, "")
    if not value or not os.path.isabs(value):
        raise RuntimeError("ses_credential_file_required")
    path = Path(value)
    if path.is_symlink():
        raise RuntimeError("ses_credential_symlink_denied")
    try:
        meta = path.stat()
        if not stat.S_ISREG(meta.st_mode) or meta.st_mode & 0o077 or meta.st_size > 2048:
            raise RuntimeError("ses_credential_permissions_invalid")
        secret = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("ses_credential_unavailable") from exc
    if not 8 <= len(secret) <= 512 or "\n" in secret or "\r" in secret:
        raise RuntimeError("ses_credential_invalid")
    return secret


def _address(value: object) -> str:
    if not isinstance(value, str) or len(value) > 254:
        raise RuntimeError("ses_address_invalid")
    if "\r" in value or "\n" in value or value.count("@") != 1:
        raise RuntimeError("ses_address_invalid")
    if not re.fullmatch(r"[A-Za-z0-9._+%-]+@[A-Za-z0-9.-]+", value):
        raise RuntimeError("ses_address_invalid")
    return value.lower()


def _submit_smtp(payload: dict, *, correlation_id: str) -> str:
    if os.getenv("KLYROW_EMAIL_TRANSPORT", "postal") != "ses":
        raise RuntimeError("ses_transport_not_selected")
    if not live_email_delivery_enabled():
        raise RuntimeError("ses_live_delivery_gate_closed")
    if os.getenv("KLYROW_SES_REGION", SES_REGION) != SES_REGION:
        raise RuntimeError("ses_region_not_approved")
    if os.getenv("KLYROW_SES_CONFIGURATION_SET", SES_CONFIG_SET) != SES_CONFIG_SET:
        raise RuntimeError("ses_configuration_set_not_approved")
    if payload.get("stream") != "transactional":
        raise RuntimeError("ses_marketing_not_authorized")

    sender = _address(payload.get("from"))
    if sender.rsplit("@", 1)[1] not in ALLOWED_DOMAINS:
        raise RuntimeError("ses_sender_domain_not_approved")
    recipients = payload.get("to")
    if not isinstance(recipients, list) or len(recipients) != 1:
        raise RuntimeError("ses_single_recipient_required")
    recipient = _address(recipients[0])
    canary_only = os.getenv("KLYROW_SES_CANARY_ONLY", "true").strip().lower()
    if canary_only == "true":
        if recipient != SIMULATOR_RECIPIENT:
            raise RuntimeError("ses_simulator_only")
    elif canary_only == "false":
        if os.getenv("KLYROW_SES_LIVE_APPROVED") != "true":
            raise RuntimeError("ses_live_release_not_approved")
    else:
        raise RuntimeError("ses_canary_flag_invalid")

    subject = payload.get("subject")
    if not isinstance(subject, str) or not 1 <= len(subject) <= 200 or "\r" in subject or "\n" in subject:
        raise RuntimeError("ses_subject_invalid")
    plain = payload.get("plain_body") or ""
    html = payload.get("html_body") or ""
    if not isinstance(plain, str) or not isinstance(html, str) or len(plain) + len(html) > 200_000:
        raise RuntimeError("ses_payload_invalid")
    if not plain and not html:
        raise RuntimeError("ses_body_required")

    username = _secret_file("KLYROW_SES_SMTP_USERNAME_FILE")
    password = _secret_file("KLYROW_SES_SMTP_PASSWORD_FILE")
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["X-SES-CONFIGURATION-SET"] = SES_CONFIG_SET
    msg["X-SES-MESSAGE-TAGS"] = "application=klyrow,environment=" + ("staging" if canary_only == "true" else "production")
    msg.set_content(plain or "HTML content available.")
    if html:
        msg.add_alternative(html, subtype="html")

    provider_id = None
    submission_error = None
    try:
        with smtplib.SMTP(SES_HOST, SES_PORT, timeout=12) as client:
            client.ehlo()
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
            client.login(username, password)
            status, details = client.mail(sender)
            if status != 250:
                raise smtplib.SMTPSenderRefused(status, details, sender)
            status, details = client.rcpt(recipient)
            if status not in (250, 251):
                raise smtplib.SMTPRecipientsRefused({recipient: (status, details)})
            try:
                status, details = client.data(msg.as_bytes())
            except smtplib.SMTPDataError:
                # Explicit rejection of DATA before the body was transmitted.
                raise
            except OSError:
                # smtplib disconnects also derive from OSError. Once DATA is
                # attempted, acceptance cannot be disproved by a lost reply.
                submission_error = ProviderSubmissionIndeterminate(
                    "ses_submission_outcome_unknown"
                )
                raise submission_error from None
            if 400 <= status <= 599:
                raise smtplib.SMTPDataError(status, details)
            if status != 250:
                submission_error = ProviderSubmissionIndeterminate(
                    "ses_submission_reply_unrecognized"
                )
                raise submission_error from None
            response = details.decode("ascii", "ignore") if isinstance(details, bytes) else str(details)
            match = re.search(r"\bOk\s+([A-Za-z0-9_-]{10,150})\b", response)
            if not match:
                # Acceptance is known, but a local correlation ID must never be
                # invented as the SES ID used by authenticated feedback.
                submission_error = ProviderSubmissionIndeterminate(
                    "ses_accepted_provider_id_unavailable"
                )
                raise submission_error from None
            provider_id = match.group(1)
    except Exception:
        # QUIT/cleanup is outside the mail acceptance boundary. It must not
        # replace either a known acceptance or an earlier uncertain outcome.
        if provider_id is not None:
            return provider_id
        if submission_error is not None:
            raise submission_error from None
        raise
    return provider_id


async def send_ses_message(payload: dict, *, correlation_id: str) -> str:
    """Use a worker thread to avoid blocking the asynchronous outbox loop."""
    if not isinstance(payload, dict):
        raise RuntimeError("ses_payload_invalid")
    return await asyncio.to_thread(_submit_smtp, payload, correlation_id=correlation_id)
