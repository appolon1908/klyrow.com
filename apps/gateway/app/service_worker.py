"""Dedicated Klyrow worker processes with private health endpoints."""

import asyncio
import json
import os
import signal
import uuid
from datetime import timedelta

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import select

from .delivery_safety import email_activation_status
from .main import DB, email_outbox_loop, postal_retry_loop, recover_middleware_commands
from .billing import BillingEvent, BillingWorkItem, now
from .mautic_adapter import dispatch_mautic_outbox
from .postal_provisioning import provisioning_tick
from .provider import (
    dispatch_provider_outbox,
    process_one_sandbox,
    recover_expired_leases,
)
from .security_smtp_worker import security_smtp_delivery_loop
from .tenant_postal_delivery import tenant_email_outbox_loop
from .business_event_worker import dispatch as dispatch_business_events
from .campaign_dispatcher import dispatch_campaigns
from .secret_responses import cleanup_secret_responses, refresh_metrics as refresh_secret_metrics
from .telemetry import configure_tracing
from .observability import dispatch_observability_outbox

ROLE = os.getenv("KLYROW_WORKER_ROLE", "mail")
RUNNING = True
_TRUE_VALUES = {"1", "true", "yes", "on"}


def tenant_postal_provisioning_enabled() -> bool:
    """Select tenant credentials only when the complete provisioning stack is enabled."""

    return (
        os.getenv("KLYROW_TENANT_POSTAL_PROVISIONING_ENABLED", "false")
        .strip()
        .lower()
        in _TRUE_VALUES
    )


def selected_email_outbox_loop():
    """Preserve legacy delivery for the base Compose deployment.

    The tenant-scoped loop requires the provider-credential key, Postal bridge,
    provisioning worker, and signed callback attribution route. The optional
    provisioning Compose contract enables the loop only when those pieces are
    deployed together.
    """

    if tenant_postal_provisioning_enabled():
        return tenant_email_outbox_loop
    return email_outbox_loop


async def health(reader, writer):
    try:
        request = await reader.read(4096)
    except Exception:
        request = b""
    if request.startswith(b"GET /metrics "):
        body = generate_latest()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: "
            + CONTENT_TYPE_LATEST.encode()
            + b"\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        return
    body = json.dumps(
        {
            "status": "ok",
            "service": "klyrow-" + ROLE,
            "role": ROLE,
            "email_activation": email_activation_status(),
        }
    ).encode()
    writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\nConnection: close\r\n\r\n"
        + body
    )
    await writer.drain()
    writer.close()
    await writer.wait_closed()


def billing_tick(max_attempts=8):
    with DB() as session:
        for event in session.scalars(
            select(BillingEvent)
            .where(~select(BillingWorkItem.id).where(
                BillingWorkItem.billing_event_id == BillingEvent.id,
            ).exists())
            .order_by(BillingEvent.created_at, BillingEvent.id)
            .limit(200)
        ).all():
            if not session.scalar(
                select(BillingWorkItem).where(
                    BillingWorkItem.billing_event_id == event.id
                )
            ):
                session.add(
                    BillingWorkItem(
                        id=str(uuid.uuid4()),
                        billing_event_id=event.id,
                        tenant_id=event.tenant_id,
                        kind=event.kind,
                    )
                )
        session.commit()
        expired = session.scalars(
            select(BillingWorkItem).where(
                BillingWorkItem.state == "PROCESSING",
                BillingWorkItem.lease_expires_at < now(),
            )
        ).all()
        for item in expired:
            item.state = "DEAD_LETTER" if item.attempts >= max_attempts else "RETRY"
            item.available_at = now() + timedelta(
                seconds=min(900, 2 ** max(item.attempts, 1))
            )
            item.lease_expires_at = None
            item.last_error = "lease_expired"
        session.commit()
        item = session.scalar(
            select(BillingWorkItem)
            .where(
                BillingWorkItem.state.in_(["PENDING", "RETRY"]),
                BillingWorkItem.available_at <= now(),
            )
            .order_by(BillingWorkItem.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not item:
            return 0
        item.state = "PROCESSING"
        item.attempts += 1
        item.lease_expires_at = now() + timedelta(seconds=60)
        session.commit()
        item = session.get(BillingWorkItem, item.id)
        item.state = "COMPLETED"
        item.completed_at = now()
        item.lease_expires_at = None
        item.last_error = None
        session.commit()
        return 1


def secret_response_maintenance_tick() -> int:
    """Redact expired credentials from the always-on base worker."""
    with DB() as session:
        redacted = cleanup_secret_responses(session)
        refresh_secret_metrics(session)
        session.commit()
        return redacted


async def loop():
    while RUNNING:
        try:
            if ROLE == "mail":
                await recover_middleware_commands()
                with DB() as session:
                    recover_expired_leases(session)
                    for _ in range(50):
                        if not process_one_sandbox(session):
                            break
                await dispatch_provider_outbox()
                secret_response_maintenance_tick()
            elif ROLE == "provisioning":
                await provisioning_tick()
            elif ROLE == "billing":
                billing_tick()
            elif ROLE == "scheduler":
                await dispatch_mautic_outbox()
            elif ROLE == "observability":
                await dispatch_observability_outbox()
            elif ROLE == "business":
                await dispatch_business_events()
                secret_response_maintenance_tick()
            elif ROLE == "campaign":
                dispatch_campaigns()
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "level": "error",
                        "service": "klyrow-" + ROLE,
                        "event": "worker_tick_failed",
                        "error": type(exc).__name__,
                    }
                )
            )
        await asyncio.sleep(2 if ROLE != "scheduler" else 10)


async def main():
    global RUNNING
    configure_tracing("klyrow-" + ROLE)
    event = asyncio.Event()

    def stop():
        global RUNNING
        RUNNING = False
        event.set()

    loop_obj = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop_obj.add_signal_handler(sig, stop)
    server = await asyncio.start_server(
        health,
        "0.0.0.0",
        int(os.getenv("KLYROW_WORKER_HEALTH_PORT", "8080")),
    )
    tasks = [asyncio.create_task(loop())]
    if ROLE == "mail":
        tasks.extend(
            [
                asyncio.create_task(postal_retry_loop()),
                asyncio.create_task(selected_email_outbox_loop()()),
                asyncio.create_task(security_smtp_delivery_loop()),
            ]
        )
    await event.wait()
    for task in tasks:
        task.cancel()
    server.close()
    await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
