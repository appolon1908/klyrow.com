"""Klyrow-owned, provider-agnostic commercial billing core.

This module deliberately has no dependency on Telnexa and never accepts or
stores raw payment-card data. Payment methods are opaque provider references.
"""
import json, secrets, uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .main import Base, Tenant, audit, auth, db, require, sha

router=APIRouter(prefix="/v1",tags=["Klyrow billing"])
now=lambda:datetime.now(timezone.utc)
money=lambda value:Decimal(value).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)
def expected_invoice_status(current:str,total,paid,refunded)->str:
    if current in {"VOID","CREDITED"}:return current
    net=money(paid)-money(refunded)
    if net>=money(total):return "PAID"
    if net>0:return "PARTIALLY_PAID"
    return "OPEN" if current in {"PAID","PARTIALLY_PAID"} else current

class BillingProduct(Base):
    __tablename__="klyrow_products"; id:Mapped[str]=mapped_column(String,primary_key=True); code:Mapped[str]=mapped_column(String,unique=True); name:Mapped[str]=mapped_column(String); active:Mapped[bool]=mapped_column(Boolean,default=True)
class BillingPlan(Base):
    __tablename__="klyrow_plans"; id:Mapped[str]=mapped_column(String,primary_key=True); product_id:Mapped[str]=mapped_column(ForeignKey("klyrow_products.id")); code:Mapped[str]=mapped_column(String,unique=True); name:Mapped[str]=mapped_column(String); features_json:Mapped[str]=mapped_column(Text,default="{}"); active:Mapped[bool]=mapped_column(Boolean,default=True)
class BillingPrice(Base):
    __tablename__="klyrow_prices"; id:Mapped[str]=mapped_column(String,primary_key=True); plan_id:Mapped[str]=mapped_column(ForeignKey("klyrow_plans.id"),index=True); version:Mapped[int]=mapped_column(Integer); currency:Mapped[str]=mapped_column(String); billing_cycle:Mapped[str]=mapped_column(String); base_amount:Mapped[Decimal]=mapped_column(Numeric(18,6)); included_units:Mapped[int]=mapped_column(Integer); overage_amount:Mapped[Decimal]=mapped_column(Numeric(18,8)); effective_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); retired_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True); __table_args__=(UniqueConstraint("plan_id","version",name="uq_klyrow_price_version"),)
class BillingSubscription(Base):
    __tablename__="klyrow_subscriptions"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(ForeignKey("tenants.id"),unique=True,index=True); plan_id:Mapped[str]=mapped_column(String); price_id:Mapped[str]=mapped_column(String); status:Mapped[str]=mapped_column(String,default="TRIALING"); period_start:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); period_end:Mapped[datetime]=mapped_column(DateTime(timezone=True)); trial_end:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True); cancel_at_period_end:Mapped[bool]=mapped_column(Boolean,default=False); version:Mapped[int]=mapped_column(Integer,default=1)
class UsageEvent(Base):
    __tablename__="klyrow_usage_events"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); subscription_id:Mapped[str]=mapped_column(String,index=True); message_id:Mapped[Optional[str]]=mapped_column(String,nullable=True); event_key:Mapped[str]=mapped_column(String); unit:Mapped[str]=mapped_column(String); quantity:Mapped[int]=mapped_column(Integer); price_id:Mapped[str]=mapped_column(String); occurred_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); __table_args__=(UniqueConstraint("tenant_id","event_key",name="uq_klyrow_usage_event"),)
class Invoice(Base):
    __tablename__="klyrow_invoices"; id:Mapped[str]=mapped_column(String,primary_key=True); number:Mapped[str]=mapped_column(String,unique=True); tenant_id:Mapped[str]=mapped_column(String,index=True); subscription_id:Mapped[str]=mapped_column(String); request_key:Mapped[Optional[str]]=mapped_column(String,nullable=True); currency:Mapped[str]=mapped_column(String); subtotal:Mapped[Decimal]=mapped_column(Numeric(18,2)); tax:Mapped[Decimal]=mapped_column(Numeric(18,2),default=0); discount:Mapped[Decimal]=mapped_column(Numeric(18,2),default=0); credits:Mapped[Decimal]=mapped_column(Numeric(18,2),default=0); total:Mapped[Decimal]=mapped_column(Numeric(18,2)); status:Mapped[str]=mapped_column(String,default="DRAFT"); due_at:Mapped[datetime]=mapped_column(DateTime(timezone=True)); evidence_json:Mapped[str]=mapped_column(Text,default="{}"); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); __table_args__=(UniqueConstraint("tenant_id","request_key",name="uq_klyrow_invoice_request_key"),)
