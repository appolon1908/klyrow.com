"""PostgreSQL-only proofs for PaymentAttempt locking semantics."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from apps.gateway.app.billing import Invoice
from apps.gateway.app.main import Audit, Tenant
from apps.gateway.app.payment_attempts import (
    CAPTURED,
    PENDING,
    PaymentAttempt,
    PaymentAttemptEvent,
    PaymentAttemptTransitionIn,
    transition_payment_attempt,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("KLYROW_CONTRACT_POSTGRES_URL"),
    reason="Requires disposable PostgreSQL",
)


@pytest.fixture
def payment_database(monkeypatch, tmp_path):
    admin = create_engine(os.environ["KLYROW_CONTRACT_POSTGRES_URL"])
    schema = "payment_attempt_" + uuid.uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        os.environ["KLYROW_CONTRACT_POSTGRES_URL"],
        connect_args={"options": f"-csearch_path={schema}"},
    )
    for model in (Tenant, Invoice, PaymentAttempt, PaymentAttemptEvent, Audit):
        model.__table__.create(engine)

    secret = tmp_path / "stripe-secret"
    secret.write_text("SYNTHETIC-FIXTURE-VALUE-NOT-A-CREDENTIAL")
    monkeypatch.setenv("KLYROW_BILLING_ENABLED", "true")
    monkeypatch.setenv("KLYROW_STRIPE_ENABLED", "true")
    monkeypatch.setenv("KLYROW_STRIPE_SECRET_FILE", str(secret))
    monkeypatch.setenv("KLYROW_LIVE_CHARGING_ENABLED", "true")

    invoice_id = str(uuid.uuid4())
    attempt_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    with Session(engine) as session:
        session.add(Tenant(id="tenant", name="Synthetic", quota=100))
        session.add(Invoice(
            id=invoice_id, number="INV-CONCURRENCY", tenant_id="tenant",
            subscription_id="subscription", currency="USD",
            subtotal=Decimal("10.00"), tax=Decimal("0.00"),
            discount=Decimal("0.00"), credits=Decimal("0.00"),
            total=Decimal("10.00"), status="OPEN",
            due_at=datetime.now(timezone.utc) + timedelta(days=1),
        ))
        for index, attempt_id in enumerate(attempt_ids):
            session.add(PaymentAttempt(
                id=attempt_id, tenant_id="tenant", invoice_id=invoice_id,
                provider="stripe", idempotency_key=f"key-{index}",
                request_fingerprint=f"fingerprint-{index}", amount_minor=600,
                currency="USD", status=PENDING, created_by="admin",
            ))
        session.commit()
    try:
        yield engine, attempt_ids
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_concurrent_captures_cannot_overpay_one_invoice(payment_database):
    engine, attempt_ids = payment_database
    barrier = Barrier(2)

    def capture(attempt_id):
        with Session(engine) as session:
            barrier.wait(timeout=10)
            try:
                result = transition_payment_attempt(
                    attempt_id,
                    PaymentAttemptTransitionIn(target_status=CAPTURED),
                    {"tenant": "root", "sub": "admin"},
                    session,
                )
                return result["status"]
            except HTTPException as error:
                return error.status_code, error.detail

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(capture, attempt_ids))

    assert sorted(outcomes, key=str) == sorted(
        [CAPTURED, (409, "amount_exceeds_remaining_balance")], key=str
    )
    with Session(engine) as session:
        captured = session.scalars(
            select(PaymentAttempt).where(PaymentAttempt.status == CAPTURED)
        ).all()
        assert len(captured) == 1

