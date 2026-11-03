"""Fail-closed compatibility and progress fixes for provider reconciliation.

This module is installed by the canonical production composition after all
provider routes are loaded. It preserves the deployed legacy Server A Odoo
route shape and makes bounded dead-letter reconciliation progress without
hiding or resolving lossy records.
"""

from __future__ import annotations

import binascii
from collections import Counter
from typing import Any

from sqlalchemy import case, func, or_, select

from . import main as core
from . import provider

EXPLICIT_ODOO_DESTINATIONS = frozenset({"odoo_helpdesk", "odoo_accounting"})
LEGACY_ODOO_WEBHOOK_DESTINATIONS = {
    "server-a:odoo-support": "odoo_helpdesk",
    "server-a:odoo-helpdesk": "odoo_helpdesk",
    "server-a:odoo-accounting": "odoo_accounting",
}
BLOCKED_RECONCILIATION_MARKER = "operator_reconciliation_required"
_INSTALLED = False


def middleware_destination_kind(route: Any) -> str:
    """Return the closed canonical Server A destination for a route.

    Existing deployments persisted the Odoo destination as a generic webhook.
    Only the exact allowlisted Server A references are upgraded; unrelated
    webhook routes remain local/non-Odoo and can never cross this boundary.
    """

    raw_kind = str(getattr(route, "destination_kind", "") or "").strip()
    kind = raw_kind.lower()
    reference = str(getattr(route, "destination_ref", "") or "").strip().lower()

    if kind == "support":
        return "odoo_helpdesk"
    if kind == "odoo":
        return "odoo_accounting" if "account" in reference else "odoo_helpdesk"
    if kind in EXPLICIT_ODOO_DESTINATIONS:
        return kind
    if kind == "webhook":
        return LEGACY_ODOO_WEBHOOK_DESTINATIONS.get(reference, raw_kind)
    return raw_kind


def middleware_inbound_eligible(route: Any, disposition: str) -> bool:
    """Allow only accepted mail bound to an explicit/allowlisted Odoo route."""

    return (
        disposition == "ACCEPT" and middleware_destination_kind(route) in EXPLICIT_ODOO_DESTINATIONS
    )


def _scoped_inbound_route(session: Any, item: Any, event: Any) -> Any | None:
    """Resolve an inbound route only inside the event's tenant boundary."""

    if str(item.tenant_id) != str(event.tenant_id):
        return None

    route = session.scalar(
        select(core.InboundRouteConfig).where(
            core.InboundRouteConfig.id == item.route_id,
            core.InboundRouteConfig.tenant_id == event.tenant_id,
        )
    )
    if route is not None:
        return route

    from .messaging import InboundRoute

    return session.scalar(
        select(InboundRoute).where(
            InboundRoute.id == item.route_id,
            InboundRoute.tenant_id == event.tenant_id,
        )
    )


def _decode_attachment(attachment: Any) -> bytes | None:
    if (
        not isinstance(attachment, dict)
        or "data_b64" not in attachment
        or not isinstance(attachment["data_b64"], str)
    ):
        return None
    try:
        return provider.base64.b64decode(
            attachment["data_b64"].encode("ascii"),
            validate=True,
        )
    except (UnicodeEncodeError, ValueError, binascii.Error):
        return None


def _normalized_manifest(item: Any) -> list[dict[str, Any]] | None:
    """Return validated relational attachment metadata or ``None``."""

    try:
        raw = provider.json.loads(item.attachments_json or "[]")
    except (TypeError, ValueError):
        return None
    if not isinstance(raw, list):
        return None

    manifest: list[dict[str, Any]] = []
    for attachment in raw:
        if not isinstance(attachment, dict):
            return None
        filename = attachment.get("filename")
        content_type = attachment.get("content_type")
        digest = attachment.get("sha256")
        try:
            size = int(attachment.get("size"))
        except (TypeError, ValueError):
            return None
        if (
            not isinstance(filename, str)
            or not filename
            or not isinstance(content_type, str)
            or not content_type
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in digest)
        ):
            return None
        manifest.append(
            {
                "filename": filename,
                "content_type": content_type,
                "size": size,
                "sha256": digest.lower(),
            }
        )
    return manifest


def _attachment_key(attachment: dict[str, Any]) -> tuple[str, str, int, str]:
    return (
        str(attachment["filename"]),
        str(attachment["content_type"]),
        int(attachment["size"]),
        str(attachment["sha256"]).lower(),
    )