class InvoiceLine(Base):
    __tablename__="klyrow_invoice_lines"; id:Mapped[str]=mapped_column(String,primary_key=True); invoice_id:Mapped[str]=mapped_column(String,index=True); kind:Mapped[str]=mapped_column(String); description:Mapped[str]=mapped_column(String); quantity:Mapped[int]=mapped_column(Integer); unit_amount:Mapped[Decimal]=mapped_column(Numeric(18,8)); amount:Mapped[Decimal]=mapped_column(Numeric(18,2)); reference:Mapped[Optional[str]]=mapped_column(String,nullable=True); __table_args__=(UniqueConstraint("invoice_id","kind","reference",name="uq_klyrow_invoice_line_reference"),)
class PaymentMethodReference(Base):
    __tablename__="klyrow_payment_method_references"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); provider:Mapped[str]=mapped_column(String); provider_reference:Mapped[str]=mapped_column(String); label:Mapped[str]=mapped_column(String); is_default:Mapped[bool]=mapped_column(Boolean,default=False); revoked_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True)
class Payment(Base):
    __tablename__="klyrow_payments"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); invoice_id:Mapped[str]=mapped_column(String,index=True); provider:Mapped[str]=mapped_column(String); provider_reference:Mapped[str]=mapped_column(String); amount:Mapped[Decimal]=mapped_column(Numeric(18,2)); currency:Mapped[str]=mapped_column(String); status:Mapped[str]=mapped_column(String); confirmed_by:Mapped[Optional[str]]=mapped_column(String,nullable=True); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); __table_args__=(UniqueConstraint("provider","provider_reference",name="uq_klyrow_payment_provider_ref"),)
class Credit(Base):
    __tablename__="klyrow_credits"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); invoice_id:Mapped[Optional[str]]=mapped_column(String,nullable=True); amount:Mapped[Decimal]=mapped_column(Numeric(18,2)); currency:Mapped[str]=mapped_column(String); reason:Mapped[str]=mapped_column(String); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
class Refund(Base):
    __tablename__="klyrow_refunds"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); payment_id:Mapped[str]=mapped_column(String,index=True); amount:Mapped[Decimal]=mapped_column(Numeric(18,2)); status:Mapped[str]=mapped_column(String); provider_reference:Mapped[str]=mapped_column(String,unique=True); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
class Wallet(Base):
    __tablename__="klyrow_wallets"; tenant_id:Mapped[str]=mapped_column(String,primary_key=True); currency:Mapped[str]=mapped_column(String); balance:Mapped[Decimal]=mapped_column(Numeric(18,2),default=0); version:Mapped[int]=mapped_column(Integer,default=0)
class WalletTransaction(Base):
    __tablename__="klyrow_wallet_transactions"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); kind:Mapped[str]=mapped_column(String); amount:Mapped[Decimal]=mapped_column(Numeric(18,2)); currency:Mapped[str]=mapped_column(String); reference:Mapped[str]=mapped_column(String); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); __table_args__=(UniqueConstraint("tenant_id","reference",name="uq_klyrow_wallet_reference"),)
class TaxRule(Base):
    __tablename__="klyrow_tax_rules"; id:Mapped[str]=mapped_column(String,primary_key=True); jurisdiction:Mapped[str]=mapped_column(String); mode:Mapped[str]=mapped_column(String); rate:Mapped[Decimal]=mapped_column(Numeric(8,6),default=0); evidence_label:Mapped[str]=mapped_column(String); active:Mapped[bool]=mapped_column(Boolean,default=True)
class BillingEvent(Base):
    __tablename__="klyrow_billing_events"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); kind:Mapped[str]=mapped_column(String); reference:Mapped[str]=mapped_column(String,index=True); payload_json:Mapped[str]=mapped_column(Text,default="{}"); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
class BillingWorkItem(Base):
    __tablename__="klyrow_billing_work_items"; id:Mapped[str]=mapped_column(String,primary_key=True); billing_event_id:Mapped[str]=mapped_column(String,unique=True,index=True); tenant_id:Mapped[str]=mapped_column(String,index=True); kind:Mapped[str]=mapped_column(String,index=True); state:Mapped[str]=mapped_column(String,default="PENDING",index=True); attempts:Mapped[int]=mapped_column(Integer,default=0); available_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,index=True); lease_expires_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True); last_error:Mapped[Optional[str]]=mapped_column(String,nullable=True); completed_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
class CheckoutSession(Base):
    __tablename__="klyrow_checkout_sessions"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); plan_id:Mapped[str]=mapped_column(String); price_id:Mapped[str]=mapped_column(String); provider:Mapped[str]=mapped_column(String); state:Mapped[str]=mapped_column(String); provider_reference:Mapped[str]=mapped_column(String,unique=True); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
class CreditNote(Base):
    __tablename__="klyrow_credit_notes"; id:Mapped[str]=mapped_column(String,primary_key=True); number:Mapped[str]=mapped_column(String,unique=True); tenant_id:Mapped[str]=mapped_column(String,index=True); invoice_id:Mapped[str]=mapped_column(String,index=True); amount:Mapped[Decimal]=mapped_column(Numeric(18,2)); currency:Mapped[str]=mapped_column(String); reason:Mapped[str]=mapped_column(String); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)

