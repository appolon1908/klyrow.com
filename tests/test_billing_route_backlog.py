from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from apps.gateway.app import billing, service_worker


def seed_subscription(sessions):
    with sessions() as session:
        session.add(billing.BillingProduct(id="product", code="EMAIL", name="Email"))
        session.flush()
        for suffix, amount in (("old", "10"), ("new", "20")):
            session.add(
                billing.BillingPlan(
                    id=suffix, product_id="product", code=suffix.upper(), name=suffix
                )
            )
            session.flush()
            session.add(
                billing.BillingPrice(
                    id=suffix,
                    plan_id=suffix,
                    version=1,
                    currency="USD",
                    billing_cycle="MONTHLY",
                    base_amount=amount,
                    included_units=100,
                    overage_amount="0.01",
                )
            )
        session.flush()
        session.add(
            billing.BillingSubscription(
                id="sub",
                tenant_id="tenant-a",
                plan_id="old",
                price_id="old",
                status="ACTIVE",
                period_start=datetime.now(UTC),
                period_end=datetime.now(UTC) + timedelta(days=30),
            )
        )
        session.commit()


def test_literal_subscription_change_and_cancel_routes_are_reachable(gateway):
    client, sessions, context = gateway
    context.update(role="BILLING", permissions=["billing.manage"])
    seed_subscription(sessions)
    response = client.post("/v1/billing/subscription/change", json={"plan_code": "NEW"})
    assert response.status_code == 200, response.text
    assert response.json()["effective"] == "IMMEDIATE"
    response = client.post("/v1/billing/subscription/cancel")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "CANCEL_AT_PERIOD_END"


def test_billing_events_after_first_200_are_not_starved(gateway, monkeypatch):
    _, sessions, _ = gateway
    monkeypatch.setattr(service_worker, "DB", sessions)
    timestamp = datetime.now(UTC) - timedelta(days=1)
    with sessions() as session:
        for index in range(205):
            identity = f"event-{index:04}"
            session.add(
                billing.BillingEvent(
                    id=identity,
                    tenant_id="tenant-a",
                    kind="subscription.created",
                    reference="sub",
                    created_at=timestamp + timedelta(seconds=index),
                )
            )
            if index < 200:
                session.add(
                    billing.BillingWorkItem(
                        id=identity,
                        billing_event_id=identity,
                        tenant_id="tenant-a",
                        kind="subscription.created",
                        state="COMPLETED",
                    )
                )
        session.commit()
    assert [service_worker.billing_tick() for _ in range(6)] == [1, 1, 1, 1, 1, 0]
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(billing.BillingWorkItem)) == 205
        assert (
            session.scalar(
                select(func.count())
                .select_from(billing.BillingWorkItem)
                .where(billing.BillingWorkItem.state != "COMPLETED")
            )
            == 0
        )
