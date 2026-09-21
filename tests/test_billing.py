import pytest

pytestmark = pytest.mark.usefixtures("canonical_api_owner")

from datetime import datetime, timedelta, timezone

from apps.gateway.app.main import Base, DB, Tenant, User, app, engine, ph, rate_buckets
from apps.gateway.app.billing import BillingPrice, Invoice, InvoiceLine, Payment
from fastapi.testclient import TestClient
import jwt
from apps.gateway.app.main import SECRET


client=TestClient(app)
tokens={}


def setup_module():
    rate_buckets.clear();tokens.clear();Base.metadata.drop_all(engine);Base.metadata.create_all(engine)
    with DB() as session:
        for tenant_id,role in (("a","tenant_admin"),("b","tenant_admin"),("root","platform_admin")):
            session.add(Tenant(id=tenant_id,name=tenant_id,quota=10000))
            session.add(User(id=tenant_id,tenant_id=tenant_id,email=f"{tenant_id}@example.com",password_hash=ph.hash("long-enough-password"),role=role))
        session.commit()


def login(email="root@example.com"):
    if email in tokens:return tokens[email]
    response=client.post("/v1/auth/login",json={"email":email,"password":"long-enough-password"})
    assert response.status_code==200,response.text
    tokens[email]={"Authorization":"Bearer "+response.json()["access_token"]}
    return tokens[email]


def test_versioned_catalog_subscription_usage_invoice_and_sandbox_payment():
    root=login()
    created=client.post("/v1/admin/billing/catalog",headers=root,json={"code":"STARTER","name":"Starter","currency":"USD","cycle":"MONTHLY","base_amount":"10.00","included_units":100,"overage_amount":"0.02","features":{"domains":2}})
    assert created.status_code==201,created.text
    second=client.post("/v1/admin/billing/catalog",headers=root,json={"code":"STARTER","name":"Starter","currency":"USD","cycle":"MONTHLY","base_amount":"12.00","included_units":200,"overage_amount":"0.015","features":{"domains":3}})
    assert second.status_code==201 and second.json()["version"]==2
    tenant=login("a@example.com")
    subscription=client.post("/v1/billing/subscription",headers=tenant,json={"plan_code":"STARTER","trial_days":0})
    assert subscription.status_code==201 and subscription.json()["price_version"]==2
    usage={"event_key":"accepted-message-0001","message_id":"message-1","quantity":250}
    first=client.post("/v1/billing/usage-events",headers=tenant,json=usage)
    duplicate=client.post("/v1/billing/usage-events",headers=tenant,json=usage)
    conflict=client.post("/v1/billing/usage-events",headers=tenant,json={**usage,"quantity":251})
    assert first.status_code==202 and duplicate.json()["duplicate"] is True and conflict.status_code==409
    invoice=client.post("/v1/billing/invoices",headers=tenant,json={"due_at":(datetime.now(timezone.utc)+timedelta(days=14)).isoformat()})
    assert invoice.status_code==201,invoice.text
    assert invoice.json()["total"]=="12.75"
    with DB() as session:
        assert session.get(Invoice,invoice.json()["id"]).status=="OPEN"
    payment=client.post("/v1/billing/payments",headers=tenant,json={"invoice_id":invoice.json()["id"],"provider":"SANDBOX","provider_reference":"sandbox-payment-0001","amount":"12.75"})
    assert payment.status_code==409 and payment.json()["detail"]=="sandbox_payment_requires_hosted_checkout"


def test_wallet_is_immutable_idempotent_and_cannot_overspend():
    tenant=login("a@example.com")
    credit={"kind":"CREDIT","amount":"20.00","currency":"USD","reference":"wallet-credit-0001"}
    first=client.post("/v1/billing/wallet/transactions",headers=tenant,json=credit)
    duplicate=client.post("/v1/billing/wallet/transactions",headers=tenant,json=credit)
    debit=client.post("/v1/billing/wallet/transactions",headers=tenant,json={"kind":"DEBIT","amount":"7.50","currency":"USD","reference":"wallet-debit-0001"})
    denied=client.post("/v1/billing/wallet/transactions",headers=tenant,json={"kind":"DEBIT","amount":"20.00","currency":"USD","reference":"wallet-debit-0002"})
    assert first.status_code==201 and duplicate.json()["duplicate"] is True
    assert debit.json()["balance"]=="12.50" and denied.status_code==409