class CatalogIn(BaseModel):
    code:str=Field(pattern=r"^[A-Z][A-Z0-9_]{1,39}$"); name:str; currency:str=Field(pattern=r"^[A-Z]{3}$"); cycle:str=Field(pattern="^(FREE|TRIAL|MONTHLY|ANNUAL|USAGE_BASED|CUSTOM)$"); base_amount:Decimal=Field(ge=0); included_units:int=Field(ge=0); overage_amount:Decimal=Field(ge=0); features:dict=Field(default_factory=dict)
class SubscribeIn(BaseModel): plan_code:str; trial_days:int=Field(default=0,ge=0,le=365)
class UsageIn(BaseModel): event_key:str=Field(min_length=8,max_length=200); message_id:Optional[str]=None; unit:str="accepted_message"; quantity:int=Field(default=1,gt=0,le=1000000)
class WalletIn(BaseModel): kind:str=Field(pattern="^(CREDIT|DEBIT|REFUND|ADJUSTMENT|PROMOTIONAL_CREDIT)$"); amount:Decimal=Field(gt=0); currency:str=Field(pattern=r"^[A-Z]{3}$"); reference:str=Field(min_length=8,max_length=200)
class InvoiceIn(BaseModel): due_at:datetime; jurisdiction:Optional[str]=None
class PaymentIn(BaseModel): invoice_id:str; provider:str=Field(pattern="^(MANUAL_OFFLINE|SANDBOX)$"); provider_reference:str=Field(min_length=8,max_length=200); amount:Decimal=Field(gt=0)
class RefundIn(BaseModel): amount:Decimal=Field(gt=0); provider_reference:str=Field(min_length=8,max_length=200)
class PaymentMethodIn(BaseModel): provider:str=Field(pattern="^(MANUAL_OFFLINE|SANDBOX|EXTERNAL_TOKENIZED)$"); provider_reference:str=Field(min_length=6,max_length=300); label:str=Field(min_length=1,max_length=100); is_default:bool=False
class CheckoutIn(BaseModel): plan_code:str; provider:str=Field(pattern="^(MANUAL_OFFLINE|SANDBOX)$"); provider_reference:str=Field(min_length=8,max_length=200)
class PlanChangeIn(BaseModel): plan_code:str
class CreditNoteIn(BaseModel): amount:Decimal=Field(gt=0); reason:str=Field(min_length=3,max_length=500)
class DunningIn(BaseModel): grace_days:int=Field(default=7,ge=1,le=90); suspend_days:int=Field(default=21,ge=2,le=180)

STATES={"TRIALING":{"ACTIVE","CANCELLED"},"ACTIVE":{"PAST_DUE","CANCEL_AT_PERIOD_END","SUSPENDED"},"PAST_DUE":{"ACTIVE","GRACE_PERIOD","SUSPENDED"},"GRACE_PERIOD":{"ACTIVE","SUSPENDED"},"SUSPENDED":{"ACTIVE","CANCELLED"},"CANCEL_AT_PERIOD_END":{"ACTIVE","CANCELLED"},"CANCELLED":{"ACTIVE","CLOSED"},"CLOSED":set()}

def enqueue_subscription_changed(s,sub,*,causation_id=None):
    from .business_events import enqueue_named_event
    enqueue_named_event(s,event_type="klyrow.subscription.changed",tenant_id=sub.tenant_id,
        aggregate_id=sub.id,causation_id=causation_id or sub.id,
        correlation_id="sub_"+sub.id,
        data={"subscription_id":sub.id,"status":sub.status,"plan_id":sub.plan_id,
              "price_id":sub.price_id,"version":int(sub.version or 1),"effective_at":now()})

def tenant_item(s,model,item_id,tenant):
    item=s.scalar(select(model).where(model.id==item_id,model.tenant_id==tenant))
    if not item:raise HTTPException(404,"not_found")
    return item

@router.post("/admin/billing/catalog",status_code=201)
def catalog(x:CatalogIn,ctx=Depends(require("platform_admin")),s:Session=Depends(db)):
    product=s.scalar(select(BillingProduct).where(BillingProduct.code=="EMAIL")) or BillingProduct(id=str(uuid.uuid4()),code="EMAIL",name="Klyrow Email")
    plan=s.scalar(select(BillingPlan).where(BillingPlan.code==x.code)) or BillingPlan(id=str(uuid.uuid4()),product_id=product.id,code=x.code,name=x.name)
    plan.name=x.name;plan.features_json=json.dumps(x.features,sort_keys=True);version=(s.scalar(select(func.max(BillingPrice.version)).where(BillingPrice.plan_id==plan.id)) or 0)+1
    price=BillingPrice(id=str(uuid.uuid4()),plan_id=plan.id,version=version,currency=x.currency,billing_cycle=x.cycle,base_amount=x.base_amount,included_units=x.included_units,overage_amount=x.overage_amount)
    s.add_all([product,plan,price]);audit(s,ctx,"billing.catalog.version_created");s.commit();return {"plan_id":plan.id,"price_id":price.id,"version":version}

