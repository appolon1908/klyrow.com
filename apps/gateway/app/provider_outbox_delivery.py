"""Bounded callback dispatch with per-record failure isolation and claim fencing."""

import json
from datetime import timedelta

from sqlalchemy import select


async def dispatch(limit=50):
    from .main import SMTP_EVENT_MAP
    from . import main, provider

    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
        raise ValueError("provider_outbox_limit_out_of_range")
    result = {"events_delivered": 0, "events_failed": 0, "usage_delivered": 0, "usage_failed": 0}
    for model, prefix in (
        (provider.ProviderEvent, "events"),
        (provider.ProviderUsageEvent, "usage"),
    ):
        current = provider.now()
        # Usage already has available_at: while PROCESSING it is the claim
        # deadline. Event claims retain their existing updated_at lease.
        is_event = model is provider.ProviderEvent
        lease_column = model.updated_at if is_event else model.available_at
        expired_before = current - timedelta(minutes=5) if is_event else current
        claim = current if is_event else current + timedelta(minutes=5)
        with main.DB() as session:
            stale = session.scalars(
                select(model)
                .where(
                    model.state == "PROCESSING",
                    lease_column < expired_before,
                )
                .with_for_update(skip_locked=True)
                .limit(limit)
            ).all()
            for item in stale:
                item.state = "RETRY"
                item.available_at = current
                item.last_error = "delivery_lease_expired"
            session.flush()
            items = session.scalars(
                select(model)
                .where(
                    model.state.in_(["PENDING", "RETRY"]),
                    model.available_at <= current,
                )
                .order_by(model.created_at, model.id)
                .with_for_update(skip_locked=True)
                .limit(limit)
            ).all()
            snapshots = []
            for item in items:
                item.state = "PROCESSING"
                setattr(item, lease_column.key, claim)
                if is_event:
                    snapshots.append((item.id, item.kind, item.payload_json))
                else:
                    snapshots.append(
                        (
                            item.id,
                            "klyrow.usage.recorded",
                            {
                                "event_id": item.id,
                                "usage_event_id": item.id,
                                "tenant_id": item.tenant_id,
                                "message_id": item.message_id,
                                "stream": item.stream,
                                "billable_units": item.billable_units,
                                "timestamp": item.created_at.isoformat(),
                                "provider_result_category": item.result_category,
                            },
                        )
                    )
            session.commit()
        for item_id, kind, raw_payload in snapshots:
            ok = False
            error = (
                "server_a_delivery_failed" if is_event else "billing_control_plane_delivery_failed"
            )
            try:
                payload = json.loads(raw_payload) if is_event else raw_payload
                if not isinstance(payload, dict):
                    raise ValueError("payload_not_object")
                # Missing legacy IDs must not become a new UUID on every retry.
                if not payload.get("event_id"):
                    payload["event_id"] = item_id
                event_type = SMTP_EVENT_MAP.get(kind) if is_event else "klyrow.usage.recorded"
                if not event_type:
                    error = "unsupported_provider_event_kind"
                else:
                    ok = await main.emit_middleware(event_type, payload)
            except (TypeError, ValueError):
                error = "invalid_provider_event_payload"
            except Exception:
                # Do not include exception messages, payloads or secret values.
                # CancelledError is a BaseException and leaves a recoverable lease.
                error = "middleware_delivery_exception"
            with main.DB() as session:
                item = session.scalar(
                    select(model)
                    .where(
                        model.id == item_id,
                        model.state == "PROCESSING",
                        lease_column == claim,
                    )
                    .with_for_update()
                )
                if item is None:
                    continue  # A newer worker owns the claim after lease expiry.
                item.attempts += 1
                item.last_error = None if ok else error
                if is_event:
                    item.updated_at = provider.now()
                if ok:
                    item.state = "DELIVERED"
                else:
                    item.state = "DEAD_LETTER" if item.attempts >= 8 else "RETRY"
                    item.available_at = provider.now() + timedelta(
                        seconds=min(900, 2 ** min(item.attempts, 10))
                    )
                session.commit()
                result[prefix + ("_delivered" if ok else "_failed")] += 1
    return result


def status(session, tenant_id):
    """Return tenant-scoped counts without mail content or recipient identities."""
    from sqlalchemy import func

    from .provider import ProviderEvent, ProviderUsageEvent

    queues = {}
    for name, model in (("events", ProviderEvent), ("usage", ProviderUsageEvent)):
        counts = dict(
            session.execute(
                select(model.state, func.count())
                .where(model.tenant_id == tenant_id)
                .group_by(model.state)
            ).all()
        )
        queues[name] = {
            state: counts.get(state, 0)
            for state in ("PENDING", "PROCESSING", "RETRY", "DEAD_LETTER", "DELIVERED", "SKIPPED")
        }
    return {
        "version": 1,
        "scope": "tenant",
        "queues": queues,
        "reconciliation_required": any(q["DEAD_LETTER"] for q in queues.values()),
        "delivery_verified": False,
    }
