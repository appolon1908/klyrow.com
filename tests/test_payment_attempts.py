"""Mission 02: provider-neutral PaymentAttempt foundation tests."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

pytestmark = pytest.mark.usefixtures("canonical_api_owner")

from fastapi.testclient import TestClient

from apps.gateway.app.main import Base, DB, Tenant, User, app, engine, ph, rate_buckets
from apps.gateway.app.billing import Invoice, Payment
from apps.gateway.app.payment_attempts import (
    AUTHORIZED,
    CANCELLED,
    CAPTURED,
    CREATED,
    EXPIRED,
    FAILED,
    PENDING,
    REQUIRES_ACTION,
    TERMINAL_STATES,
    TRANSITIONS,
    PaymentAttempt,
    PaymentAttemptEvent,
    PaymentAttemptConflict,
    _apply_transition,
    _minor_from_decimal,
    _decimal_from_minor,
)

client = TestClient(app)
tokens = {}


def setup_module():
    rate_buckets.clear()
    tokens.clear()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with DB() as session:
        for tenant_id, role in (("a", "tenant_admin"), ("b", "tenant_admin"), ("root", "platform_admin")):
            session.add(Tenant(id=tenant_id, name=tenant_id, quota=10000))
            session.add(
                User(
                    id=tenant_id,
                    tenant_id=tenant_id,
                    email=f"{tenant_id}@example.com",
                    password_hash=ph.hash("long-enough-password"),
                    role=role,
                )
            )
        session.commit()


def login(email):
    if email in tokens:
        return tokens[email]
    response = client.post("/v1/auth/login", json={"email": email, "password": "long-enough-password"})
    assert response.status_code == 200, response.text
    tokens[email] = {"Authorization": "Bearer " + response.json()["access_token"]}
    return tokens[email]


def make_invoice(tenant_id, *, total="100.00", currency="USD", status="OPEN", credits="0.00"):
    inv = Invoice(
        id=str(uuid.uuid4()),
        number="INV-" + uuid.uuid4().hex[:8].upper(),
        tenant_id=tenant_id,
        subscription_id="test-subscription",
        currency=currency,
        subtotal=Decimal(total),
        tax=Decimal("0.00"),
        discount=Decimal("0.00"),
        credits=Decimal(credits),
        total=Decimal(total),
        status=status,
        due_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    with DB() as session:
        session.add(inv)
        session.commit()
    return inv.id


def enable_billing(monkeypatch):
    monkeypatch.setenv("KLYROW_BILLING_ENABLED", "true")


def create_attempt(headers, *, invoice_id, amount_minor=1000, currency="USD", provider="disabled",
                    idempotency_key=None, payment_method_reference_id=None):
    payload = {
        "invoice_id": invoice_id,
        "amount_minor": amount_minor,
        "currency": currency,
        "provider": provider,
    }
    if payment_method_reference_id:
        payload["payment_method_reference_id"] = payment_method_reference_id
    request_headers = dict(headers)
    request_headers["Idempotency-Key"] = idempotency_key or ("key-" + uuid.uuid4().hex)
    return client.post("/v1/billing/payment-attempts", json=payload, headers=request_headers)


# ---------------------------------------------------------------------------
# State machine unit tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "start,target",
    [
        (CREATED, PENDING), (CREATED, CANCELLED), (CREATED, EXPIRED),
        (PENDING, REQUIRES_ACTION), (PENDING, AUTHORIZED), (PENDING, CAPTURED),
        (PENDING, FAILED), (PENDING, CANCELLED), (PENDING, EXPIRED),
        (REQUIRES_ACTION, PENDING), (REQUIRES_ACTION, AUTHORIZED), (REQUIRES_ACTION, CAPTURED),
        (REQUIRES_ACTION, FAILED), (REQUIRES_ACTION, CANCELLED), (REQUIRES_ACTION, EXPIRED),
        (AUTHORIZED, CAPTURED), (AUTHORIZED, FAILED), (AUTHORIZED, CANCELLED), (AUTHORIZED, EXPIRED),
    ],
)
def test_every_documented_valid_transition_is_allowed(start, target):
    assert target in TRANSITIONS[start]


@pytest.mark.parametrize(
    "start,target",
    [
        (CREATED, AUTHORIZED), (CREATED, CAPTURED), (CREATED, REQUIRES_ACTION),
        (CAPTURED, PENDING), (FAILED, PENDING), (CANCELLED, PENDING), (EXPIRED, PENDING),
        (AUTHORIZED, PENDING), (AUTHORIZED, REQUIRES_ACTION),
    ],
)
def test_every_undocumented_transition_is_rejected(start, target):
    assert target not in TRANSITIONS.get(start, frozenset())


def test_terminal_states_have_no_outgoing_transitions():
    for state in TERMINAL_STATES:
        assert TRANSITIONS[state] == frozenset()


def _fresh_attempt(status=CREATED, **overrides):
    attempt = PaymentAttempt(
        id=str(uuid.uuid4()),
        tenant_id="a",
        invoice_id="inv",
        provider="disabled",
        idempotency_key="k-" + uuid.uuid4().hex,
        request_fingerprint="fp",
        amount_minor=1000,
        currency="USD",
        status=status,
        created_by="tester",
        version=1,
    )
    for key, value in overrides.items():
        setattr(attempt, key, value)
    return attempt


def test_apply_transition_valid_updates_status_and_timestamp():
    attempt = _fresh_attempt(CREATED)
    ctx = {"sub": "tester"}
    _apply_transition(_FakeSession(), attempt, PENDING, event_type="t", source="test", ctx=ctx)
    assert attempt.status == PENDING
    assert attempt.version == 2


def test_apply_transition_invalid_raises_conflict():
    attempt = _fresh_attempt(CREATED)
    with pytest.raises(PaymentAttemptConflict):
        _apply_transition(_FakeSession(), attempt, CAPTURED, event_type="t", source="test", ctx={"sub": "tester"})


def test_apply_transition_from_terminal_state_raises_conflict():
    attempt = _fresh_attempt(CAPTURED)
    with pytest.raises(PaymentAttemptConflict):
        _apply_transition(_FakeSession(), attempt, FAILED, event_type="t", source="test", ctx={"sub": "tester"})


def test_apply_transition_identical_replay_is_idempotent_and_appends_no_event():
    attempt = _fresh_attempt(PENDING)
    session = _FakeSession()
    _apply_transition(session, attempt, PENDING, event_type="t", source="test", ctx={"sub": "tester"})
    assert attempt.status == PENDING
    assert attempt.version == 1
    assert session.added == []


class _FakeSession:
    def __init__(self):
        self.added = []

    def add(self, item):
        self.added.append(item)

    def scalar(self, *_args, **_kwargs):
        return None


def test_minor_unit_conversion_boundary_round_trips():
    assert _minor_from_decimal(Decimal("10.00")) == 1000
    assert _decimal_from_minor(1000) == Decimal("10.00")
    assert _minor_from_decimal(Decimal("0.01")) == 1


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------

def test_creation_fails_closed_when_billing_disabled():
    invoice_id = make_invoice("a")
    response = create_attempt(login("a@example.com"), invoice_id=invoice_id)
    assert response.status_code == 503
    assert response.json()["detail"] == "billing_disabled"


def test_creation_requires_idempotency_key(monkeypatch):
    enable_billing(monkeypatch)
    invoice_id = make_invoice("a")
    response = client.post(
        "/v1/billing/payment-attempts",
        json={"invoice_id": invoice_id, "amount_minor": 1000, "currency": "USD", "provider": "disabled"},
        headers=login("a@example.com"),
    )
    assert response.status_code == 422


def test_creation_rejects_malformed_idempotency_key(monkeypatch):
    enable_billing(monkeypatch)
    invoice_id = make_invoice("a")
    headers = dict(login("a@example.com"))
    headers["Idempotency-Key"] = "short"
    response = client.post(
        "/v1/billing/payment-attempts",
        json={"invoice_id": invoice_id, "amount_minor": 1000, "currency": "USD", "provider": "disabled"},
        headers=headers,
    )
    assert response.status_code == 422


def test_authenticated_creation_succeeds(monkeypatch):
    enable_billing(monkeypatch)
    invoice_id = make_invoice("a")
    response = create_attempt(login("a@example.com"), invoice_id=invoice_id, amount_minor=2500)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == CREATED
    assert body["amount_minor"] == 2500
    assert body["currency"] == "USD"
    assert body["invoice_id"] == invoice_id
    assert "provider_attempt_reference" in body


def test_unauthenticated_creation_is_rejected(monkeypatch):
    enable_billing(monkeypatch)
    invoice_id = make_invoice("a")
    response = client.post(
        "/v1/billing/payment-attempts",
        json={"invoice_id": invoice_id, "amount_minor": 1000, "currency": "USD", "provider": "disabled"},
        headers={"Idempotency-Key": "key-" + uuid.uuid4().hex},
    )
    assert response.status_code == 401


def test_identical_idempotent_replay_returns_original(monkeypatch):
    enable_billing(monkeypatch)
    invoice_id = make_invoice("a")
    key = "replay-" + uuid.uuid4().hex
    first = create_attempt(login("a@example.com"), invoice_id=invoice_id, idempotency_key=key)
    second = create_attempt(login("a@example.com"), invoice_id=invoice_id, idempotency_key=key)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]


def test_conflicting_idempotency_key_reuse_is_rejected(monkeypatch):
    enable_billing(monkeypatch)
    invoice_id = make_invoice("a")
    key = "conflict-" + uuid.uuid4().hex
    first = create_attempt(login("a@example.com"), invoice_id=invoice_id, idempotency_key=key, amount_minor=1000)
    second = create_attempt(login("a@example.com"), invoice_id=invoice_id, idempotency_key=key, amount_minor=2000)
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["detail"] == "idempotency_key_payload_mismatch"


def test_missing_invoice_is_rejected(monkeypatch):
    enable_billing(monkeypatch)
    response = create_attempt(login("a@example.com"), invoice_id="does-not-exist")
    assert response.status_code == 404


def test_cross_tenant_invoice_is_rejected(monkeypatch):
    enable_billing(monkeypatch)
    invoice_id = make_invoice("b")
    response = create_attempt(login("a@example.com"), invoice_id=invoice_id)
    assert response.status_code == 404


def test_currency_mismatch_is_rejected(monkeypatch):
    enable_billing(monkeypatch)
    invoice_id = make_invoice("a", currency="USD")
    response = create_attempt(login("a@example.com"), invoice_id=invoice_id, currency="EUR")
    assert response.status_code == 422
    assert response.json()["detail"] == "currency_mismatch"


def test_zero_and_negative_amounts_are_rejected(monkeypatch):
    enable_billing(monkeypatch)
    invoice_id = make_invoice("a")
    for amount in (0, -100):
        response = client.post(
            "/v1/billing/payment-attempts",
            json={"invoice_id": invoice_id, "amount_minor": amount, "currency": "USD", "provider": "disabled"},
            headers={**login("a@example.com"), "Idempotency-Key": "key-" + uuid.uuid4().hex},
        )
        assert response.status_code == 422


def test_amount_exceeding_remaining_balance_is_rejected(monkeypatch):
    enable_billing(monkeypatch)
    invoice_id = make_invoice("a", total="10.00")
    response = create_attempt(login("a@example.com"), invoice_id=invoice_id, amount_minor=2000)
    assert response.status_code == 409
    assert response.json()["detail"] == "amount_exceeds_remaining_balance"


def test_two_sibling_attempts_may_both_be_created_below_invoice_total(monkeypatch):
    """Creation-time eligibility only counts already-CAPTURED attempts (per the
    remaining-balance definition); two merely PENDING/CREATED sibling attempts
    may coexist even if their sum would exceed the invoice total, since only an
    actual capture can overpay. Capture-time re-validation (see below) is what
    prevents the collective overpayment."""
    enable_billing(monkeypatch)
    invoice_id = make_invoice("a", total="10.00")
    first = create_attempt(login("a@example.com"), invoice_id=invoice_id, amount_minor=700)
    assert first.status_code == 201
    second = create_attempt(login("a@example.com"), invoice_id=invoice_id, amount_minor=700)
    assert second.status_code == 201


def test_capture_is_revalidated_against_invoice_balance_at_transition_time(monkeypatch):
    """Capture-time integrity: two attempts that were each individually eligible
    at creation must not both be allowed to reach CAPTURED if doing so would
    collectively overpay the invoice."""
    enable_billing(monkeypatch)
    invoice_id = make_invoice("a", total="10.00")
    first = create_attempt(login("a@example.com"), invoice_id=invoice_id, amount_minor=600).json()
    second = create_attempt(login("a@example.com"), invoice_id=invoice_id, amount_minor=500).json()

    def advance_to_captured(attempt_id):
        pending = client.post(
            f"/v1/internal/billing/payment-attempts/{attempt_id}/transition",
            json={"target_status": "PENDING"},
            headers=login("root@example.com"),
        )
        assert pending.status_code == 200
        return client.post(
            f"/v1/internal/billing/payment-attempts/{attempt_id}/transition",
            json={"target_status": "CAPTURED"},
            headers=login("root@example.com"),
        )

    first_captured = advance_to_captured(first["id"])
    assert first_captured.status_code == 200
    assert first_captured.json()["status"] == CAPTURED

    second_captured = advance_to_captured(second["id"])
    assert second_captured.status_code == 409
    assert second_captured.json()["detail"] == "amount_exceeds_remaining_balance"


def test_void_invoice_rejects_new_attempts(monkeypatch):
    enable_billing(monkeypatch)
    invoice_id = make_invoice("a", status="VOID")
    response = create_attempt(login("a@example.com"), invoice_id=invoice_id)
    assert response.status_code == 409
    assert response.json()["detail"] == "invoice_not_payable"


def test_credited_invoice_rejects_new_attempts(monkeypatch):
    enable_billing(monkeypatch)
    invoice_id = make_invoice("a", status="CREDITED")
    response = create_attempt(login("a@example.com"), invoice_id=invoice_id)
    assert response.status_code == 409
    assert response.json()["detail"] == "invoice_not_payable"


def test_fully_credited_invoice_rejects_new_attempts(monkeypatch):
    enable_billing(monkeypatch)
    invoice_id = make_invoice("a", total="10.00", credits="10.00", status="OPEN")
    response = create_attempt(login("a@example.com"), invoice_id=invoice_id, amount_minor=1)
    assert response.status_code == 409


def test_unsupported_provider_is_rejected(monkeypatch):
    enable_billing(monkeypatch)
    invoice_id = make_invoice("a")
    response = client.post(
        "/v1/billing/payment-attempts",
        json={"invoice_id": invoice_id, "amount_minor": 1000, "currency": "USD", "provider": "carrier-pigeon"},
        headers={**login("a@example.com"), "Idempotency-Key": "key-" + uuid.uuid4().hex},
    )
    assert response.status_code == 422


def test_provider_disabled_by_config_is_rejected(monkeypatch):
    enable_billing(monkeypatch)
    monkeypatch.setenv("KLYROW_STRIPE_ENABLED", "false")
    invoice_id = make_invoice("a")
    response = create_attempt(login("a@example.com"), invoice_id=invoice_id, provider="stripe")
    assert response.status_code == 503
    assert response.json()["detail"] == "provider_disabled"


def test_list_and_detail_are_tenant_scoped(monkeypatch):
    enable_billing(monkeypatch)
    invoice_id = make_invoice("a")
    created = create_attempt(login("a@example.com"), invoice_id=invoice_id).json()

    listed = client.get("/v1/billing/payment-attempts", headers=login("a@example.com"))
    assert listed.status_code == 200
    assert any(row["id"] == created["id"] for row in listed.json())

    detail = client.get(f"/v1/billing/payment-attempts/{created['id']}", headers=login("a@example.com"))
    assert detail.status_code == 200

    cross_tenant = client.get(f"/v1/billing/payment-attempts/{created['id']}", headers=login("b@example.com"))
    assert cross_tenant.status_code == 404

    cross_tenant_list = client.get("/v1/billing/payment-attempts", headers=login("b@example.com"))
    assert all(row["id"] != created["id"] for row in cross_tenant_list.json())


def test_event_history_is_tenant_scoped_and_available_after_cancel(monkeypatch):
    enable_billing(monkeypatch)
    invoice_id = make_invoice("a")
    created = create_attempt(login("a@example.com"), invoice_id=invoice_id).json()

    cancelled = client.post(f"/v1/billing/payment-attempts/{created['id']}/cancel", headers=login("a@example.com"))
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == CANCELLED

    events = client.get(f"/v1/billing/payment-attempts/{created['id']}/events", headers=login("a@example.com"))
    assert events.status_code == 200
    kinds = [row["to_status"] for row in events.json()]
    assert kinds == [CREATED, CANCELLED]

    cross_tenant_events = client.get(f"/v1/billing/payment-attempts/{created['id']}/events", headers=login("b@example.com"))
    assert cross_tenant_events.status_code == 404


def test_cancel_of_terminal_attempt_is_rejected(monkeypatch):
    enable_billing(monkeypatch)
    invoice_id = make_invoice("a")
    created = create_attempt(login("a@example.com"), invoice_id=invoice_id).json()
    first = client.post(f"/v1/billing/payment-attempts/{created['id']}/cancel", headers=login("a@example.com"))
    assert first.status_code == 200
    second = client.post(f"/v1/billing/payment-attempts/{created['id']}/cancel", headers=login("a@example.com"))
    assert second.status_code == 200  # identical replay of CANCELLED is idempotent, not a conflict


def test_cross_tenant_cancel_is_rejected(monkeypatch):
    enable_billing(monkeypatch)
    invoice_id = make_invoice("a")
    created = create_attempt(login("a@example.com"), invoice_id=invoice_id).json()
    response = client.post(f"/v1/billing/payment-attempts/{created['id']}/cancel", headers=login("b@example.com"))
    assert response.status_code == 404


def test_ordinary_user_cannot_call_internal_transition(monkeypatch):
    enable_billing(monkeypatch)
    invoice_id = make_invoice("a")
    created = create_attempt(login("a@example.com"), invoice_id=invoice_id).json()
    response = client.post(
        f"/v1/internal/billing/payment-attempts/{created['id']}/transition",
        json={"target_status": "AUTHORIZED"},
        headers=login("a@example.com"),
    )
    assert response.status_code == 403


def test_platform_admin_can_call_internal_transition(monkeypatch):
    enable_billing(monkeypatch)
    invoice_id = make_invoice("a")
    created = create_attempt(login("a@example.com"), invoice_id=invoice_id).json()
    response = client.post(
        f"/v1/internal/billing/payment-attempts/{created['id']}/transition",
        json={"target_status": "PENDING"},
        headers=login("root@example.com"),
    )
    assert response.status_code == 200
    assert response.json()["status"] == PENDING


def test_live_charging_disabled_blocks_authorize_for_real_provider(monkeypatch):
    enable_billing(monkeypatch)
    monkeypatch.setenv("KLYROW_STRIPE_ENABLED", "true")
    monkeypatch.setenv("KLYROW_STRIPE_ENVIRONMENT", "sandbox")
    import tempfile
    from pathlib import Path
    secret_path = Path(tempfile.mkdtemp()) / "stripe-secret"
    secret_path.write_text("SYNTHETIC-FIXTURE-VALUE")
    monkeypatch.setenv("KLYROW_STRIPE_SECRET_FILE", str(secret_path))
    invoice_id = make_invoice("a")
    created = create_attempt(login("a@example.com"), invoice_id=invoice_id, provider="stripe").json()
    monkeypatch.setenv("KLYROW_LIVE_CHARGING_ENABLED", "false")
    response = client.post(
        f"/v1/internal/billing/payment-attempts/{created['id']}/transition",
        json={"target_status": "PENDING"},
        headers=login("root@example.com"),
    )
    assert response.status_code == 200
    response = client.post(
        f"/v1/internal/billing/payment-attempts/{created['id']}/transition",
        json={"target_status": "AUTHORIZED"},
        headers=login("root@example.com"),
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "live_charging_disabled"


def test_duplicate_provider_event_transition_is_idempotent(monkeypatch):
    enable_billing(monkeypatch)
    invoice_id = make_invoice("a")
    created = create_attempt(login("a@example.com"), invoice_id=invoice_id).json()
    provider_event_reference = "evt-" + uuid.uuid4().hex
    first = client.post(
        f"/v1/internal/billing/payment-attempts/{created['id']}/transition",
        json={"target_status": "PENDING", "provider_event_reference": provider_event_reference},
        headers=login("root@example.com"),
    )
    assert first.status_code == 200
    second = client.post(
        f"/v1/internal/billing/payment-attempts/{created['id']}/transition",
        json={"target_status": "FAILED", "provider_event_reference": provider_event_reference},
        headers=login("root@example.com"),
    )
    # Same provider event replayed: idempotent no-op, does not apply the new target.
    assert second.status_code == 200
    assert second.json()["status"] == "PENDING"


def test_invalid_transition_returns_conflict(monkeypatch):
    enable_billing(monkeypatch)
    invoice_id = make_invoice("a")
    created = create_attempt(login("a@example.com"), invoice_id=invoice_id).json()
    response = client.post(
        f"/v1/internal/billing/payment-attempts/{created['id']}/transition",
        json={"target_status": "CAPTURED"},
        headers=login("root@example.com"),
    )
    assert response.status_code == 409


def test_safe_serialization_never_exposes_internal_fingerprint_or_key(monkeypatch):
    enable_billing(monkeypatch)
    invoice_id = make_invoice("a")
    response = create_attempt(login("a@example.com"), invoice_id=invoice_id)
    body = response.json()
    assert "idempotency_key" not in body
    assert "request_fingerprint" not in body
    assert "created_by" not in body