@router.post("/billing/subscription",status_code=201)
def subscribe(x:SubscribeIn,ctx=Depends(auth),s:Session=Depends(db)):
    plan=s.scalar(select(BillingPlan).where(BillingPlan.code==x.plan_code,BillingPlan.active==True));
    if not plan:raise HTTPException(404,"plan_not_found")
    price=s.scalar(select(BillingPrice).where(BillingPrice.plan_id==plan.id,BillingPrice.retired_at==None).order_by(BillingPrice.version.desc()))
    if not price:raise HTTPException(409,"plan_has_no_active_price")
    existing=s.scalar(select(BillingSubscription).where(BillingSubscription.tenant_id==ctx["tenant"]))
    if existing and existing.status not in {"CANCELLED","CLOSED"}:raise HTTPException(409,"active_subscription_exists")
    from datetime import timedelta
    start=now();end=start+timedelta(days=x.trial_days or (365 if price.billing_cycle=="ANNUAL" else 30));status="TRIALING" if x.trial_days else "ACTIVE"
    sub=existing or BillingSubscription(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],plan_id=plan.id,price_id=price.id,period_end=end)
    sub.plan_id=plan.id;sub.price_id=price.id;sub.status=status;sub.period_start=start;sub.period_end=end;sub.trial_end=end if x.trial_days else None;sub.cancel_at_period_end=False
    billing_event=BillingEvent(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],kind="subscription.created",reference=sub.id);s.add(sub);s.add(billing_event);enqueue_subscription_changed(s,sub,causation_id=billing_event.id);audit(s,ctx,"billing.subscription.created");s.commit();return {"id":sub.id,"status":sub.status,"price_version":price.version}

# Literal operations must precede the parameterized state-transition route.
@router.post("/billing/subscription/change")
def billing_subscription_change(x:PlanChangeIn,ctx=Depends(auth),s:Session=Depends(db)):return change_plan(x,ctx,s)
@router.post("/billing/subscription/cancel")
def billing_subscription_cancel(ctx=Depends(auth),s:Session=Depends(db)):return transition("CANCEL_AT_PERIOD_END",ctx,s)

@router.post("/billing/subscription/{status}")
def transition(status:str,ctx=Depends(auth),s:Session=Depends(db)):
    target=status.upper();sub=s.scalar(select(BillingSubscription).where(BillingSubscription.tenant_id==ctx["tenant"]).with_for_update())
    if not sub:raise HTTPException(404,"subscription_not_found")
    if target not in STATES.get(sub.status,set()):raise HTTPException(409,"invalid_subscription_transition")
    sub.status=target;sub.cancel_at_period_end=target=="CANCEL_AT_PERIOD_END";sub.version+=1;billing_event=BillingEvent(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],kind="subscription."+target.lower(),reference=sub.id);s.add(billing_event);enqueue_subscription_changed(s,sub,causation_id=billing_event.id);audit(s,ctx,"billing.subscription."+target.lower());s.commit();return {"id":sub.id,"status":sub.status,"version":sub.version}

@router.post("/billing/usage-events",status_code=202)
def meter(x:UsageIn,ctx=Depends(auth),s:Session=Depends(db)):
    sub=s.scalar(select(BillingSubscription).where(BillingSubscription.tenant_id==ctx["tenant"]));
    if not sub or sub.status not in {"TRIALING","ACTIVE","PAST_DUE","GRACE_PERIOD"}:raise HTTPException(402,"subscription_not_billable")
    old=s.scalar(select(UsageEvent).where(UsageEvent.tenant_id==ctx["tenant"],UsageEvent.event_key==x.event_key))
    if old:
        if (old.message_id,old.unit,old.quantity)!=(x.message_id,x.unit,x.quantity):raise HTTPException(409,"usage_idempotency_conflict")
        return {"id":old.id,"duplicate":True}
    event=UsageEvent(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],subscription_id=sub.id,message_id=x.message_id,event_key=x.event_key,unit=x.unit,quantity=x.quantity,price_id=sub.price_id);s.add(event);s.commit();return {"id":event.id,"duplicate":False}