def _authoritative_replay_attachments(
    item: Any,
    payload_attachments: Any,
) -> list[dict[str, Any]] | None:
    """Match every replay byte sequence to the relational attachment manifest."""

    manifest = _normalized_manifest(item)
    if manifest is None or not isinstance(payload_attachments, list):
        return None
    if len(manifest) != len(payload_attachments):
        return None

    supplied: dict[tuple[str, str, int, str], list[dict[str, Any]]] = {}
    for candidate in payload_attachments:
        if not isinstance(candidate, dict):
            return None
        try:
            candidate_metadata = {
                "filename": candidate["filename"],
                "content_type": candidate["content_type"],
                "size": int(candidate["size"]),
                "sha256": str(candidate["sha256"]).lower(),
            }
        except (KeyError, TypeError, ValueError):
            return None
        supplied.setdefault(_attachment_key(candidate_metadata), []).append(candidate)

    if Counter(map(_attachment_key, manifest)) != Counter(
        key for key, values in supplied.items() for _ in values
    ):
        return None

    normalized: list[dict[str, Any]] = []
    for expected in manifest:
        candidates = supplied.get(_attachment_key(expected))
        if not candidates:
            return None
        candidate = candidates.pop()
        decoded = _decode_attachment(candidate)
        if (
            decoded is None
            or len(decoded) != expected["size"]
            or provider.hashlib.sha256(decoded).hexdigest() != expected["sha256"]
        ):
            return None
        normalized.append({**expected, "data_b64": candidate["data_b64"]})
    return normalized


def _upgrade_legacy_inbound_event(
    session: Any,
    event: Any,
) -> tuple[str, dict[str, Any] | None]:
    """Upgrade one inbound event without crossing its tenant boundary."""

    item = session.scalar(
        select(provider.ProviderInbound).where(
            provider.ProviderInbound.id == event.message_id,
            provider.ProviderInbound.tenant_id == event.tenant_id,
        )
    )
    if item is None:
        return "blocked", None

    route = _scoped_inbound_route(session, item, event)
    if route is None:
        return "blocked", None
    if not middleware_inbound_eligible(route, item.disposition):
        return "skipped", None

    try:
        existing = provider.json.loads(event.payload_json)
    except (TypeError, ValueError):
        return "blocked", None
    if not isinstance(existing, dict):
        return "blocked", None

    manifest = _normalized_manifest(item)
    if manifest is None:
        return "blocked", None

    required = ("recipient", "sender", "subject", "destination_kind")
    if all(existing.get(field) is not None for field in required):
        replay_attachments = _authoritative_replay_attachments(
            item,
            existing.get("attachments", []),
        )
        if replay_attachments is None:
            return "blocked", None

        # The relational rows selected through the event tenant are the
        # authority for identity and message metadata. Retain only verified
        # replay bytes and bounded transport metadata from the old payload.
        existing.update(
            {
                "event_id": event.id,
                "event": "inbound.received",
                "tenant_id": item.tenant_id,
                "inbound_id": item.id,
                "provider_event_id": item.provider_event_id,
                "route_id": item.route_id,
                "destination_kind": middleware_destination_kind(route),
                "destination_ref": route.destination_ref,
                "disposition": item.disposition,
                "recipient": item.recipient,
                "sender": item.sender,
                "subject": item.subject,
                "message_id": item.message_id_header,
                "text": item.text_body,
                "html": item.html_body,
                "attachments": replay_attachments,
            }
        )
        return "requeue", existing

    if manifest:
        # Historical rows intentionally retained only attachment hashes. Never
        # fabricate content or acknowledge a lossy replay.
        return "blocked", None

    parsed = {
        "message_id": item.message_id_header,
        "in_reply_to": existing.get("in_reply_to"),
        "references": existing.get("references"),
        "date": existing.get("date"),
        "cc": existing.get("cc"),
        "text": item.text_body,
        "html": item.html_body,
        "attachments": [],
        "attachment_contents": [],
    }
    occurred_at = str(existing.get("occurred_at") or event.created_at.isoformat())
    payload_hash = str(
        existing.get("payload_hash")
        or provider.hashlib.sha256(event.payload_json.encode()).hexdigest()
    )
    upgraded = provider.middleware_inbound_payload(
        event_id=event.id,
        item=item,
        route=route,
        parsed=parsed,
        occurred_at=occurred_at,
        payload_hash=payload_hash,
    )
    return "requeue", upgraded


def _bounded_text(value: Any, default: str, *, limit: int = 500) -> str:
    if not isinstance(value, str):
        return default
    normalized = value.strip()
    return normalized[:limit] if normalized else default