def test_read_only_member_cannot_mutate_wallet():
    raw=jwt.encode({"sub":"readonly-user","tenant":"a","role":"READ_ONLY","exp":datetime.now(timezone.utc)+timedelta(hours=1)},SECRET,algorithm="HS256")
    response=client.post("/v1/billing/wallet/transactions",headers={"Authorization":"Bearer "+raw},json={"kind":"CREDIT","amount":"999.00","currency":"USD","reference":"unauthorized-credit-0001"})
    assert response.status_code==403 and response.json()["detail"]=="billing_management_denied"


def test_billing_worker_retry_cannot_duplicate_invoice_or_lines():
    tenant=login("a@example.com")
    payload={"due_at":(datetime.now(timezone.utc)+timedelta(days=14)).isoformat()}
    headers={**tenant,"Idempotency-Key":"invoice-worker-period-0001"}
    first=client.post("/v1/billing/invoices",headers=headers,json=payload)
    replay=client.post("/v1/billing/invoices",headers=headers,json=payload)
    assert first.status_code==201 and first.json()["duplicate"] is False
    assert replay.status_code==201 and replay.json()["duplicate"] is True and replay.json()["id"]==first.json()["id"]
    with DB() as session:
        assert session.query(Invoice).filter_by(request_key="invoice-worker-period-0001").count()==1
        line_count=session.query(InvoiceLine).filter_by(invoice_id=first.json()["id"]).count()
        assert 1<=line_count<=2


def test_manual_payment_requires_reconciliation_confirmation_and_no_raw_cards():
    tenant=login("a@example.com");root=login()
    created=client.post("/v1/billing/invoices",headers=tenant,json={"due_at":(datetime.now(timezone.utc)+timedelta(days=14)).isoformat()})
    assert created.status_code==201,created.text
    manual=client.post("/v1/billing/payments",headers=tenant,json={"invoice_id":created.json()["id"],"provider":"MANUAL_OFFLINE","provider_reference":"wire-transfer-0001","amount":created.json()["total"]})
    assert manual.status_code==201 and manual.json()["status"]=="PENDING_RECONCILIATION"
    confirmed=client.post(f"/v1/billing/payments/{manual.json()['id']}/confirm",headers=root)
    assert confirmed.status_code==200 and confirmed.json()["invoice_status"]=="PAID"
    forbidden=client.post("/v1/billing/payment-methods",headers=tenant,json={"provider":"EXTERNAL_TOKENIZED","provider_reference":"card_number=4111111111111111","label":"bad"})
    accepted=client.post("/v1/billing/payment-methods",headers=tenant,json={"provider":"EXTERNAL_TOKENIZED","provider_reference":"pm_provider_opaque_123","label":"Business card","is_default":True})
    assert forbidden.status_code==422 and accepted.status_code==201


def test_cross_tenant_billing_access_is_denied():
    other=login("b@example.com")
    with DB() as session:
        invoice=session.query(Invoice).filter(Invoice.tenant_id=="a").first()
    response=client.post("/v1/billing/payments",headers=other,json={"invoice_id":invoice.id,"provider":"SANDBOX","provider_reference":"cross-tenant-payment","amount":"1.00"})
    assert response.status_code==404