@router.post("/billing/wallet/transactions",status_code=201)
def wallet_tx(x:WalletIn,ctx=Depends(auth),s:Session=Depends(db)):
    if ctx.get("role") not in {"platform_admin","tenant_admin","OWNER","ADMIN","BILLING"}:
        from .tenancy import ROLE_PERMISSIONS,TenantMember
        membership=s.scalar(select(TenantMember).where(TenantMember.tenant_id==ctx["tenant"],TenantMember.user_id==ctx["sub"],TenantMember.active==True))
        permissions=ROLE_PERMISSIONS.get(membership.role,set()) if membership else set()
        if "*" not in permissions and "billing.manage" not in permissions:
            raise HTTPException(403,"billing_management_denied")
    old=s.scalar(select(WalletTransaction).where(WalletTransaction.tenant_id==ctx["tenant"],WalletTransaction.reference==x.reference));
    if old:return {"id":old.id,"duplicate":True}
    wallet=s.scalar(select(Wallet).where(Wallet.tenant_id==ctx["tenant"]).with_for_update()) or Wallet(tenant_id=ctx["tenant"],currency=x.currency,balance=0,version=0)
    if wallet.currency!=x.currency:raise HTTPException(409,"wallet_currency_mismatch")
    signed=-money(x.amount) if x.kind=="DEBIT" else money(x.amount)
    if money(wallet.balance)+signed<0:raise HTTPException(409,"insufficient_wallet_balance")
    wallet.balance=money(wallet.balance)+signed;wallet.version+=1;tx=WalletTransaction(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],kind=x.kind,amount=signed,currency=x.currency,reference=x.reference);s.add_all([wallet,tx]);audit(s,ctx,"billing.wallet."+x.kind.lower());s.commit();return {"id":tx.id,"balance":str(wallet.balance),"version":wallet.version,"duplicate":False}

@router.post("/billing/invoices",status_code=201)
def invoice_create(x:InvoiceIn,ctx=Depends(auth),s:Session=Depends(db),idempotency_key:Optional[str]=Header(default=None,alias="Idempotency-Key",min_length=8,max_length=200)):
    sub=s.scalar(select(BillingSubscription).where(BillingSubscription.tenant_id==ctx["tenant"]));
    if not sub:raise HTTPException(404,"subscription_not_found")
    existing=s.scalar(select(Invoice).where(Invoice.tenant_id==ctx["tenant"],Invoice.request_key==idempotency_key)) if idempotency_key else None
    if existing:return {"id":existing.id,"number":existing.number,"status":existing.status,"total":str(existing.total),"currency":existing.currency,"duplicate":True}
    price=s.get(BillingPrice,sub.price_id);quantity=s.scalar(select(func.sum(UsageEvent.quantity)).where(UsageEvent.subscription_id==sub.id,UsageEvent.occurred_at>=sub.period_start,UsageEvent.occurred_at<sub.period_end)) or 0
    over=max(0,quantity-price.included_units);base=money(price.base_amount);overage=money(Decimal(over)*Decimal(price.overage_amount));subtotal=base+overage
    rule=s.scalar(select(TaxRule).where(TaxRule.jurisdiction==x.jurisdiction,TaxRule.active==True)) if x.jurisdiction else None;tax=money(subtotal*Decimal(rule.rate)) if rule and rule.mode!="NO_TAX" else Decimal("0.00")
    inv=Invoice(id=str(uuid.uuid4()),number="KLY-"+now().strftime("%Y%m%d")+"-"+secrets.token_hex(4).upper(),tenant_id=ctx["tenant"],subscription_id=sub.id,request_key=idempotency_key,currency=price.currency,subtotal=subtotal,tax=tax,total=subtotal+tax,due_at=x.due_at,evidence_json=json.dumps({"price_id":price.id,"price_version":price.version,"usage_quantity":quantity,"tax_rule_id":rule.id if rule else None},sort_keys=True))
    lines=[InvoiceLine(id=str(uuid.uuid4()),invoice_id=inv.id,kind="BASE",description="Subscription",quantity=1,unit_amount=price.base_amount,amount=base,reference=price.id)]
    if over:lines.append(InvoiceLine(id=str(uuid.uuid4()),invoice_id=inv.id,kind="OVERAGE",description="Email overage",quantity=over,unit_amount=price.overage_amount,amount=overage,reference=sub.period_end.isoformat()))
    s.add(inv);s.add_all(lines);audit(s,ctx,"billing.invoice.created");s.commit();return {"id":inv.id,"number":inv.number,"status":inv.status,"total":str(inv.total),"currency":inv.currency,"duplicate":False}

@router.post("/billing/payment-methods",status_code=201)
def payment_method(x:PaymentMethodIn,ctx=Depends(auth),s:Session=Depends(db)):
    forbidden=("card_number","pan","cvv","cvc")
    if any(word in x.provider_reference.lower() for word in forbidden):raise HTTPException(422,"raw_card_data_forbidden")
    if x.is_default:
        for item in s.scalars(select(PaymentMethodReference).where(PaymentMethodReference.tenant_id==ctx["tenant"],PaymentMethodReference.revoked_at==None)).all():item.is_default=False
    item=PaymentMethodReference(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],provider=x.provider,provider_reference=x.provider_reference,label=x.label,is_default=x.is_default);s.add(item);audit(s,ctx,"billing.payment_method.added");s.commit();return {"id":item.id,"provider":item.provider,"label":item.label,"is_default":item.is_default}