def _bounded_metadata(value: Any) -> dict[str, Any]:
    """Retain only non-authority scalar provider diagnostics."""

    if not isinstance(value, dict):
        return {}
    allowed = {
        "provider_event",
        "provider_message_token",
        "smtp_status",
        "reason",
        "event_source",
    }
    result: dict[str, Any] = {}
    for key in sorted(allowed):
        item = value.get(key)
        if isinstance(item, str):
            result[key] = item[:500]
        elif isinstance(item, (bool, int, float)) or item is None:
            if key in value:
                result[key] = item
    return result


def _bounded_attempt(value: Any) -> int:
    try:
        return max(1, min(int(value), 1000))
    except (TypeError, ValueError):
        return 1


def _upgrade_lifecycle_event(
    session: Any,
    event: Any,
) -> tuple[str, dict[str, Any] | None]:
    """Rebuild a lifecycle payload from tenant-scoped message authority."""

    try:
        existing = provider.json.loads(event.payload_json)
    except (TypeError, ValueError):
        return "blocked", None
    if not isinstance(existing, dict):
        return "blocked", None

    provider_message = session.scalar(
        select(provider.ProviderMessage).where(
            provider.ProviderMessage.id == event.message_id,
            provider.ProviderMessage.tenant_id == event.tenant_id,
        )
    )
    core_message = None
    outbox = None
    if provider_message is None:
        core_message = session.scalar(
            select(core.Message).where(
                core.Message.id == event.message_id,
                core.Message.tenant_id == event.tenant_id,
            )
        )
        if core_message is not None:
            outbox = session.scalar(
                select(core.EmailOutbox).where(
                    core.EmailOutbox.message_id == event.message_id,
                    core.EmailOutbox.tenant_id == event.tenant_id,
                )
            )
    if provider_message is None and core_message is None:
        return "blocked", None

    if provider_message is not None:
        sender = provider_message.sender
        recipient = provider_message.recipient
        correlation_id = provider_message.correlation_id or event.id
        provider_message_id = provider_message.provider_message_id or provider_message.id
        stream = str(provider_message.stream or "TRANSACTIONAL").lower()
        status = str(provider_message.status or event.kind.rsplit(".", 1)[-1]).lower()
    else:
        sender = core_message.sender
        recipient = core_message.recipient
        correlation_id = (
            getattr(outbox, "correlation_id", None) or existing.get("correlation_id") or event.id
        )
        provider_message_id = (
            getattr(outbox, "provider_message_id", None)
            or existing.get("provider_message_id")
            or core_message.id
        )
        stream = _bounded_text(existing.get("stream"), "transactional").lower()
        status = _bounded_text(
            existing.get("canonical_status") or existing.get("status"),
            str(core_message.status or event.kind.rsplit(".", 1)[-1]),
        ).lower()

    occurred_at = _bounded_text(
        existing.get("occurred_at") or existing.get("timestamp"),
        event.created_at.isoformat(),
        limit=100,
    )
    operation_id = _bounded_text(
        getattr(outbox, "operation_id", None) or existing.get("operation_id"),
        event.message_id,
        limit=200,
    )
    correlation_id = _bounded_text(correlation_id, event.id, limit=200)
    causation_id = _bounded_text(
        existing.get("causation_id"),
        correlation_id,
        limit=200,
    )
    provider_name = _bounded_text(existing.get("provider"), "klyrow", limit=100)
    metadata = _bounded_metadata(existing.get("metadata"))

    rebuilt = {
        "event_id": event.id,
        "schema_version": _bounded_text(
            existing.get("schema_version") or existing.get("event_version"),
            "1.0",
            limit=20,
        ),
        "event": event.kind,
        "event_version": _bounded_text(existing.get("event_version"), "1.0", limit=20),
        "occurred_at": occurred_at,
        "timestamp": occurred_at,
        "tenant_id": event.tenant_id,
        "customer_id": event.tenant_id,
        "operation_id": operation_id,
        "message_id": event.message_id,
        "provider_message_id": _bounded_text(
            provider_message_id,
            event.message_id,
            limit=200,
        ),
        "stream": stream,
        "recipient": recipient,
        "sender": sender,
        "recipient_reference": (
            "sha256:" + provider.hashlib.sha256(str(recipient).lower().encode()).hexdigest()
        ),
        "status": status,
        "canonical_status": status,
        "provider": provider_name,
        "correlation_id": correlation_id,
        "causation_id": causation_id,
        "attempt": _bounded_attempt(existing.get("attempt")),
        "metadata": metadata,
    }
    rebuilt["payload_hash"] = provider.hashlib.sha256(
        provider.json.dumps(
            rebuilt,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    return "requeue", rebuilt


def _due_event_predicate(tenant_id: str, current: Any):
    return (
        provider.ProviderEvent.state == "DEAD_LETTER",
        provider.ProviderEvent.tenant_id == tenant_id,
        provider.ProviderEvent.available_at <= current,
    )


def _due_usage_predicate(tenant_id: str, current: Any):
    return (
        provider.ProviderUsageEvent.state == "DEAD_LETTER",
        provider.ProviderUsageEvent.tenant_id == tenant_id,
        provider.ProviderUsageEvent.available_at <= current,
    )


def _unreviewed_event_predicate():
    return or_(
        provider.ProviderEvent.last_error.is_(None),
        provider.ProviderEvent.last_error != BLOCKED_RECONCILIATION_MARKER,
    )


def _event_ordering():
    # Previously classified blocked records remain critically visible, but
    # unreviewed later records always sort ahead of them on the next page.
    return (
        case(
            (
                provider.ProviderEvent.last_error == BLOCKED_RECONCILIATION_MARKER,
                1,
            ),
            else_=0,
        ),
        provider.ProviderEvent.created_at,
        provider.ProviderEvent.id,
    )


def _load_events(
    session: Any,
    *,
    tenant_id: str,
    current: Any,
    limit: int,
    excluded_ids: set[str] | None = None,
) -> list[Any]:
    if limit <= 0:
        return []
    statement = select(provider.ProviderEvent).where(*_due_event_predicate(tenant_id, current))
    if excluded_ids:
        statement = statement.where(provider.ProviderEvent.id.not_in(sorted(excluded_ids)))
    return list(
        session.scalars(
            statement.order_by(*_event_ordering()).limit(limit).with_for_update(skip_locked=True)
        ).all()
    )


def _load_usages(
    session: Any,
    *,
    tenant_id: str,
    current: Any,
    limit: int,
) -> list[Any]:
    if limit <= 0:
        return []
    return list(
        session.scalars(
            select(provider.ProviderUsageEvent)
            .where(*_due_usage_predicate(tenant_id, current))
            .order_by(
                provider.ProviderUsageEvent.created_at,
                provider.ProviderUsageEvent.id,
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
    )


def _count(session: Any, statement: Any) -> int:
    return int(session.scalar(statement) or 0)


def reconcile_provider_outbox_dead_letters(
    session: Any,
    *,
    tenant_id: str,
    limit: int = 50,
    apply: bool = False,
) -> dict[str, int | bool]:
    """Plan or apply one bounded provider-outbox reconciliation page.

    The batch is shared fairly between event and usage queues whenever both
    contain due records. Blocked event rows stay ``DEAD_LETTER`` and keep the
    critical marker, but are sorted behind unreviewed rows on later calls. A
    one-row apply first classifies an unreviewed event, then yields to usage
    while only already-reviewed blocked events remain.
    """

    if not 1 <= limit <= 50:
        raise ValueError("provider_outbox_reconcile_limit_out_of_range")

    current = provider.now()
    due_event_count = _count(
        session,
        select(func.count())
        .select_from(provider.ProviderEvent)
        .where(*_due_event_predicate(tenant_id, current)),
    )
    due_usage_count = _count(
        session,
        select(func.count())
        .select_from(provider.ProviderUsageEvent)
        .where(*_due_usage_predicate(tenant_id, current)),
    )
    unreviewed_due_event_count = _count(
        session,
        select(func.count())
        .select_from(provider.ProviderEvent)
        .where(
            *_due_event_predicate(tenant_id, current),
            _unreviewed_event_predicate(),
        ),
    )

    if due_event_count and due_usage_count:
        if limit == 1:
            event_budget = 1 if unreviewed_due_event_count else 0
            usage_budget = 1 - event_budget
        else:
            event_budget = max(1, limit // 2)
            usage_budget = limit - event_budget
    elif due_event_count:
        event_budget, usage_budget = limit, 0
    else:
        event_budget, usage_budget = 0, limit

    events = _load_events(
        session,
        tenant_id=tenant_id,
        current=current,
        limit=event_budget,
    )
    usages = _load_usages(
        session,
        tenant_id=tenant_id,
        current=current,
        limit=usage_budget,
    )

    # Backfill unused capacity without exceeding the fixed transaction bound.
    unused = limit - len(events) - len(usages)
    if unused and due_event_count > len(events):
        extra_events = _load_events(
            session,
            tenant_id=tenant_id,
            current=current,
            limit=unused,
            excluded_ids={str(item.id) for item in events},
        )
        events.extend(extra_events)
        unused -= len(extra_events)
    if unused and due_usage_count > len(usages):
        # The initial usage query already starts at the oldest row. Fetching a
        # larger prefix and taking the unseen suffix avoids a mutable cursor.
        expanded = _load_usages(
            session,
            tenant_id=tenant_id,
            current=current,
            limit=len(usages) + unused,
        )
        seen = {str(item.id) for item in usages}
        usages.extend(item for item in expanded if str(item.id) not in seen)

    result: dict[str, int | bool] = {
        "apply": apply,
        "examined": 0,
        "events_requeued": 0,
        "events_skipped": 0,
        "events_blocked": 0,
        "usage_requeued": 0,
    }

    for event in events:
        result["examined"] = int(result["examined"]) + 1
        upgraded = None
        if event.kind == "inbound.received":
            decision, upgraded = provider._upgrade_legacy_inbound_event(session, event)
        elif event.kind in core.SMTP_EVENT_MAP:
            decision, upgraded = _upgrade_lifecycle_event(session, event)
        else:
            decision = "blocked"

        key = {
            "requeue": "events_requeued",
            "skipped": "events_skipped",
            "blocked": "events_blocked",
        }[decision]
        result[key] = int(result[key]) + 1
        if not apply:
            continue

        if decision == "skipped":
            event.state = "SKIPPED"
            event.last_error = "middleware_delivery_not_applicable"
        elif decision == "requeue":
            if upgraded is not None:
                event.payload_json = provider.json.dumps(
                    upgraded,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            event.state = "RETRY"
            event.attempts = 0
            event.available_at = current
            event.last_error = None
        else:
            event.state = "DEAD_LETTER"
            event.last_error = BLOCKED_RECONCILIATION_MARKER
        event.updated_at = current

    result["examined"] = int(result["examined"]) + len(usages)
    result["usage_requeued"] = len(usages)
    if apply:
        for usage in usages:
            usage.state = "RETRY"
            usage.attempts = 0
            usage.available_at = current
            usage.last_error = None
        session.flush()

    due_events_remaining = _count(
        session,
        select(func.count())
        .select_from(provider.ProviderEvent)
        .where(*_due_event_predicate(tenant_id, current)),
    )
    due_usages_remaining = _count(
        session,
        select(func.count())
        .select_from(provider.ProviderUsageEvent)
        .where(*_due_usage_predicate(tenant_id, current)),
    )
    unreviewed_events_remaining = _count(
        session,
        select(func.count())
        .select_from(provider.ProviderEvent)
        .where(
            *_due_event_predicate(tenant_id, current),
            _unreviewed_event_predicate(),
        ),
    )
    blocked_events_visible = _count(
        session,
        select(func.count())
        .select_from(provider.ProviderEvent)
        .where(
            provider.ProviderEvent.state == "DEAD_LETTER",
            provider.ProviderEvent.tenant_id == tenant_id,
            provider.ProviderEvent.last_error == BLOCKED_RECONCILIATION_MARKER,
        ),
    )

    result.update(
        {
            "due_events_remaining": due_events_remaining,
            "due_usage_remaining": due_usages_remaining,
            "unreviewed_events_remaining": unreviewed_events_remaining,
            "blocked_events_visible": blocked_events_visible,
            "remaining": due_events_remaining + due_usages_remaining,
            "has_more": bool(due_events_remaining or due_usages_remaining),
        }
    )
    return result


def install_provider_reconciliation_fixes() -> None:
    """Install the compatibility/progress functions into provider endpoints."""

    global _INSTALLED
    if _INSTALLED:
        return
    provider.middleware_destination_kind = middleware_destination_kind
    provider.middleware_inbound_eligible = middleware_inbound_eligible
    provider._upgrade_legacy_inbound_event = _upgrade_legacy_inbound_event
    provider.reconcile_provider_outbox_dead_letters = reconcile_provider_outbox_dead_letters
    _INSTALLED = True


__all__ = [
    "BLOCKED_RECONCILIATION_MARKER",
    "EXPLICIT_ODOO_DESTINATIONS",
    "LEGACY_ODOO_WEBHOOK_DESTINATIONS",
    "_authoritative_replay_attachments",
    "_upgrade_legacy_inbound_event",
    "_upgrade_lifecycle_event",
    "install_provider_reconciliation_fixes",
    "middleware_destination_kind",
    "middleware_inbound_eligible",
    "reconcile_provider_outbox_dead_letters",
]