def test_refund_rejects_payment_with_cross_tenant_invoice(monkeypatch):
    from types import SimpleNamespace
    from apps.gateway.app.billing import Refund
    monkeypatch.setattr("apps.gateway.app.billing.load_billing_settings", lambda: SimpleNamespace(refunds_enabled=True))
    with DB() as session:
        foreign_invoice=Invoice(
            id="foreign-refund-invoice", number="KLY-FOREIGN-REFUND", tenant_id="b",
            subscription_id="missing", currency="USD", subtotal="10.00", tax="0.00",
            discount="0.00", credits="0.00", total="10.00", status="OPEN",
            due_at=datetime.now(timezone.utc), evidence_json="{}",
        )
        payment=Payment(
            id="cross-tenant-refund-payment", tenant_id="a", invoice_id=foreign_invoice.id,
            provider="MANUAL_OFFLINE", provider_reference="cross-tenant-refund-payment",
            amount="10.00", currency="USD", status="CONFIRMED",
        )
        session.add_all([foreign_invoice, payment])
        session.commit()
    response=client.post(
        "/v1/billing/payments/cross-tenant-refund-payment/refunds",
        headers=login("a@example.com"),
        json={"amount":"1.00", "provider_reference":"refund-cross-tenant-0001"},
    )
    assert response.status_code==404 and response.json()["detail"]=="invoice_not_found"
    with DB() as session:
        assert session.query(Refund).filter_by(payment_id="cross-tenant-refund-payment").count()==0


def test_checkout_proration_credit_note_and_dunning_are_auditable():
    root=login();tenant_a=login("a@example.com");tenant_b=login("b@example.com")
    growth=client.post("/v1/admin/billing/catalog",headers=root,json={"code":"GROWTH","name":"Growth","currency":"USD","cycle":"MONTHLY","base_amount":"30.00","included_units":1000,"overage_amount":"0.01","features":{"domains":10}})
    assert growth.status_code==201
    changed=client.post("/v1/billing/subscription-plan-change",headers=tenant_a,json={"plan_code":"GROWTH"})
    assert changed.status_code==200 and changed.json()["effective"]=="IMMEDIATE" and float(changed.json()["charge"])>=0
    checkout=client.post("/v1/billing/checkout",headers=tenant_b,json={"plan_code":"STARTER","provider":"MANUAL_OFFLINE","provider_reference":"manual-checkout-0001"})
    assert checkout.status_code==201 and checkout.json()["state"]=="PAYMENT_PENDING" and checkout.json()["raw_card_storage"] is False
    assert client.post("/v1/billing/checkout",headers=tenant_b,json={"plan_code":"STARTER","provider":"MANUAL_OFFLINE","provider_reference":"manual-checkout-0001"}).status_code==409
    invoice=client.post("/v1/billing/invoices",headers=tenant_a,json={"due_at":(datetime.now(timezone.utc)-timedelta(days=10)).isoformat()})
    note=client.post(f"/v1/billing/invoices/{invoice.json()['id']}/credit-notes",headers=tenant_a,json={"amount":"1.00","reason":"Service availability credit"})
    assert note.status_code==201 and note.json()["amount"]=="1.00"
    with DB() as session:
        item=session.get(Invoice,invoice.json()["id"]);item.status="OPEN";session.commit()
    dunning=client.post("/v1/admin/billing/dunning",headers=root,json={"grace_days":7,"suspend_days":21})
    assert dunning.status_code==200 and any(item["invoice_id"]==invoice.json()["id"] and item["subscription_status"]=="GRACE_PERIOD" for item in dunning.json()["items"])
    assert dunning.json()["login_disabled"] is False


def test_reconciliation_detects_paid_invoice_without_confirmed_payment():
    tenant=login("a@example.com")
    created=client.post("/v1/billing/invoices",headers=tenant,json={"due_at":(datetime.now(timezone.utc)+timedelta(days=14)).isoformat()})
    with DB() as session:
        invoice=session.get(Invoice,created.json()["id"]);invoice.status="PAID";session.commit()
    reconciliation=client.get("/v1/billing/reconciliation",headers=tenant)
    assert reconciliation.status_code==200 and reconciliation.json()["status"]=="DRIFT"
    assert any(item["invoice_id"]==created.json()["id"] and item["expected"]=="OPEN" for item in reconciliation.json()["issues"])