@router.post("/billing/payments",status_code=201)
def pay(x:PaymentIn,ctx=Depends(auth),s:Session=Depends(db)):
    inv=tenant_item(s,Invoice,x.invoice_id,ctx["tenant"])
    if inv.status in {"PAID","VOID","CREDITED"}:raise HTTPException(409,"invoice_not_payable")
    payment=Payment(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],invoice_id=inv.id,provider=x.provider,provider_reference=x.provider_reference,amount=money(x.amount),currency=inv.currency,status="CONFIRMED" if x.provider=="SANDBOX" else "PENDING_RECONCILIATION",confirmed_by=ctx["sub"] if x.provider=="MANUAL_OFFLINE" else "sandbox-adapter")
    s.add(payment)
    if payment.status=="CONFIRMED" and payment.amount>=inv.total:inv.status="PAID"
    audit(s,ctx,"billing.payment.created");s.commit();return {"id":payment.id,"status":payment.status,"invoice_status":inv.status}

@router.post("/billing/payments/{payment_id}/confirm")
def confirm_manual(payment_id:str,ctx=Depends(require("platform_admin")),s:Session=Depends(db)):
    payment=s.get(Payment,payment_id)
    if not payment or payment.provider!="MANUAL_OFFLINE":raise HTTPException(404,"manual_payment_not_found")
    payment.status="CONFIRMED";payment.confirmed_by=ctx["sub"];inv=s.get(Invoice,payment.invoice_id)
    total=s.scalar(select(func.sum(Payment.amount)).where(Payment.invoice_id==inv.id,Payment.status=="CONFIRMED")) or 0;inv.status="PAID" if money(total)>=money(inv.total) else "PARTIALLY_PAID";audit(s,ctx,"billing.manual_payment.confirmed");s.commit();return {"status":payment.status,"invoice_status":inv.status}

@router.post("/billing/payments/{payment_id}/refunds",status_code=201)
def refund(payment_id:str,x:RefundIn,ctx=Depends(auth),s:Session=Depends(db)):
    payment=tenant_item(s,Payment,payment_id,ctx["tenant"]);already=s.scalar(select(func.sum(Refund.amount)).where(Refund.payment_id==payment.id,Refund.status=="CONFIRMED")) or 0
    if money(already)+money(x.amount)>money(payment.amount):raise HTTPException(409,"refund_exceeds_payment")
    item=Refund(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],payment_id=payment.id,amount=money(x.amount),status="CONFIRMED" if payment.provider=="SANDBOX" else "PENDING_RECONCILIATION",provider_reference=x.provider_reference);s.add(item);audit(s,ctx,"billing.refund.created");s.commit();return {"id":item.id,"status":item.status}

@router.post("/billing/checkout",status_code=201)
def checkout(x:CheckoutIn,ctx=Depends(auth),s:Session=Depends(db)):
    if s.scalar(select(CheckoutSession).where(CheckoutSession.provider==x.provider,CheckoutSession.provider_reference==x.provider_reference)):raise HTTPException(409,"checkout_reference_exists")
    plan=s.scalar(select(BillingPlan).where(BillingPlan.code==x.plan_code,BillingPlan.active==True))
    if not plan:raise HTTPException(404,"plan_not_found")
    price=s.scalar(select(BillingPrice).where(BillingPrice.plan_id==plan.id,BillingPrice.retired_at==None).order_by(BillingPrice.version.desc()))
    if not price:raise HTTPException(409,"plan_has_no_active_price")
    existing=s.scalar(select(BillingSubscription).where(BillingSubscription.tenant_id==ctx["tenant"]))
    if existing and existing.status not in {"CANCELLED","CLOSED"}:raise HTTPException(409,"active_subscription_exists")
    state="COMPLETED" if x.provider=="SANDBOX" else "PAYMENT_PENDING"
    item=CheckoutSession(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],plan_id=plan.id,price_id=price.id,provider=x.provider,state=state,provider_reference=x.provider_reference)
    start=now();sub=existing or BillingSubscription(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],plan_id=plan.id,price_id=price.id,period_end=start+timedelta(days=30))
    sub.plan_id=plan.id;sub.price_id=price.id;sub.period_start=start;sub.period_end=start+timedelta(days=365 if price.billing_cycle=="ANNUAL" else 30);sub.status="ACTIVE" if state=="COMPLETED" else "TRIALING"
    billing_event=BillingEvent(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],kind="checkout."+state.lower(),reference=item.id,payload_json=json.dumps({"provider":x.provider,"raw_card_storage":False},sort_keys=True));s.add_all([item,sub,billing_event]);enqueue_subscription_changed(s,sub,causation_id=billing_event.id);audit(s,ctx,"billing.checkout.created");s.commit()
    return {"id":item.id,"state":state,"subscription_id":sub.id,"payment_instructions_required":x.provider=="MANUAL_OFFLINE","raw_card_storage":False}

