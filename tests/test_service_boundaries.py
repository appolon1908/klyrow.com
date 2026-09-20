from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.gateway.app.main import Base, DB, Tenant, User, app, engine, ph, rate_buckets
from apps.gateway.app.billing import BillingEvent, BillingPlan, BillingPrice, BillingProduct, BillingSubscription, BillingWorkItem
from apps.gateway.app.service_worker import billing_tick

client=TestClient(app)

def setup_module():
    rate_buckets.clear();Base.metadata.drop_all(engine);Base.metadata.create_all(engine)
    with DB() as s:
        s.add(Tenant(id="boundary-a",name="Boundary A",quota=1000))
        s.add(User(id="boundary-user",tenant_id="boundary-a",email="boundary@example.com",password_hash=ph.hash("long-enough-password"),role="tenant_admin"))
        product=BillingProduct(id="product",code="EMAIL",name="Email")
        plan=BillingPlan(id="plan",product_id="product",code="BOUNDARY",name="Boundary",features_json='{"mail":true}')
        price=BillingPrice(id="price",plan_id="plan",version=1,currency="USD",billing_cycle="MONTHLY",base_amount="10",included_units=100,overage_amount="0.01")
        sub=BillingSubscription(id="subscription",tenant_id="boundary-a",plan_id="plan",price_id="price",status="ACTIVE",period_end=datetime.now(timezone.utc)+timedelta(days=30))
        event=BillingEvent(id="billing-event",tenant_id="boundary-a",kind="subscription.created",reference="subscription")
        s.add_all([product,plan,price,sub,event]);s.commit()

def auth():
    response=client.post("/v1/auth/login",json={"email":"boundary@example.com","password":"long-enough-password"})
    return {"Authorization":"Bearer "+response.json()["access_token"]}

def test_dedicated_billing_read_contract_is_tenant_authenticated():
    assert client.get("/v1/billing/plan").status_code==401
    headers=auth()
    assert client.get("/v1/billing/plan",headers=headers).json()["code"]=="BOUNDARY"
    assert client.get("/v1/billing/subscription",headers=headers).status_code==200
    assert client.get("/v1/billing/usage",headers=headers).status_code==200
    assert client.get("/v1/billing/quota",headers=headers).json()["remaining"]==100
    assert client.get("/v1/billing/invoices",headers=headers).status_code==200
    assert client.get("/v1/billing/credits",headers=headers).status_code==200

def test_billing_worker_ledger_is_idempotent_and_reclaims_expired_lease():
    assert billing_tick()==1
    assert billing_tick()==0
    with DB() as s:
        item=s.scalar(select(BillingWorkItem).where(BillingWorkItem.billing_event_id=="billing-event"))
        assert item.state=="COMPLETED" and item.attempts==1
        item.state="PROCESSING";item.attempts=1;item.lease_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1);s.commit()
    assert billing_tick()==0
    with DB() as s:
        item=s.scalar(select(BillingWorkItem).where(BillingWorkItem.billing_event_id=="billing-event"))
        assert item.state=="RETRY" and item.attempts==1
        item.available_at=datetime.now(timezone.utc)-timedelta(seconds=1);s.commit()
    assert billing_tick()==1
    with DB() as s:
        item=s.scalar(select(BillingWorkItem).where(BillingWorkItem.billing_event_id=="billing-event"))
        assert item.state=="COMPLETED" and item.attempts==2


def test_billing_worker_dead_letter_retains_unknown_kind_error():
    with DB() as s:
        s.add(BillingWorkItem(id="unknown-kind", billing_event_id="unknown-event", tenant_id="boundary-a", kind="unknown.kind", state="PENDING", available_at=datetime.now(timezone.utc)-timedelta(seconds=1)))
        s.commit()
    assert billing_tick()==1
    with DB() as s:
        item=s.get(BillingWorkItem, "unknown-kind")
        assert item.state=="DEAD_LETTER" and item.last_error=="unknown_billing_work_kind"