@router.post("/billing/subscription-plan-change")
def change_plan(x:PlanChangeIn,ctx=Depends(auth),s:Session=Depends(db)):
    sub=s.scalar(select(BillingSubscription).where(BillingSubscription.tenant_id==ctx["tenant"]).with_for_update())
    if not sub or sub.status not in {"TRIALING","ACTIVE"}:raise HTTPException(409,"subscription_not_changeable")
    plan=s.scalar(select(BillingPlan).where(BillingPlan.code==x.plan_code,BillingPlan.active==True));new=s.scalar(select(BillingPrice).where(BillingPrice.plan_id==plan.id,BillingPrice.retired_at==None).order_by(BillingPrice.version.desc())) if plan else None
    if not new:raise HTTPException(404,"active_plan_price_not_found")
    old=s.get(BillingPrice,sub.price_id);period_start=sub.period_start if sub.period_start.tzinfo else sub.period_start.replace(tzinfo=timezone.utc);period_end=sub.period_end if sub.period_end.tzinfo else sub.period_end.replace(tzinfo=timezone.utc);total=max(1,(period_end-period_start).total_seconds());remaining=max(0,(period_end-now()).total_seconds());ratio=Decimal(str(remaining/total))
    delta=money((Decimal(new.base_amount)-Decimal(old.base_amount))*ratio)
    if delta<0:
        s.add(BillingEvent(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],kind="subscription.downgrade_scheduled",reference=sub.id,payload_json=json.dumps({"next_plan_id":plan.id,"next_price_id":new.id,"effective_at":sub.period_end.isoformat()},sort_keys=True)));audit(s,ctx,"billing.subscription.downgrade_scheduled");s.commit();return {"effective":"NEXT_PERIOD","credit":"0.00","charge":"0.00"}
    sub.plan_id=plan.id;sub.price_id=new.id;sub.version+=1
    billing_event=BillingEvent(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],kind="subscription.upgraded",reference=sub.id,payload_json=json.dumps({"old_price_id":old.id,"new_price_id":new.id,"proration_charge":str(delta)},sort_keys=True));s.add(billing_event);enqueue_subscription_changed(s,sub,causation_id=billing_event.id);audit(s,ctx,"billing.subscription.upgraded");s.commit();return {"effective":"IMMEDIATE","charge":str(delta),"credit":"0.00","price_version":new.version}

@router.post("/billing/invoices/{invoice_id}/credit-notes",status_code=201)
def credit_note(invoice_id:str,x:CreditNoteIn,ctx=Depends(auth),s:Session=Depends(db)):
    inv=tenant_item(s,Invoice,invoice_id,ctx["tenant"]);amount=money(x.amount)
    if amount>money(inv.total)-money(inv.credits):raise HTTPException(409,"credit_exceeds_invoice_balance")
    item=CreditNote(id=str(uuid.uuid4()),number="KLY-CN-"+now().strftime("%Y%m%d")+"-"+secrets.token_hex(4).upper(),tenant_id=ctx["tenant"],invoice_id=inv.id,amount=amount,currency=inv.currency,reason=x.reason)
    credit=Credit(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],invoice_id=inv.id,amount=amount,currency=inv.currency,reason=x.reason);inv.credits=money(inv.credits)+amount
    if money(inv.credits)>=money(inv.total):inv.status="CREDITED"
    s.add_all([item,credit]);audit(s,ctx,"billing.credit_note.created");s.commit();return {"id":item.id,"number":item.number,"amount":str(item.amount),"invoice_status":inv.status}

@router.post("/admin/billing/dunning")
def dunning(x:DunningIn,ctx=Depends(require("platform_admin")),s:Session=Depends(db)):
    changed=[];current=now()
    for inv in s.scalars(select(Invoice).where(Invoice.status.in_(("OPEN","PAST_DUE")),Invoice.due_at<current).with_for_update()).all():
        due_at=inv.due_at if inv.due_at.tzinfo else inv.due_at.replace(tzinfo=timezone.utc);overdue=(current-due_at).days;sub=s.get(BillingSubscription,inv.subscription_id)
        inv.status="PAST_DUE"
        target="SUSPENDED" if overdue>=x.suspend_days else "GRACE_PERIOD" if overdue>=x.grace_days else "PAST_DUE"
        if sub and sub.status not in {"CANCELLED","CLOSED"}:sub.status=target;sub.version+=1
        s.add(BillingEvent(id=str(uuid.uuid4()),tenant_id=inv.tenant_id,kind="dunning."+target.lower(),reference=inv.id,payload_json=json.dumps({"days_overdue":overdue,"login_enabled":True,"sending_enabled":target!="SUSPENDED"},sort_keys=True)));changed.append({"invoice_id":inv.id,"subscription_status":target})
    audit(s,ctx,"billing.dunning.run");s.commit();return {"processed":len(changed),"items":changed,"login_disabled":False}

@router.get("/billing/reconciliation")
def reconcile(ctx=Depends(auth),s:Session=Depends(db)):
    issues=[]
    for inv in s.scalars(select(Invoice).where(Invoice.tenant_id==ctx["tenant"])).all():
        paid=money(s.scalar(select(func.sum(Payment.amount)).where(Payment.invoice_id==inv.id,Payment.status=="CONFIRMED")) or 0)
        refunded=money(s.scalar(select(func.sum(Refund.amount)).where(Refund.tenant_id==ctx["tenant"],Refund.status=="CONFIRMED",Refund.payment_id.in_(select(Payment.id).where(Payment.invoice_id==inv.id)))) or 0)
        expected=expected_invoice_status(inv.status,inv.total,paid,refunded)
        if inv.status!=expected:issues.append({"invoice_id":inv.id,"actual":inv.status,"expected":expected})
    return {"status":"PASS" if not issues else "DRIFT","issues":issues,"auto_corrected":False}

@router.get("/billing/portal")
def billing_portal(ctx=Depends(auth),s:Session=Depends(db)):
    sub=s.scalar(select(BillingSubscription).where(BillingSubscription.tenant_id==ctx["tenant"]));wallet=s.get(Wallet,ctx["tenant"]);return {"subscription":sub,"invoices":s.scalars(select(Invoice).where(Invoice.tenant_id==ctx["tenant"])).all(),"payments":s.scalars(select(Payment).where(Payment.tenant_id==ctx["tenant"])).all(),"credits":s.scalars(select(Credit).where(Credit.tenant_id==ctx["tenant"])).all(),"wallet":{"balance":str(wallet.balance),"currency":wallet.currency} if wallet else None,"raw_card_storage":False}

# Stable dedicated-billing-service read and command contract. All lookups remain
# tenant-qualified; the aliases preserve compatibility with the original API.
@router.get("/billing/plan")
def billing_plan(ctx=Depends(auth),s:Session=Depends(db)):
    sub=s.scalar(select(BillingSubscription).where(BillingSubscription.tenant_id==ctx["tenant"]));
    if not sub:raise HTTPException(404,"subscription_not_found")
    plan=s.get(BillingPlan,sub.plan_id);price=s.get(BillingPrice,sub.price_id)
    return {"code":plan.code,"name":plan.name,"features":json.loads(plan.features_json),"price_version":price.version,"currency":price.currency}
@router.get("/billing/subscription")
def billing_subscription(ctx=Depends(auth),s:Session=Depends(db)):
    item=s.scalar(select(BillingSubscription).where(BillingSubscription.tenant_id==ctx["tenant"]));
    if not item:raise HTTPException(404,"subscription_not_found")
    return item
@router.get("/billing/usage")
def billing_usage(ctx=Depends(auth),s:Session=Depends(db)):
    rows=s.scalars(select(UsageEvent).where(UsageEvent.tenant_id==ctx["tenant"]).order_by(UsageEvent.occurred_at.desc()).limit(500)).all();return {"events":rows,"quantity":sum(row.quantity for row in rows),"billing_active":True}
@router.get("/billing/quota")
def billing_quota(ctx=Depends(auth),s:Session=Depends(db)):
    sub=s.scalar(select(BillingSubscription).where(BillingSubscription.tenant_id==ctx["tenant"]));
    if not sub:raise HTTPException(404,"subscription_not_found")
    price=s.get(BillingPrice,sub.price_id);used=s.scalar(select(func.sum(UsageEvent.quantity)).where(UsageEvent.subscription_id==sub.id,UsageEvent.occurred_at>=sub.period_start,UsageEvent.occurred_at<sub.period_end)) or 0
    return {"included":price.included_units,"used":used,"remaining":max(0,price.included_units-used),"behavior":"OVERAGE"}
@router.get("/billing/invoices")
def billing_invoices(ctx=Depends(auth),s:Session=Depends(db)):return s.scalars(select(Invoice).where(Invoice.tenant_id==ctx["tenant"]).order_by(Invoice.created_at.desc())).all()
@router.get("/billing/invoices/{invoice_id}")
def billing_invoice(invoice_id:str,ctx=Depends(auth),s:Session=Depends(db)):
    item=tenant_item(s,Invoice,invoice_id,ctx["tenant"]);lines=s.scalars(select(InvoiceLine).where(InvoiceLine.invoice_id==item.id)).all();return {"invoice":item,"lines":lines}
@router.get("/billing/credits")
def billing_credits(ctx=Depends(auth),s:Session=Depends(db)):return s.scalars(select(Credit).where(Credit.tenant_id==ctx["tenant"]).order_by(Credit.created_at.desc())).all()
@router.post("/billing/payments/manual",status_code=201)
def billing_manual_payment(x:PaymentIn,ctx=Depends(auth),s:Session=Depends(db)):
    if x.provider!="MANUAL_OFFLINE":raise HTTPException(422,"manual_offline_provider_required")
    return pay(x,ctx,s)
@router.post("/billing/refunds",status_code=201)
def billing_refund(payment_id:str,x:RefundIn,ctx=Depends(auth),s:Session=Depends(db)):return refund(payment_id,x,ctx,s)
