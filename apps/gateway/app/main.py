import asyncio, base64, hashlib, hmac, ipaddress, json, os, re, secrets, socket, ssl, time, uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .delivery_safety import email_activation_status, safe_mode_enabled
from .durable_results import read_control_response, seal_control_response
from .durable_keys import keyring_ready
from .capabilities import has_service_permission, mutation_permission
from .telemetry import TraceMiddleware, configure_tracing, stored_carrier, trace_carrier, traced
from typing import Optional

import httpx, jwt
from jwt import PyJWKClient
from argon2 import PasswordHasher
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, EmailStr, Field, ValidationError, field_validator, model_validator
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, func, or_, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

def database_url():
    value=os.getenv("KLYROW_DATABASE_URL", "sqlite:///./klyrow.db")
    password_file=os.getenv("KLYROW_DATABASE_PASSWORD_FILE","").strip()
    production=os.getenv("KLYROW_ENV","development").lower()=="production"
    if production and not password_file:raise RuntimeError("production requires KLYROW_DATABASE_PASSWORD_FILE")
    if not password_file:return value
    path=Path(password_file)
    if production and (not path.is_absolute() or path.is_symlink()):raise RuntimeError("database password file must be absolute and not a symlink")
    try:password=path.read_text(encoding="utf-8").strip()
    except OSError as exc:raise RuntimeError("database password file unavailable") from exc
    if not password or len(password)>1024:raise RuntimeError("database password file is empty or oversized")
    return make_url(value).set(password=password)

def runtime_secret(name:str)->str:
    """Read a rotatable secret without permitting production env injection."""

    path_value=os.getenv(name+"_FILE","").strip()
    if path_value:
        path=Path(path_value)
        if os.getenv("KLYROW_ENV","development").lower()=="production" and (not path.is_absolute() or path.is_symlink()):
            raise RuntimeError(name+" secret file must be absolute and not a symlink")
        try:
            value=path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(name+" secret file unavailable") from exc
        if not value or len(value)>65536:raise RuntimeError(name+" secret file is empty or oversized")
        return value
    value=os.getenv(name,"")
    if os.getenv("KLYROW_ENV","development").lower()=="production" and value:
        raise RuntimeError("production requires "+name+"_FILE")
    return value

def required_session_secret():
    path=os.getenv("KLYROW_SESSION_SECRET_FILE","")
    if path:
        try:value=Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:raise RuntimeError("KLYROW session secret file unavailable") from exc
    else:value=os.getenv("KLYROW_SESSION_SECRET","")
    if os.getenv("KLYROW_ENV", "development").lower()=="production" and not path:raise RuntimeError("production requires KLYROW_SESSION_SECRET_FILE")
    if not value:value=secrets.token_urlsafe(32)
    return value
SECRET=required_session_secret()
if len(SECRET) < 32: raise RuntimeError("KLYROW_SESSION_SECRET must contain at least 32 characters")
DATABASE_URL=database_url()
SAFE_MODE=safe_mode_enabled()
engine=create_engine(DATABASE_URL, pool_pre_ping=True)
DB=sessionmaker(engine, expire_on_commit=False)
ph=PasswordHasher()
app=FastAPI(title="Klyrow API", version="1.0.0", docs_url=None if os.getenv("KLYROW_ENV")=="production" else "/docs")
app.add_middleware(TraceMiddleware)
app.on_event("startup")(configure_tracing)
AUTH_WEB_DIST=Path(__file__).with_name("auth_web")
if not AUTH_WEB_DIST.exists():
    AUTH_WEB_DIST=Path(__file__).parents[2]/"web"/"dist"
app.mount("/auth-assets",StaticFiles(directory=AUTH_WEB_DIST,check_dir=False),name="auth-assets")
class PlatformMetric:
    def __init__(self,collector):
        self.collector=collector
        self.platform_values=("klyrow-email","klyrow.com",os.getenv("KLYROW_SERVICE","gateway"),os.getenv("KLYROW_ENV","development"),os.getenv("CODESTRA_SERVER","provider-communications"),os.getenv("CODESTRA_REGION","eu-central"),os.getenv("CODESTRA_DEPLOYMENT","unknown"))
        self.dynamic_count=len(collector._labelnames)-len(self.platform_values)
    def labels(self,*args,**kwargs):
        if kwargs:raise ValueError("platform metrics require positional bounded labels")
        return self.collector.labels(*(self.platform_values+args))
    def __getattr__(self,name):
        target=self.collector.labels(*self.platform_values) if self.dynamic_count==0 else self.collector
        return getattr(target,name)
def platform_metric(collector):return PlatformMetric(collector)
REQUESTS=platform_metric(Counter("klyrow_http_requests_total","Requests",["codestra_business","application","service","environment","server","region","deployment","path","status"]))
MAIL=platform_metric(Counter("klyrow_mail_total","Mail lifecycle",["codestra_business","application","service","environment","server","region","deployment","event"]))
LATENCY=platform_metric(Histogram("klyrow_http_request_duration_seconds","Request latency",["codestra_business","application","service","environment","server","region","deployment","path"]))
PROVIDER_QUEUE=platform_metric(Gauge("klyrow_provider_queue_messages","Provider messages by status",["codestra_business","application","service","environment","server","region","deployment","status"]))
PROVIDER_EVENTS=platform_metric(Gauge("klyrow_provider_events","Provider events by state",["codestra_business","application","service","environment","server","region","deployment","state"]))
PROVIDER_USAGE=platform_metric(Gauge("klyrow_provider_usage_events","Provider usage events by state",["codestra_business","application","service","environment","server","region","deployment","state"]))
OUTBOX_OLDEST=platform_metric(Gauge("klyrow_email_outbox_oldest_seconds","Age of oldest active email outbox item",["codestra_business","application","service","environment","server","region","deployment"]))
INTEGRATION_QUEUE=platform_metric(Gauge("klyrow_integration_outbox_items","Integration outbox items",["codestra_business","application","service","environment","server","region","deployment","target","state"]))
WEBHOOK_QUEUE=platform_metric(Gauge("klyrow_webhook_attempts","Customer webhook attempts",["codestra_business","application","service","environment","server","region","deployment","state"]))
DELIVERY_RATE=platform_metric(Gauge("klyrow_delivery_ratio","Delivery outcome ratio",["codestra_business","application","service","environment","server","region","deployment","outcome"]))
DNS_INVALID=platform_metric(Gauge("klyrow_domain_dns_invalid","Domains not currently verified for sending",["codestra_business","application","service","environment","server","region","deployment"]))
BILLING_DRIFT=platform_metric(Gauge("klyrow_billing_reconciliation_drift","Invoices whose ledger state disagrees with confirmed payments",["codestra_business","application","service","environment","server","region","deployment"]))
rate_buckets=defaultdict(deque)
_jwks_clients={}

SMTP_EVENT_MAP={
    "email.accepted":"klyrow.email.accepted","email.queued":"klyrow.email.queued",
    "email.submitted":"klyrow.email.submitted","email.sent":"klyrow.email.sent",
    "email.delivered":"klyrow.email.delivered","email.deferred":"klyrow.email.deferred",
    "email.bounced":"klyrow.email.bounced","email.complained":"klyrow.email.complained",
    "email.rejected":"klyrow.email.rejected","email.failed":"klyrow.email.failed",
    "email.cancelled":"klyrow.email.cancelled","email.unknown_outcome":"klyrow.email.unknown_outcome",
    "email.opened":"klyrow.email.opened","email.clicked":"klyrow.email.clicked",
    "email.unsubscribed":"klyrow.email.unsubscribed","email.inbound_received":"klyrow.email.inbound_received",
    "message.submitted":"klyrow.email.submitted","message.delivered":"klyrow.email.delivered",
    "message.opened":"klyrow.email.opened","message.clicked":"klyrow.email.clicked",
    "inbound.received":"klyrow.email.inbound_received",
}
CANONICAL_SMTP_STATUSES={"accepted","queued","processing","submitted","provider_accepted","delivered","deferred","bounced","complained","rejected","suppressed","failed","cancelled","unknown_outcome","indeterminate"}
MIDDLEWARE_ALLOWED_COMMANDS={"email.message.send.v1","email.message.cancel.v1","email.domain.verify.v1","email.suppression.upsert.v1","email.reputation.snapshot.request.v1"}

def set_core_message_status(message,value:str)->None:
    if value not in CANONICAL_SMTP_STATUSES:raise RuntimeError("noncanonical_message_status")
    message.status=value

class Base(DeclarativeBase): pass
class Tenant(Base):
    __tablename__="tenants"; id:Mapped[str]=mapped_column(String,primary_key=True); name:Mapped[str]=mapped_column(String); enabled:Mapped[bool]=mapped_column(Boolean,default=True); quota:Mapped[int]=mapped_column(Integer,default=10000)
class User(Base):
    __tablename__="users"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(ForeignKey("tenants.id")); email:Mapped[str]=mapped_column(String,unique=True,index=True); password_hash:Mapped[str]=mapped_column(String); role:Mapped[str]=mapped_column(String,default="tenant_user"); enabled:Mapped[bool]=mapped_column(Boolean,default=True); reset_hash:Mapped[Optional[str]]=mapped_column(String,nullable=True); reset_expires:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True)
class ApiKey(Base):
    __tablename__="api_keys"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(ForeignKey("tenants.id"),index=True); name:Mapped[str]=mapped_column(String); key_hash:Mapped[str]=mapped_column(String,unique=True); revoked:Mapped[bool]=mapped_column(Boolean,default=False); rotated_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True)
class Domain(Base):
    __tablename__="domains"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(ForeignKey("tenants.id"),index=True); domain:Mapped[str]=mapped_column(String); token:Mapped[str]=mapped_column(String); verified:Mapped[bool]=mapped_column(Boolean,default=False)
    __table_args__=(UniqueConstraint("tenant_id","domain",name="uq_domain_tenant_name"),)
class AllowedSender(Base):
    __tablename__="allowed_senders"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(ForeignKey("tenants.id"),index=True); address:Mapped[str]=mapped_column(String,index=True); role:Mapped[str]=mapped_column(String); enabled:Mapped[bool]=mapped_column(Boolean,default=True)
    __table_args__=(UniqueConstraint("tenant_id","address",name="uq_allowed_sender_tenant_address"),)
class InboundRouteConfig(Base):
    __tablename__="inbound_route_configs"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(ForeignKey("tenants.id"),index=True); address:Mapped[str]=mapped_column(String,index=True); destination_kind:Mapped[str]=mapped_column(String); destination_ref:Mapped[Optional[str]]=mapped_column(String,nullable=True); verified:Mapped[bool]=mapped_column(Boolean,default=False); enabled:Mapped[bool]=mapped_column(Boolean,default=False)
    __table_args__=(UniqueConstraint("tenant_id","address",name="uq_inbound_route_tenant_address"),)
class Message(Base):
    __tablename__="messages"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(ForeignKey("tenants.id"),index=True); recipient:Mapped[str]=mapped_column(String); sender:Mapped[str]=mapped_column(String); subject:Mapped[str]=mapped_column(String); status:Mapped[str]=mapped_column(String); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
class Event(Base):
    __tablename__="events"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(ForeignKey("tenants.id"),index=True); message_id:Mapped[str]=mapped_column(String,index=True); kind:Mapped[str]=mapped_column(String); payload:Mapped[str]=mapped_column(Text); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
class Suppression(Base):
    __tablename__="suppressions"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(ForeignKey("tenants.id"),index=True); email:Mapped[str]=mapped_column(String,index=True); reason:Mapped[str]=mapped_column(String)
class Replay(Base):
    __tablename__="webhook_replays"; id:Mapped[str]=mapped_column(String,primary_key=True); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
class PostalEvent(Base):
    __tablename__="postal_events"; id:Mapped[str]=mapped_column(String,primary_key=True); event_type:Mapped[str]=mapped_column(String,index=True); correlation_id:Mapped[str]=mapped_column(String,index=True); message_id:Mapped[str]=mapped_column(String,index=True); tenant_id:Mapped[str]=mapped_column(String,index=True); payload:Mapped[str]=mapped_column(Text); state:Mapped[str]=mapped_column(String,default="pending",index=True); attempts:Mapped[int]=mapped_column(Integer,default=0); last_error:Mapped[Optional[str]]=mapped_column(String,nullable=True); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc)); updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
class Audit(Base):
    __tablename__="audit_log"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); actor:Mapped[str]=mapped_column(String); action:Mapped[str]=mapped_column(String); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
class Contact(Base):
    __tablename__="contacts"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(ForeignKey("tenants.id"),index=True); email:Mapped[str]=mapped_column(String,index=True); name:Mapped[Optional[str]]=mapped_column(String,nullable=True); subscribed:Mapped[bool]=mapped_column(Boolean,default=True); metadata_json:Mapped[str]=mapped_column(Text,default="{}")
    __table_args__=(UniqueConstraint("tenant_id","email",name="uq_contact_tenant_email"),)
class Campaign(Base):
    __tablename__="campaigns"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(ForeignKey("tenants.id"),index=True); name:Mapped[str]=mapped_column(String); status:Mapped[str]=mapped_column(String,default="draft"); subject:Mapped[Optional[str]]=mapped_column(String,nullable=True); sender_id:Mapped[Optional[str]]=mapped_column(String,nullable=True); template_id:Mapped[Optional[str]]=mapped_column(String,nullable=True); segment_id:Mapped[Optional[str]]=mapped_column(String,nullable=True); current_version:Mapped[int]=mapped_column(Integer,default=1); scheduled_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
class Idempotency(Base):
    __tablename__="idempotency_keys"; id:Mapped[str]=mapped_column(String,primary_key=True,default=lambda:str(uuid.uuid4())); key:Mapped[str]=mapped_column(String); tenant_id:Mapped[str]=mapped_column(String,index=True); request_hash:Mapped[str]=mapped_column(String); resource_id:Mapped[str]=mapped_column(String); response_json:Mapped[str]=mapped_column(Text); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc)); __table_args__=(UniqueConstraint("tenant_id","key",name="uq_idempotency_tenant_key"),)
class EmailOutbox(Base):
    trace_context_json:Mapped[str]=mapped_column(Text,default=lambda:json.dumps(trace_carrier()),server_default="{}")
    __tablename__="email_outbox"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); message_id:Mapped[str]=mapped_column(String,unique=True,index=True); operation_id:Mapped[Optional[str]]=mapped_column(String,nullable=True,index=True); correlation_id:Mapped[Optional[str]]=mapped_column(String,nullable=True,index=True); payload:Mapped[str]=mapped_column(Text); priority:Mapped[int]=mapped_column(Integer,default=20,index=True); state:Mapped[str]=mapped_column(String,default="pending",index=True); attempts:Mapped[int]=mapped_column(Integer,default=0); provider_message_id:Mapped[Optional[str]]=mapped_column(String,nullable=True); last_error:Mapped[Optional[str]]=mapped_column(String,nullable=True); next_attempt_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc)); updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
class MiddlewareCommandOperation(Base):
    __tablename__="middleware_command_operations";command_id:Mapped[str]=mapped_column(String,primary_key=True);tenant_id:Mapped[str]=mapped_column(String,index=True);command:Mapped[str]=mapped_column(String,index=True);idempotency_key:Mapped[str]=mapped_column(String,index=True);correlation_id:Mapped[str]=mapped_column(String,index=True);state:Mapped[str]=mapped_column(String,default="accepted",index=True);request_hash:Mapped[str]=mapped_column(String);request_json:Mapped[str]=mapped_column(Text,default="{}");result_json:Mapped[str]=mapped_column(Text,default="{}");error:Mapped[Optional[str]]=mapped_column(String,nullable=True);created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc));updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc));__table_args__=(UniqueConstraint("tenant_id","idempotency_key",name="uq_middleware_command_tenant_idempotency"),)
class ProductionCanaryGate(Base):
    __tablename__="production_canary_gate"; gate_key:Mapped[str]=mapped_column(String,primary_key=True); reserved_deliveries:Mapped[int]=mapped_column(Integer,default=0); claimed_deliveries:Mapped[int]=mapped_column(Integer,default=0); updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
class WebhookEndpoint(Base):
    __tablename__="webhook_endpoints"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(ForeignKey("tenants.id"),index=True); url:Mapped[str]=mapped_column(String); enabled:Mapped[bool]=mapped_column(Boolean,default=True); secret_hash:Mapped[str]=mapped_column(String); rotated_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True)

def db():
    with DB() as s: yield s
def sha(v): return hashlib.sha256(v.encode()).hexdigest()
def scoped_idempotency_key(ctx:dict,raw_key:str,*,action:str,resource:str,api_version:str="v1")->str:
    """Bind a client key to the complete durable command identity."""
    caller=str(ctx.get("sub") or "unknown")
    service=str(ctx.get("client_id") or ctx.get("service") or "")
    identity="\0".join((ctx["tenant"],caller,service,action,api_version,resource,raw_key))
    return "scope:"+api_version+":"+hashlib.sha256(identity.encode()).hexdigest()
def semantic_request_hash(*,action:str,resource:str,payload:dict,api_version:str="v1")->str:
    canonical=json.dumps(
        {"action":action,"api_version":api_version,"resource":resource,"payload":payload},
        separators=(",",":"),sort_keys=True,default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
def token(user,s):
    from .saas import SessionRecord
    sid=str(uuid.uuid4());s.add(SessionRecord(id=sid,user_id=user.id,tenant_id=user.tenant_id));s.commit();return jwt.encode({"sub":user.id,"tenant":user.tenant_id,"role":user.role,"sid":sid,"exp":datetime.now(timezone.utc)+timedelta(hours=8)},SECRET,algorithm="HS256")
def audit(s, ctx, action): s.add(Audit(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],actor=ctx["sub"],action=action))
def safe_webhook_url(value:str)->str:
    from urllib.parse import urlsplit
    parsed=urlsplit(value)
    if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password:raise HTTPException(422,"unsafe_webhook_url")
    try: addresses={item[4][0] for item in socket.getaddrinfo(parsed.hostname,parsed.port or 443,type=socket.SOCK_STREAM)}
    except OSError:raise HTTPException(422,"webhook_dns_unavailable")
    if not addresses or any(ipaddress.ip_address(address).is_private or ipaddress.ip_address(address).is_loopback or ipaddress.ip_address(address).is_link_local or ipaddress.ip_address(address).is_reserved or ipaddress.ip_address(address).is_unspecified for address in addresses):raise HTTPException(422,"unsafe_webhook_destination")
    return value
def auth_rate(request:Request,action:str):
    identity=request.client.host if request.client else "unknown";now=time.time();bucket=rate_buckets[("auth",action,identity)]
    while bucket and bucket[0]<now-60:bucket.popleft()
    limit=int(os.getenv("KLYROW_AUTH_RATE_PER_MINUTE","10"))
    if len(bucket)>=limit:raise HTTPException(429,"rate_limit_exceeded")
    bucket.append(now)
def auth(request:Request,authorization:str=Header(default=""),x_klyrow_tenant_id:Optional[str]=Header(default=None),x_tenant_id:Optional[str]=Header(default=None,alias="X-Tenant-ID"),s:Session=Depends(db)):
    if not authorization.startswith("Bearer "): raise HTTPException(401,"authentication_required")
    raw=authorization[7:]
    if x_klyrow_tenant_id and x_tenant_id and x_klyrow_tenant_id!=x_tenant_id:raise HTTPException(403,"tenant_header_mismatch")
    requested_tenant=x_klyrow_tenant_id or x_tenant_id
    owner_claims=None;owner_identity_id=None
    try:
        resolver=os.getenv("KLYROW_TENANT_RESOLVER_URL","").strip()
        if resolver:
            if any(name.lower() in {"x-codestra-tenant-id","x-codestra-identity-id","x-codestra-tenant","x-codestra-subject"} for name in request.headers):raise HTTPException(403,"not_found")
            if request.url.path=="/v1/commands":permission="klyrow.middleware.command.write"
            elif request.url.path=="/v1/integrations/results":permission="klyrow.integration.result.write"
            elif request.url.path.startswith("/v1/internal/integrations/"):permission="klyrow.observability.write" if request.method not in {"GET","HEAD","OPTIONS"} else "klyrow.observability.read"
            elif request.url.path.startswith("/v1/operations/"):permission="klyrow.middleware.operation.read" if request.method in {"GET","HEAD","OPTIONS"} else "klyrow.middleware.operation.write"
            elif request.method=="POST" and re.fullmatch(r"/v1/(?:campaigns|campaign-definitions)/[^/]+/schedule",request.url.path):permission="campaign.manage"
            elif required_mutation_permission:=mutation_permission(request.method,request.url.path):permission=required_mutation_permission
            else:permission="klyrow.webhook" if "webhook" in request.url.path else "klyrow.send" if request.method not in {"GET","HEAD","OPTIONS"} else "klyrow.read"
            headers={"Authorization":"Bearer "+raw,"X-Codestra-Required-Permission":permission}
            if requested_tenant:headers["X-Klyrow-Tenant-Id"]=requested_tenant
            try:
                response=httpx.get(resolver,headers=headers,timeout=5,follow_redirects=False)
            except httpx.RequestError:
                raise HTTPException(503,"authorization_unavailable")
            if response.status_code==401:raise HTTPException(401,"invalid_credentials")
            if response.status_code==404:raise HTTPException(404,"not_found")
            if response.status_code>=500:raise HTTPException(503,"authorization_unavailable")
            if response.status_code!=200:raise HTTPException(403,"not_found")
            try:resolved=response.json()
            except (TypeError,ValueError):raise HTTPException(503,"authorization_unavailable")
            if not isinstance(resolved,dict):raise HTTPException(503,"authorization_unavailable")
            if resolved.get("authorized") is not True or resolved.get("permission")!=permission:raise HTTPException(403,"not_found")
            if any(not isinstance(resolved.get(field),str) or not resolved[field].strip() for field in ("identity_id","tenant_id")):
                raise HTTPException(503,"authorization_unavailable")
            if requested_tenant and resolved["tenant_id"]!=requested_tenant:raise HTTPException(403,"not_found")
            ctx={"sub":resolved["identity_id"],"tenant":resolved["tenant_id"],"role":resolved.get("role","tenant_user"),"identity_type":resolved.get("identity_type"),"service":True,"permissions":[permission]}
            tenant=s.get(Tenant,ctx["tenant"])
            if not tenant or not tenant.enabled:raise HTTPException(403,"tenant_suspended")
        else:
            middleware_key=runtime_secret("KLYROW_MIDDLEWARE_API_KEY")
            if middleware_key and hmac.compare_digest(raw.encode(),middleware_key.encode()):
                tenant=s.get(Tenant,requested_tenant) if requested_tenant else None
                if not tenant or not tenant.enabled:raise HTTPException(403,"valid_tenant_required")
                ctx={"sub":"middleware-service","tenant":tenant.id,"role":"tenant_admin","service":True,
                     "identity_type":"SERVICE","permissions":["klyrow.middleware.command.write","klyrow.integration.result.write","klyrow.observability.read","klyrow.observability.write"]}
            elif raw.startswith("kly_"):
                key=s.scalar(select(ApiKey).where(ApiKey.key_hash==sha(raw),ApiKey.revoked==False))
                if not key: raise ValueError()
                tenant=s.get(Tenant,key.tenant_id)
                if not tenant or not tenant.enabled: raise HTTPException(403,"account_suspended")
                ctx={"sub":key.id,"tenant":key.tenant_id,"role":"tenant_admin","api_key":True}
            else:
                header=jwt.get_unverified_header(raw)
                if header.get("alg")=="HS256":
                    if os.getenv("KLYROW_ENV","development").lower()=="production" or os.getenv("KLYROW_LOCAL_AUTH_ENABLED","true").lower()!="true":raise HTTPException(401,"canonical_oidc_required")
                    ctx=jwt.decode(raw,SECRET,algorithms=["HS256"])
                    from .saas import SessionRecord
                    session=s.get(SessionRecord,ctx.get("sid")) if ctx.get("sid") else None
                    if ctx.get("sid") and (not session or session.revoked):raise HTTPException(401,"session_revoked")
                else:
                    from .identity_profile import canonical_issuer
                    issuer=canonical_issuer()
                    client=_jwks_clients.setdefault(issuer,PyJWKClient(issuer+"/protocol/openid-connect/certs",cache_keys=True,lifespan=300))
                    signing_key=client.get_signing_key_from_jwt(raw)
                    audience=os.getenv("KLYROW_OIDC_AUDIENCE","klyrow-api")
                    claims=jwt.decode(raw,signing_key.key,algorithms=["RS256","ES256"],audience=audience,issuer=issuer,options={"require":["exp","iat","sub","iss","aud"]})
                    from .tenancy import OidcIdentity,TenantMember
                    identity=s.scalar(select(OidcIdentity).where(OidcIdentity.issuer==issuer,OidcIdentity.subject==claims["sub"],OidcIdentity.enabled==True))
                    if not identity:raise HTTPException(403,"oidc_identity_not_registered")
                    tenant_id=requested_tenant or identity.default_tenant_id
                    membership=s.scalar(select(TenantMember).where(TenantMember.tenant_id==tenant_id,TenantMember.user_id==identity.user_id,TenantMember.active==True)) if tenant_id else None
                    if not membership:raise HTTPException(403,"tenant_membership_required")
                    owner_claims=claims;owner_identity_id=identity.id
                    ctx={"sub":identity.user_id,"oidc_sub":claims["sub"],"tenant":tenant_id,"role":membership.role,"identity_type":identity.identity_type,"service":str(identity.identity_type).upper() in {"SERVICE","SERVICE_ACCOUNT"},"scopes":set(str(claims.get("scope","")).split())}
                tenant=s.get(Tenant,ctx["tenant"])
                if not tenant or not tenant.enabled:raise HTTPException(403,"account_suspended")
        if str(ctx.get("role") or "").lower()=="platform_admin":
            from .platform_owner_api import resolve_api_owner_proof, validate_api_owner
            if resolver and owner_claims is None:
                owner_claims,owner_identity_id=resolve_api_owner_proof(s,ctx,raw)
            validate_api_owner(s,ctx,claims=owner_claims,identity_id=owner_identity_id)
            request.state.klyrow_platform_owner_api_validated=True
    except HTTPException: raise
    except Exception: raise HTTPException(401,"invalid_credentials")
    now=time.time(); q=rate_buckets[ctx["tenant"]]
    while q and q[0]<now-60:q.popleft()
    if len(q)>=int(os.getenv("KLYROW_RATE_PER_MINUTE","60")): raise HTTPException(429,"rate_limit_exceeded")
    q.append(now); return ctx

def beyvra_service_auth(authorization:str=Header(default=""),x_service_identity:str=Header(default=""),x_service_scopes:str=Header(default=""),s:Session=Depends(db)):
    """Dedicated Server A identity that grants email send/status only."""
    token_file=os.getenv("BEYVRA_EMAIL_SERVICE_TOKEN_FILE","")
    try: expected=Path(token_file).read_text(encoding="utf-8").strip()
    except OSError: raise HTTPException(503,"service_identity_unavailable")
    supplied=authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
    scopes=set(x_service_scopes.split())
    if not expected or not hmac.compare_digest(supplied,expected):raise HTTPException(401,"invalid_service_identity")
    if x_service_identity!="codestra-server-a:beyvra-email-production":raise HTTPException(403,"wrong_service_identity")
    if "email.send" not in scopes:raise HTTPException(403,"email_send_scope_required")
    tenant_id=os.getenv("BEYVRA_EMAIL_TENANT_ID","");tenant=s.get(Tenant,tenant_id) if tenant_id else None
    if not tenant or not tenant.enabled:raise HTTPException(403,"beyvra_tenant_unavailable")
    return {"sub":"beyvra-email-production","tenant":tenant.id,"role":"email_service","service":True,"scopes":scopes}
def require(*roles):
    def inner(ctx=Depends(auth)):
        if ctx["role"] not in roles: raise HTTPException(403,"insufficient_role")
        return ctx
    return inner

def require_middleware_command_scope(ctx:dict):
    if not has_service_permission(ctx,"klyrow.middleware.command.write"):
        raise HTTPException(403,"middleware_command_scope_required")

def canary_configuration()->tuple[str,str,str,int]:
    try:maximum=int(os.getenv("KLYROW_CANARY_MAX_DELIVERIES","0"))
    except (TypeError,ValueError):maximum=-1
    return (os.getenv("KLYROW_CANARY_ALLOWED_DOMAIN","").lower(),os.getenv("KLYROW_CANARY_ALLOWED_SENDER","").lower(),os.getenv("KLYROW_CANARY_ALLOWED_RECIPIENT","").lower(),maximum)

def canary_gate_key()->str:return os.getenv("KLYROW_CANARY_GATE_KEY","klyrow-single-domain")

def canary_configuration_valid()->bool:
    domain,sender,recipient,maximum=canary_configuration()
    return bool(domain and sender and recipient and maximum==1 and sender.endswith("@"+domain))

def campaign_execution_mode()->str:
    mode=os.getenv("KLYROW_CAMPAIGN_EXECUTION_MODE","CAMPAIGN_EXECUTION_DISABLED").upper()
    if mode not in {"CAMPAIGN_EXECUTION_DISABLED","CAMPAIGN_CANARY_ONLY","CAMPAIGN_PRODUCTION_ENABLED"}:
        return "CAMPAIGN_EXECUTION_DISABLED"
    return mode

def enforce_campaign_canary(x,ctx,s):
    mode=campaign_execution_mode()
    if mode=="CAMPAIGN_EXECUTION_DISABLED":raise HTTPException(403,"campaign_execution_disabled")
    if mode=="CAMPAIGN_PRODUCTION_ENABLED":return
    if os.getenv("KLYROW_ENV","").lower()!="production" or os.getenv("KLYROW_CAMPAIGN_CANARY_ENABLED","false").lower()!="true":raise HTTPException(403,"campaign_canary_disabled")
    try:maximum=int(os.getenv("KLYROW_CAMPAIGN_CANARY_MAX_RECIPIENTS","1"))
    except ValueError:maximum=0
    if maximum!=1:raise HTTPException(503,"campaign_canary_hard_limit_invalid")
    if ctx["tenant"]!=os.getenv("KLYROW_CAMPAIGN_CANARY_TENANT_ID",""):raise HTTPException(403,"campaign_canary_tenant_denied")
    if not x.campaign_id or x.campaign_id!=os.getenv("KLYROW_CAMPAIGN_CANARY_CAMPAIGN_ID",""):raise HTTPException(403,"campaign_canary_campaign_denied")
    allowed={value.strip().lower() for value in os.getenv("KLYROW_CAMPAIGN_CANARY_RECIPIENTS","").split(",") if value.strip()}
    if len(allowed)!=1 or str(x.to).lower() not in allowed:raise HTTPException(403,"campaign_canary_recipient_denied")
    sender=os.getenv("KLYROW_CAMPAIGN_CANARY_SENDER","").lower()
    if not sender or x.sender.lower()!=sender:raise HTTPException(403,"campaign_canary_sender_denied")
    gate_key="campaign:"+x.campaign_id
    gate=s.scalar(select(ProductionCanaryGate).where(ProductionCanaryGate.gate_key==gate_key).with_for_update()) if s else None
    if s and not gate:
        gate=ProductionCanaryGate(gate_key=gate_key,reserved_deliveries=0,claimed_deliveries=0);s.add(gate);s.flush()
    if gate and gate.reserved_deliveries>=maximum:raise HTTPException(409,"campaign_canary_limit_reached")
    if gate:gate.reserved_deliveries+=1;gate.updated_at=datetime.now(timezone.utc)

def campaign_canary_payload_allowed(payload:dict,tenant_id:str)->bool:
    try:maximum=int(os.getenv("KLYROW_CAMPAIGN_CANARY_MAX_RECIPIENTS","1"))
    except ValueError:return False
    recipients=payload.get("to")
    allowed={value.strip().lower() for value in os.getenv("KLYROW_CAMPAIGN_CANARY_RECIPIENTS","").split(",") if value.strip()}
    normalized=[value.lower() for value in recipients] if isinstance(recipients,list) and all(isinstance(value,str) for value in recipients) else []
    return (campaign_execution_mode()=="CAMPAIGN_CANARY_ONLY" and os.getenv("KLYROW_ENV","").lower()=="production"
        and os.getenv("KLYROW_CAMPAIGN_CANARY_ENABLED","false").lower()=="true" and maximum==1
        and tenant_id==os.getenv("KLYROW_CAMPAIGN_CANARY_TENANT_ID","")
        and payload.get("campaign_id")==os.getenv("KLYROW_CAMPAIGN_CANARY_CAMPAIGN_ID","")
        and payload.get("from","").lower()==os.getenv("KLYROW_CAMPAIGN_CANARY_SENDER","").lower()
        and len(allowed)==1 and normalized==[next(iter(allowed))] and payload.get("stream")=="marketing")

def campaign_worker_payload_allowed(payload:dict,tenant_id:str)->bool:
    if campaign_execution_mode()=="CAMPAIGN_PRODUCTION_ENABLED":return payload.get("stream")=="marketing"
    return campaign_canary_payload_allowed(payload,tenant_id)

def production_gate_open(s:Session)->bool:
    if SAFE_MODE or os.getenv("KLYROW_PRODUCTION_GATE_APPROVED","false").lower()!="true" or not canary_configuration_valid():return False
    gate=s.get(ProductionCanaryGate,canary_gate_key())
    maximum=canary_configuration()[3]
    return bool(gate and gate.reserved_deliveries<maximum and gate.claimed_deliveries<=gate.reserved_deliveries)

def canary_payload_allowed(payload:dict)->bool:
    domain,sender,recipient,maximum=canary_configuration()
    recipients=payload.get("to")
    normalized_recipients=[value.lower() for value in recipients] if isinstance(recipients,list) and all(isinstance(value,str) for value in recipients) else []
    allowed_campaign=os.getenv("KLYROW_CANARY_ALLOWED_CAMPAIGN","")
    return canary_configuration_valid() and bool(allowed_campaign) and payload.get("campaign_id")==allowed_campaign and payload.get("from","").lower()==sender and normalized_recipients==[recipient]

def enforce_production_canary(x,s):
    if SAFE_MODE:return
    if x.stream=="marketing":return
    domain,sender,recipient,maximum=canary_configuration()
    if not canary_configuration_valid():raise HTTPException(503,"production_canary_configuration_invalid")
    allowed_campaign=os.getenv("KLYROW_CANARY_ALLOWED_CAMPAIGN","")
    if x.stream!="transactional" or not allowed_campaign or x.campaign_id!=allowed_campaign:raise HTTPException(403,"canary_campaign_denied")
    if x.sender.lower()!=sender or x.sender.lower().rsplit("@",1)[1]!=domain:raise HTTPException(403,"canary_sender_denied")
    if str(x.to).lower()!=recipient:raise HTTPException(403,"canary_recipient_denied")
    gate=s.scalar(select(ProductionCanaryGate).where(ProductionCanaryGate.gate_key==canary_gate_key()).with_for_update())
    if not gate:raise HTTPException(503,"production_canary_ledger_missing")
    if gate.reserved_deliveries>=maximum:raise HTTPException(409,"production_canary_limit_reached")
    gate.reserved_deliveries+=1;gate.updated_at=datetime.now(timezone.utc)

class Login(BaseModel): email:EmailStr; password:str; otp:Optional[str]=None
class ResetRequest(BaseModel): email:EmailStr
class Reset(BaseModel): token:str; password:str=Field(min_length=14)
class KeyIn(BaseModel): name:str=Field(min_length=1,max_length=80)
class DomainIn(BaseModel): domain:str=Field(pattern=r"^[a-z0-9][a-z0-9.-]+$")
class MailIn(BaseModel):
    customer_id:Optional[str]=None; to:EmailStr; sender:EmailStr; reply_to:Optional[EmailStr]=None; subject:str=Field(max_length=998); html:str=Field(max_length=100000); text:Optional[str]=None; template_id:Optional[str]=None; campaign_id:Optional[str]=None; tags:list[str]=Field(default_factory=list,max_length=50); tracking:dict=Field(default_factory=dict); callback_metadata:dict=Field(default_factory=dict); headers:dict[str,str]=Field(default_factory=dict); stream:str=Field(default="transactional",pattern="^(marketing|transactional|security|system|bulk)$"); topic:str="marketing"
    @model_validator(mode="before")
    @classmethod
    def accept_from_alias(cls,data):
        if isinstance(data,dict) and "sender" not in data and "from" in data:data={**data,"sender":data["from"]}
        return data
    @field_validator("headers")
    @classmethod
    def validate_governed_headers(cls,value):
        allowed={"Message-ID","In-Reply-To","References"}
        if not set(value)<=allowed:raise ValueError("unsupported_mail_header")
        if any(not item or len(item)>4096 or "\r" in item or "\n" in item for item in value.values()):raise ValueError("invalid_mail_header")
        return value
class BulkMailIn(BaseModel): messages:list[MailIn]=Field(min_length=1,max_length=100)
class ContactIn(BaseModel): email:EmailStr; name:Optional[str]=Field(default=None,max_length=200); subscribed:bool=True; metadata:dict={}
class CampaignIn(BaseModel): name:str=Field(min_length=1,max_length=200); subject:Optional[str]=Field(default=None,max_length=998)
class TenantIn(BaseModel): name:str=Field(min_length=1,max_length=200); quota:int=Field(default=10000,ge=0,le=10000000)
class QuotaIn(BaseModel): quota:int=Field(ge=0,le=10000000)
class WebhookIn(BaseModel): url:str=Field(pattern=r"^https://")
class MiddlewareCommandIn(BaseModel):
    command:str=Field(min_length=1,max_length=120);payload:dict=Field(default_factory=dict);target:Optional[str]=Field(default=None,max_length=120);tenant_id:Optional[str]=Field(default=None,max_length=120);correlation_id:Optional[str]=Field(default=None,max_length=200)

async def emit_middleware(event_type:str,payload:dict)->bool:
    base=os.getenv("KLYROW_MIDDLEWARE_URL","").rstrip("/"); email_target=os.getenv("KLYROW_EMAIL_EVENT_URL","").strip(); key=runtime_secret("KLYROW_MIDDLEWARE_API_KEY"); secret=runtime_secret("KLYROW_WEBHOOK_SECRET")
    if not key or not secret:return False
    source_payload=dict(payload)
    source_payload_hash=str(source_payload.get("payload_hash") or hashlib.sha256(json.dumps(source_payload,separators=(",",":"),sort_keys=True,default=str).encode()).hexdigest())
    event_id=payload.get("event_id") or str(uuid.uuid4())
    if event_type.startswith(("klyrow.email.", "klyrow.message.")) and event_type != "klyrow.email.inbound_received":
        payload={"event_id":event_id,"schema_version":str(payload.get("schema_version") or payload.get("event_version") or "1.0"),"source_system":"klyrow","event_type":event_type,"event_version":str(payload.get("event_version") or "1.0"),
            "occurred_at":str(payload.get("occurred_at") or datetime.now(timezone.utc).isoformat()),"tenant_id":str(payload.get("tenant_id") or payload.get("customer_id") or ""),
            "operation_id":str(payload.get("operation_id") or payload.get("message_id") or event_id),"payload_hash":source_payload_hash,
            "message_id":str(payload.get("message_id") or ""),"provider_message_id":str(payload.get("provider_message_id") or payload.get("message_id") or ""),
            "stream":str(payload.get("stream") or "transactional"),"recipient_reference":str(payload.get("recipient_reference") or "sha256:"+hashlib.sha256(str(payload.get("recipient") or "").lower().encode()).hexdigest()),
            "status":str(payload.get("canonical_status") or payload.get("status") or event_type.rsplit(".",1)[-1]),"provider":str(payload.get("provider") or "postal"),
            "correlation_id":str(payload.get("correlation_id") or event_id),"causation_id":str(payload.get("causation_id") or payload.get("correlation_id") or event_id),
            "attempt":max(1,int(payload.get("attempt") or 1)),"metadata":payload.get("metadata") if isinstance(payload.get("metadata"),dict) else {}}
    else:payload={"event_id":event_id,"source_system":"klyrow","event_type":event_type,"timestamp":datetime.now(timezone.utc).isoformat(),**payload}
    body=json.dumps(payload,separators=(",",":"),sort_keys=True).encode(); ts=str(int(time.time())); canonical=ts.encode()+b"\n"+event_id.encode()+b"\nklyrow\n"+body
    tenant_id=str(payload.get("tenant_id") or payload.get("customer_id") or "");correlation_id=str(payload.get("correlation_id") or event_id)
    signature=hmac.new(secret.encode(),canonical,hashlib.sha256).hexdigest(); headers={"Authorization":"Bearer "+key,"Content-Type":"application/json","X-Source-System":"klyrow","X-Tenant-ID":tenant_id,"X-Correlation-ID":correlation_id,"Idempotency-Key":"klyrow:event:"+event_id,"X-Klyrow-Timestamp":ts,"X-Klyrow-Event-Id":event_id,"X-Klyrow-Signature":"sha256="+signature}
    # Email lifecycle events have one canonical callback authority.  Do not
    # send them through the legacy generic route first: a failure there used
    # to abort delivery before the dedicated, mTLS-protected endpoint was
    # attempted and incorrectly exhausted the outbox into DLQ.
    dedicated_event = event_type == "klyrow.usage.recorded" or event_type.startswith(("klyrow.email.", "klyrow.message."))
    if dedicated_event and email_target:
        targets=[email_target]
    else:
        if not base:return False
        path={"klyrow.email.bounced":"bounces","klyrow.email.complained":"complaints","klyrow.email.unsubscribed":"unsubscribes"}.get(event_type,"events")
        targets=[f"{base}/api/v1/klyrow/{path}"]
    try:
        if any(not target.lower().startswith("https://") for target in targets):
            raise RuntimeError("plaintext_middleware_target_denied")
        ca_file=os.getenv("KLYROW_SERVER_A_CA_FILE","").strip()
        cert_file=os.getenv("KLYROW_SERVER_A_CLIENT_CERT_FILE","").strip()
        key_file=os.getenv("KLYROW_SERVER_A_CLIENT_KEY_FILE","").strip()
        if not ca_file or not cert_file or not key_file:
            raise RuntimeError("middleware_mtls_material_required")
        for path_value in (ca_file,cert_file,key_file):
            if not Path(path_value).is_file():raise RuntimeError("middleware_mtls_material_unavailable")
        tls_context=ssl.create_default_context(cafile=ca_file)
        tls_context.minimum_version=ssl.TLSVersion.TLSv1_2
        tls_context.load_cert_chain(certfile=cert_file,keyfile=key_file)
        async with httpx.AsyncClient(timeout=3,trust_env=False,verify=tls_context) as client:
            for target in targets:
                response=await client.post(target,headers=headers,content=body)
                response.raise_for_status()
        return True
    except Exception as exc:
        print(json.dumps({"level":"warning","system":"klyrow","event_id":event_id,"message_id":payload.get("message_id"),"event":"middleware_delivery_failed","error":type(exc).__name__,
            "http_status":exc.response.status_code if isinstance(exc,httpx.HTTPStatusError) else None}))
        return False

POSTAL_EVENTS={
    "MessageSent":"email.delivered", "MessageDeliveryFailed":"email.bounced",
    "MessageBounced":"email.bounced", "MessageDelayed":"email.deferred",
    "MessageHeld":"email.deferred", "MessageLoaded":"email.opened",
    "MessageLinkClicked":"email.clicked",
}

TERMINAL_MESSAGE_STATUSES={
    "email.sent":"provider_accepted", "email.delivered":"delivered", "email.soft_bounce":"deferred",
    "email.deferred":"deferred",
    "email.hard_bounce":"bounced", "email.bounced":"bounced",
    "email.complained":"complained", "email.complaint":"complained",
    "email.rejected":"failed", "email.failed":"failed",
}
SUPPRESSION_STATUSES={"bounced", "complained"}


def classify_bounce(raw_status:Optional[str])->tuple[str,bool]:
    """Classify enhanced SMTP status without suppressing policy/spam failures."""
    value=(raw_status or "").lower()
    match=re.search(r"\b([245]\.\d\.\d)\b",value)
    code=match.group(1) if match else ""
    if code=="5.7.1":return "failed",False
    if code.startswith("4.") or code in {"5.2.2","5.3.0","5.4.0"}:return "deferred",False
    if code.startswith("5."):return "bounced",True
    return "bounced",True

def reputation_signal(raw_status:Optional[str])->bool:
    return bool(re.search(r"\b5\.7\.1\b",(raw_status or "").lower()))


def persist_email_event(s:Session, *, event_id:str, tenant_id:str, message_id:str, correlation_id:str, event_type:str, recipient:Optional[str], raw_status:Optional[str], payload:str):
    canonical_status=TERMINAL_MESSAGE_STATUSES.get(event_type,event_type.rsplit(".",1)[-1])
    suppress_recipient=canonical_status in SUPPRESSION_STATUSES
    if canonical_status=="bounced":canonical_status,suppress_recipient=classify_bounce(raw_status)
    local_message=s.get(Message,message_id) or s.get(Message,correlation_id)
    if local_message:set_core_message_status(local_message,canonical_status)
    from .provider import ProviderMessage
    provider_message=s.scalar(select(ProviderMessage).where(ProviderMessage.tenant_id==tenant_id,
        or_(ProviderMessage.id==message_id,ProviderMessage.provider_message_id==message_id,
            ProviderMessage.correlation_id==correlation_id)))
    if provider_message:
        provider_status={"provider_accepted":"SENT","delivered":"DELIVERED","deferred":"DEFERRED",
            "bounced":"BOUNCED_HARD","complained":"COMPLAINED","failed":"FAILED"}.get(canonical_status,canonical_status.upper())
        if provider_status in {"SENT","DELIVERED","BOUNCED_SOFT","BOUNCED_HARD","COMPLAINED","FAILED","DEFERRED"}:
            provider_message.status=provider_status;provider_message.updated_at=datetime.now(timezone.utc)
    # Keep the browser webmail projection synchronized with the same signed,
    # tenant-attributed provider lifecycle event as the core message.
    from .webmail import update_outbound_status
    update_outbound_status(s, tenant_id, message_id, canonical_status)
    if not s.get(Event,event_id):
        s.add(Event(id=event_id,tenant_id=tenant_id,message_id=local_message.id if local_message else message_id,kind="klyrow."+event_type,payload=payload))
        s.add(Audit(id=str(uuid.uuid4()),tenant_id=tenant_id,actor="provider:postal",action="email.status."+canonical_status))
        if reputation_signal(raw_status):s.add(Audit(id=str(uuid.uuid4()),tenant_id=tenant_id,actor="provider:postal",action="email.reputation_signal.policy_rejection"))
    if suppress_recipient and recipient:
        email=recipient.lower()
        suppression=s.scalar(select(Suppression).where(Suppression.tenant_id==tenant_id,Suppression.email==email))
        if not suppression:s.add(Suppression(id=str(uuid.uuid4()),tenant_id=tenant_id,email=email,reason=canonical_status))
        elif suppression.reason!=canonical_status:suppression.reason=canonical_status
    s.commit()
    return canonical_status

def verify_postal_signature(body:bytes,signature:str):
    key_path=os.getenv("KLYROW_POSTAL_WEBHOOK_PUBLIC_KEY","")
    if not key_path or not signature:raise HTTPException(401,"postal_signature_required")
    try:
        public_key=serialization.load_pem_public_key(Path(key_path).read_bytes())
        public_key.verify(base64.b64decode(signature,validate=True),body,padding.PKCS1v15(),hashes.SHA256())
    except (OSError,ValueError,TypeError,InvalidSignature):raise HTTPException(401,"invalid_postal_signature")

@app.post("/v1/webhooks/postal-native",status_code=202)
async def postal_native_hook(request:Request,x_postal_signature_256:str=Header(default=""),s:Session=Depends(db)):
    body=await request.body(); verify_postal_signature(body,x_postal_signature_256)
    try: raw=json.loads(body)
    except ValueError:raise HTTPException(400,"invalid_json")
    event_id=str(raw.get("uuid") or "")
    if not event_id:raise HTTPException(400,"event_id_required")
    try: event_timestamp=float(raw.get("timestamp"))
    except (TypeError,ValueError):raise HTTPException(401,"invalid_postal_timestamp")
    # Postal's native retry schedule can legitimately deliver an older event;
    # reject future timestamps and stale captures outside one bounded day.
    if event_timestamp>time.time()+300 or event_timestamp<time.time()-86400:raise HTTPException(401,"expired_postal_event")
    event_name=str(raw.get("event") or ""); canonical=POSTAL_EVENTS.get(event_name)
    if not canonical:raise HTTPException(422,"unsupported_postal_event")
    payload=raw.get("payload") or {}; message=payload.get("message") or payload.get("original_message") or {}
    tenant=os.getenv("KLYROW_POSTAL_TENANT_ID","")
    if not tenant:raise HTTPException(503,"postal_tenant_not_configured")
    message_id=str(message.get("id") or message.get("message_id") or "")
    correlation=str(payload.get("correlation_id") or message.get("tag") or message.get("message_id") or event_id)
    recipient=str(message.get("to") or "")
    canonical_status=TERMINAL_MESSAGE_STATUSES.get(canonical,canonical.rsplit(".",1)[-1])
    normalized={"event":canonical,"event_id":event_id,"event_version":"1.0","tenant_id":tenant,"message_id":message_id,"provider_message_id":str(message.get("message_id") or message_id),"stream":"transactional","correlation_id":correlation,"causation_id":correlation,"recipient_reference":"sha256:"+hashlib.sha256(recipient.lower().encode()).hexdigest(),"recipient":recipient,"sender":message.get("from"),"provider":"postal","status":payload.get("status") or canonical.rsplit(".",1)[-1],"canonical_status":canonical_status,"occurred_at":datetime.fromtimestamp(event_timestamp,timezone.utc).isoformat(),"attempt":1,"metadata":{"provider_event":event_name,"provider_message_token":message.get("token")}}
    item=s.get(PostalEvent,event_id)
    if item and item.state=="delivered":return {"accepted":True,"duplicate":True}
    if not item:
        item=PostalEvent(id=event_id,event_type=canonical,correlation_id=correlation,message_id=message_id,tenant_id=tenant,payload=json.dumps(normalized,separators=(",",":"),sort_keys=True)); s.add(item)
    item.attempts=(item.attempts or 0)+1; item.updated_at=datetime.now(timezone.utc); s.commit()
    canonical_status=persist_email_event(s,event_id=event_id,tenant_id=tenant,message_id=message_id,correlation_id=correlation,event_type=canonical,recipient=normalized.get("recipient"),raw_status=normalized.get("status"),payload=item.payload)
    item=s.get(PostalEvent,event_id);item.payload=json.dumps(normalized,separators=(",",":"),sort_keys=True);s.commit()
    canonical_event=SMTP_EVENT_MAP.get(canonical)
    if not canonical_event:raise HTTPException(422,"unsupported_postal_event")
    delivered=await emit_middleware(canonical_event,{**normalized,"customer_id":tenant})
    item=s.get(PostalEvent,event_id)
    if delivered:
        item.state="delivered"; item.last_error=None; s.commit(); MAIL.labels(canonical.rsplit(".",1)[-1]).inc(); return {"accepted":True}
    item.state="dlq" if item.attempts>=5 else "retry"; item.last_error="middleware_delivery_failed"; s.commit()
    raise HTTPException(503,"middleware_delivery_pending")

@app.on_event("startup")
def startup():
    if os.getenv("KLYROW_ENV","development").lower()=="production":
        required=os.getenv("KLYROW_REQUIRED_SCHEMA_VERSION","")
        if not required:raise RuntimeError("production requires KLYROW_REQUIRED_SCHEMA_VERSION")
        with engine.connect() as connection:
            present=connection.execute(text("SELECT count(*) FROM klyrow_schema_migrations WHERE version=:version"),{"version":required}).scalar_one()
            if os.getenv("KLYROW_REQUIRE_LEAST_PRIVILEGE_DB","true").lower()=="true":
                role, *privileges=connection.execute(text("SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname=current_user")).one()
                if role!=os.getenv("KLYROW_DATABASE_RUNTIME_ROLE","klyrow_runtime"):raise RuntimeError("unexpected runtime database role")
                if any(bool(value) for value in privileges):raise RuntimeError("runtime database role has cluster-level privileges")
        if present!=1:raise RuntimeError("required database migration is not applied")
    else:
        Base.metadata.create_all(engine)
    with DB() as s:
        if not s.scalar(select(User).limit(1)):
            email=os.getenv("KLYROW_ADMIN_EMAIL"); password=os.getenv("KLYROW_ADMIN_PASSWORD")
            if email and password and len(password)>=14:
                t=Tenant(id=str(uuid.uuid4()),name="Klyrow",quota=int(os.getenv("KLYROW_DAILY_QUOTA","10000"))); s.add(t); s.add(User(id=str(uuid.uuid4()),tenant_id=t.id,email=email.lower(),password_hash=ph.hash(password),role="platform_admin")); s.commit()

async def postal_retry_loop():
    while True:
        await asyncio.sleep(5)
        try:
            with DB() as s:
                pending=list(s.scalars(select(PostalEvent).where(PostalEvent.state=="retry",PostalEvent.attempts<5).order_by(PostalEvent.updated_at).limit(20)).all())
            for snapshot in pending:
                updated=snapshot.updated_at
                if updated.tzinfo is None:updated=updated.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc)-updated).total_seconds()<min(60,2**max(snapshot.attempts,1)):continue
                payload=json.loads(snapshot.payload)
                canonical_event=SMTP_EVENT_MAP.get(snapshot.event_type)
                if not canonical_event:raise RuntimeError("undeclared_postal_event")
                delivered=await emit_middleware(canonical_event,{**payload,"customer_id":snapshot.tenant_id})
                with DB() as s:
                    item=s.get(PostalEvent,snapshot.id)
                    if not item or item.state!="retry":continue
                    item.attempts=(item.attempts or 0)+1;item.updated_at=datetime.now(timezone.utc)
                    if delivered:
                        item.state="delivered";item.last_error=None
                    elif item.attempts>=5:
                        item.state="dlq";item.last_error="middleware_delivery_failed"
                    s.commit()
        except Exception as exc:
            print(json.dumps({"level":"warning","system":"klyrow","event":"postal_retry_worker_error","error":type(exc).__name__}))

async def email_outbox_loop():
    while True:
        await asyncio.sleep(2)
        if SAFE_MODE:continue
        snapshot=None
        try:
            with DB() as s:
                stale=datetime.now(timezone.utc)-timedelta(minutes=5)
                current=datetime.now(timezone.utc)
                from .provider_outcome import INDETERMINATE,reconcile_before_retry
                abandoned=s.scalar(select(EmailOutbox).where(EmailOutbox.state=="sending",EmailOutbox.updated_at<stale).order_by(EmailOutbox.updated_at).with_for_update(skip_locked=True))
                if abandoned and not reconcile_before_retry(state=INDETERMINATE,provider_message_id=abandoned.provider_message_id,provider_absence_confirmed=False):
                    abandoned.state=INDETERMINATE;abandoned.last_error="abandoned_submission_requires_reconciliation";abandoned.next_attempt_at=None;abandoned.updated_at=current
                    message=s.get(Message,abandoned.message_id)
                    if message:set_core_message_status(message,"indeterminate")
                    s.commit();continue
                item=s.scalar(select(EmailOutbox).where(or_(EmailOutbox.state=="pending",(EmailOutbox.state=="retry") & (or_(EmailOutbox.next_attempt_at.is_(None),EmailOutbox.next_attempt_at<=current))),EmailOutbox.attempts<5).order_by(EmailOutbox.priority,EmailOutbox.created_at).with_for_update(skip_locked=True))
                if not item:continue
                try:payload=json.loads(item.payload)
                except (TypeError,ValueError):
                    item.state="quarantined";item.last_error="invalid_outbox_payload";item.updated_at=current;s.commit();continue
                if not isinstance(payload,dict):
                    item.state="quarantined";item.last_error="invalid_outbox_payload";item.updated_at=current;s.commit();continue
                campaign_payload=payload.get("stream")=="marketing"
                campaign_production=campaign_payload and campaign_execution_mode()=="CAMPAIGN_PRODUCTION_ENABLED"
                gate_key=("campaign:"+str(payload.get("campaign_id"))) if campaign_payload else canary_gate_key()
                gate=None if campaign_production else s.scalar(select(ProductionCanaryGate).where(ProductionCanaryGate.gate_key==gate_key).with_for_update())
                maximum=1 if campaign_payload else canary_configuration()[3]
                first_attempt=(item.attempts or 0)==0
                reservation_denied=False if campaign_production else (not gate or (first_attempt and
                    (gate.claimed_deliveries>=gate.reserved_deliveries or gate.claimed_deliveries>=maximum)))
                payload_allowed=campaign_worker_payload_allowed(payload,item.tenant_id) if campaign_payload else canary_payload_allowed(payload)
                if not payload_allowed or reservation_denied:
                    item.state="quarantined";item.last_error="production_canary_policy_denied";item.updated_at=current
                    message=s.get(Message,item.message_id)
                    if message:set_core_message_status(message,"suppressed")
                    queue_email_lifecycle_event(s,kind="email.rejected",tenant_id=item.tenant_id,message_id=item.message_id,operation_id=item.operation_id or item.message_id,correlation_id=item.correlation_id or item.message_id,provider_message_id=item.provider_message_id,recipient=message.recipient if message else None,attempt=max(1,item.attempts or 1))
                    s.commit();continue
                if first_attempt:
                    if gate:gate.claimed_deliveries+=1;gate.updated_at=current
                item.state="sending";item.attempts+=1;item.next_attempt_at=None;item.updated_at=current
                message=s.get(Message,item.message_id)
                if message:set_core_message_status(message,"submitted")
                snapshot=(item.id,item.message_id,item.payload,item.operation_id,item.correlation_id,item.tenant_id,item.trace_context_json);s.commit()
            key_file=os.getenv("KLYROW_POSTAL_API_KEY_FILE","")
            key=Path(key_file).read_text(encoding="utf-8").strip() if key_file else ""
            if not key:raise RuntimeError("postal credential unavailable")
            headers={"X-Server-API-Key":key,"Idempotency-Key":"klyrow:"+snapshot[1]}
            postal_host=os.getenv("KLYROW_POSTAL_API_HOST_HEADER","").strip()
            if postal_host:headers["Host"]=postal_host
            with traced("postal submit", stored_carrier(snapshot[6])):
                headers.update(trace_carrier())
                async with httpx.AsyncClient(timeout=10,trust_env=False,follow_redirects=False) as client:
                    response=await client.post(os.environ["KLYROW_POSTAL_API_URL"]+"/api/v1/send/message",headers=headers,json=json.loads(snapshot[2]));response.raise_for_status();provider_id=str(response.json().get("data",{}).get("message_id") or snapshot[1])
            with DB() as s:
                item=s.get(EmailOutbox,snapshot[0]);message=s.get(Message,snapshot[1])
                if item:item.state="delivered";item.provider_message_id=provider_id;item.last_error=None;item.updated_at=datetime.now(timezone.utc)
                if message:set_core_message_status(message,"provider_accepted")
                if item:queue_email_lifecycle_event(s,kind="email.submitted",tenant_id=item.tenant_id,message_id=item.message_id,operation_id=item.operation_id or item.message_id,correlation_id=item.correlation_id or item.message_id,provider_message_id=provider_id,recipient=message.recipient if message else None,attempt=max(1,item.attempts))
                s.commit()
        except Exception as exc:
            with DB() as s:
                if snapshot is not None:
                    item=s.get(EmailOutbox,snapshot[0])
                    if item:
                        from .provider_outcome import INDETERMINATE,provider_outcome_is_ambiguous,reconcile_before_retry
                        if provider_outcome_is_ambiguous(exc) and not reconcile_before_retry(state=INDETERMINATE,provider_message_id=item.provider_message_id,provider_absence_confirmed=False):
                            item.state=INDETERMINATE;item.last_error=type(exc).__name__;item.updated_at=datetime.now(timezone.utc);item.next_attempt_at=None
                            message=s.get(Message,item.message_id)
                            if message:set_core_message_status(message,"indeterminate")
                            queue_email_lifecycle_event(s,kind="email.unknown_outcome",tenant_id=item.tenant_id,message_id=item.message_id,operation_id=item.operation_id or item.message_id,correlation_id=item.correlation_id or item.message_id,provider_message_id=item.provider_message_id,recipient=message.recipient if message else None,attempt=max(1,item.attempts))
                        else:
                            failed=item.attempts>=5;item.state="failed" if failed else "retry";item.last_error=type(exc).__name__;item.updated_at=datetime.now(timezone.utc);item.next_attempt_at=None if failed else item.updated_at+timedelta(seconds=min(300,2**max(item.attempts,1)))
                            message=s.get(Message,item.message_id)
                            if message and not failed:set_core_message_status(message,"deferred")
                            if failed:
                                if message:set_core_message_status(message,"failed")
                                s.add(Event(id=str(uuid.uuid4()),tenant_id=item.tenant_id,message_id=item.message_id,kind="klyrow.email.failed",payload=json.dumps({"reason":"provider_retry_exhausted"})))
                                queue_email_lifecycle_event(s,kind="email.failed",tenant_id=item.tenant_id,message_id=item.message_id,operation_id=item.operation_id or item.message_id,correlation_id=item.correlation_id or item.message_id,provider_message_id=item.provider_message_id,recipient=message.recipient if message else None,attempt=max(1,item.attempts))
                        s.commit()
            print(json.dumps({"level":"warning","system":"klyrow","event":"email_outbox_delivery_failed","error":type(exc).__name__}))

@app.on_event("startup")
async def start_postal_retry_worker():
    if os.getenv("KLYROW_EMBEDDED_WORKERS","true").lower()=="true":
        asyncio.create_task(postal_retry_loop())
        asyncio.create_task(email_outbox_loop())

def bounded_metric_path(request:Request)->str:
    """Return the reviewed route template, never a raw URL or identifier."""
    route=getattr(request.scope.get("route"),"path",None)
    if isinstance(route,str) and route.startswith("/"):
        return route
    # Starlette has not attached a route for an unmatched request.  Keeping a
    # single bounded bucket prevents tenant/message IDs and query material
    # from becoming Prometheus label values.
    return "/__unmatched__"

@app.middleware("http")
async def headers(request, call_next):
    started=time.monotonic()
    def identifier(value):
        return value if value and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) else str(uuid.uuid4())
    request_id=identifier(request.headers.get("X-Request-Id"))
    correlation_id=identifier(request.headers.get("X-Correlation-Id"))
    request.state.request_id=request_id
    request.state.correlation_id=correlation_id
    status="500"
    try:
        response=await call_next(request)
        status=str(response.status_code)
        response.headers.update({"X-Request-Id":request_id,"X-Correlation-Id":correlation_id,"X-Content-Type-Options":"nosniff","X-Frame-Options":"DENY","Referrer-Policy":"no-referrer","Permissions-Policy":"camera=(), microphone=(), geolocation=()","Content-Security-Policy":"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'"})
        return response
    finally:
        # Registered route templates bound cardinality and exclude identifiers.
        # A metrics client failure must never turn accepted mail into HTTP 500.
        try:
            metric_path=bounded_metric_path(request)
            REQUESTS.labels(metric_path,status).inc()
            LATENCY.labels(metric_path).observe(time.monotonic()-started)
        except Exception:
            pass

@app.get("/v1/health")
def health(s:Session=Depends(db)):
    s.execute(select(1));active=s.scalar(select(func.count()).select_from(EmailOutbox).where(EmailOutbox.state.in_(("pending","sending","retry"))))
    activation = email_activation_status()
    return {"status":"ok","safe_mode":SAFE_MODE,"production_gate_approved":activation["controls"]["KLYROW_PRODUCTION_GATE_APPROVED"] is True,"production_gate_open":production_gate_open(s),"database":"healthy","outbox":"healthy","outbox_active":active,"email_activation":activation}

@app.get("/v1/admin/delivery/activation", tags=["Delivery controls"])
def delivery_activation(ctx=Depends(require("platform_admin"))):
    """Read general-email configuration; this route never enables or sends mail."""
    return email_activation_status()
@app.get("/health")
def health_alias(s:Session=Depends(db)):
    """Stable orchestrator health alias with no credential or recipient data."""
    return health(s)
@app.get("/healthz")
def healthz(s:Session=Depends(db)):s.execute(select(1));return {"status":"ok"}
@app.get("/readyz")
def readyz(s:Session=Depends(db)):
    s.execute(select(1))
    if not keyring_ready():raise HTTPException(503,"durable_result_keys_unavailable")
    return {"status":"ready","safe_mode":SAFE_MODE}
@app.get("/readiness")
def readiness_alias(s:Session=Depends(db)):
    result=readyz(s)
    result.update({
        "postal_configured":bool(os.getenv("KLYROW_POSTAL_API_URL") and os.getenv("KLYROW_POSTAL_API_KEY_FILE")),
        "source_revision_configured":os.getenv("KLYROW_RELEASE_SHA","unknown")!="unknown",
    })
    return result
@app.get("/dependencies")
def dependencies(s:Session=Depends(db)):
    s.execute(select(1))
    return {
        "status":"ok",
        "database":"configured",
        "durable_result_keys":"configured" if keyring_ready() else "unavailable",
        "postal_api":"configured" if os.getenv("KLYROW_POSTAL_API_URL") and os.getenv("KLYROW_POSTAL_API_KEY_FILE") else "unconfigured",
        "middleware_callback":"configured" if os.getenv("KLYROW_MIDDLEWARE_CALLBACK_URL") else "unconfigured",
        "secrets":"file_only" if os.getenv("KLYROW_ENV","development").lower()=="production" else "development",
    }
@app.get("/capabilities")
def capabilities():
    activation = email_activation_status()
    return {
        "service":"klyrow-gateway",
        "commands":sorted(MIDDLEWARE_ALLOWED_COMMANDS),
        "events":sorted({value for value in SMTP_EVENT_MAP.values()}),
        "external_delivery_enabled":not SAFE_MODE and activation["live_delivery_enabled"],
        "email_activation":activation,
        "provider":"postal",
    }
@app.get("/version")
def version():
    return {
        "service":"klyrow-gateway",
        "version":os.getenv("KLYROW_RELEASE_VERSION","development"),
        "revision":os.getenv("KLYROW_RELEASE_SHA","unknown"),
    }
@app.get("/metrics")
def metrics(authorization:str=Header(default="")):
    token_file=os.getenv("KLYROW_METRICS_TOKEN_FILE","")
    try:expected=Path(token_file).read_text(encoding="utf-8").strip()
    except OSError:raise HTTPException(404,"not_found")
    supplied=authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
    if not expected or not hmac.compare_digest(supplied,expected):raise HTTPException(404,"not_found")
    from .provider import ProviderEvent, ProviderMessage, ProviderUsageEvent
    from .billing import Invoice,Payment,Refund,expected_invoice_status,money
    from .messaging import DomainClaim,WebhookAttempt
    from .operations import IntegrationOutbox
    with DB() as s:
        for status in ("QUEUED","PROCESSING","DEFERRED","FAILED","DEAD_LETTER"):
            PROVIDER_QUEUE.labels(status).set(s.scalar(select(func.count()).select_from(ProviderMessage).where(ProviderMessage.status==status)) or 0)
        for state in ("PENDING","PROCESSING","RETRY","DELIVERED","SKIPPED","DEAD_LETTER"):
            PROVIDER_EVENTS.labels(state).set(s.scalar(select(func.count()).select_from(ProviderEvent).where(ProviderEvent.state==state)) or 0)
        for state in ("PENDING","PROCESSING","RETRY","DELIVERED","DEAD_LETTER"):
            PROVIDER_USAGE.labels(state).set(s.scalar(select(func.count()).select_from(ProviderUsageEvent).where(ProviderUsageEvent.state==state)) or 0)
        active=s.scalars(select(EmailOutbox).where(EmailOutbox.state.in_(("pending","sending","retry")))).all()
        ages=[]
        for item in active:
            created=item.created_at if item.created_at.tzinfo else item.created_at.replace(tzinfo=timezone.utc)
            ages.append(max(0,(datetime.now(timezone.utc)-created).total_seconds()))
        OUTBOX_OLDEST.set(max(ages,default=0))
        for target in ("N8N","ODOO"):
            for state in ("PENDING","RETRY","COMPLETED","DEAD_LETTER"):
                INTEGRATION_QUEUE.labels(target,state).set(s.scalar(select(func.count()).select_from(IntegrationOutbox).where(IntegrationOutbox.target==target,IntegrationOutbox.state==state)) or 0)
        for state in ("PENDING","RETRY","DELIVERED","DEAD_LETTER"):
            WEBHOOK_QUEUE.labels(state).set(s.scalar(select(func.count()).select_from(WebhookAttempt).where(WebhookAttempt.state==state)) or 0)
        total=s.scalar(select(func.count()).select_from(Message)) or 0
        for outcome,kinds in {"delivered":("email.delivered","klyrow.email.delivered"),"bounced":("email.bounced","klyrow.email.bounced"),"complained":("email.complained","klyrow.email.complained")}.items():
            count=s.scalar(select(func.count()).select_from(Event).where(Event.kind.in_(kinds))) or 0
            DELIVERY_RATE.labels(outcome).set(count/total if total else 0)
        DNS_INVALID.set(s.scalar(select(func.count()).select_from(DomainClaim).where(DomainClaim.state.notin_(("VERIFIED","SENDING_ENABLED")))) or 0)
        drift=0
        for invoice in s.scalars(select(Invoice)).all():
            paid=money(s.scalar(select(func.sum(Payment.amount)).where(Payment.invoice_id==invoice.id,Payment.status=="CONFIRMED")) or 0)
            refunded=money(s.scalar(select(func.sum(Refund.amount)).where(Refund.payment_id.in_(select(Payment.id).where(Payment.invoice_id==invoice.id)),Refund.status=="CONFIRMED")) or 0)
            expected=expected_invoice_status(invoice.status,invoice.total,paid,refunded)
            drift+=int(invoice.status!=expected)
        BILLING_DRIFT.set(drift)
    from fastapi.responses import Response; return Response(generate_latest(),media_type="text/plain")
@app.post("/v1/auth/login")
def login(x:Login,request:Request,s:Session=Depends(db)):
    if os.getenv("KLYROW_ENV","development").lower()=="production" or os.getenv("KLYROW_LOCAL_AUTH_ENABLED","true").lower()!="true":raise HTTPException(410,"use_canonical_oidc")
    auth_rate(request,"login")
    u=s.scalar(select(User).where(User.email==x.email.lower()))
    try: valid=u and u.enabled and ph.verify(u.password_hash,x.password)
    except Exception: valid=False
    if not valid: raise HTTPException(401,"invalid_credentials")
    from .saas import MfaConfig,verify_totp
    m=s.get(MfaConfig,u.id)
    if m and m.enabled and (not x.otp or not verify_totp(m.secret,x.otp)):raise HTTPException(401,"mfa_required")
    audit(s,{"tenant":u.tenant_id,"sub":u.id},"session.login");s.commit();return {"access_token":token(u,s),"token_type":"bearer","role":u.role,"tenant_id":u.tenant_id}
@app.post("/v1/auth/logout",status_code=204)
def logout(ctx=Depends(auth),s:Session=Depends(db)):
    from .saas import SessionRecord
    if ctx.get("sid"):
        session=s.get(SessionRecord,ctx["sid"])
        if session:session.revoked=True;audit(s,ctx,"session.logout");s.commit()
    return None
@app.post("/v1/auth/forgot-password",status_code=202)
def forgot(x:ResetRequest,request:Request,s:Session=Depends(db)):
    auth_rate(request,"forgot-password")
    u=s.scalar(select(User).where(User.email==x.email.lower()))
    if u: raw=secrets.token_urlsafe(32); u.reset_hash=sha(raw); u.reset_expires=datetime.now(timezone.utc)+timedelta(minutes=30);audit(s,{"tenant":u.tenant_id,"sub":u.id},"password.reset.requested");s.commit()
    return {"detail":"If the account exists, reset instructions will be delivered."}
@app.post("/v1/auth/reset-password")
def reset(x:Reset,s:Session=Depends(db)):
    u=s.scalar(select(User).where(User.reset_hash==sha(x.token)))
    if not u or not u.reset_expires or u.reset_expires.replace(tzinfo=timezone.utc)<datetime.now(timezone.utc): raise HTTPException(400,"invalid_or_expired_token")
    u.password_hash=ph.hash(x.password); u.reset_hash=None; u.reset_expires=None
    from .saas import SessionRecord
    for session in s.scalars(select(SessionRecord).where(SessionRecord.user_id==u.id,SessionRecord.revoked==False)).all():session.revoked=True
    audit(s,{"tenant":u.tenant_id,"sub":u.id},"password.reset.sessions_revoked");s.commit(); return {"status":"reset"}
@app.get("/v1/me")
def me(ctx=Depends(auth)): return ctx
@app.post("/v1/api-keys")
def create_key(x:KeyIn,ctx=Depends(require("platform_admin","tenant_admin")),s:Session=Depends(db)):
    from .secret_responses import record_secret_response,response_metadata
    raw="kly_"+secrets.token_urlsafe(32); k=ApiKey(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],name=x.name,key_hash=sha(raw)); s.add(k);result={"id":k.id,"key":raw,"name":k.name};secret_response=record_secret_response(s,tenant_id=ctx["tenant"],resource_type="API_KEY",resource_id=k.id,action="CREATE",payload=result,actor=ctx["sub"]);audit(s,ctx,"api_key.created");s.commit();return {**result,**response_metadata(secret_response)}
@app.post("/v1/api-keys/{kid}/rotate")
def rotate_key(kid:str,ctx=Depends(require("platform_admin","tenant_admin")),s:Session=Depends(db)):
    from .secret_responses import record_secret_response,response_metadata
    k=s.scalar(select(ApiKey).where(ApiKey.id==kid,ApiKey.tenant_id==ctx["tenant"],ApiKey.revoked==False))
    if not k:raise HTTPException(404,"not_found")
    raw="kly_"+secrets.token_urlsafe(32);k.key_hash=sha(raw);k.rotated_at=datetime.now(timezone.utc);result={"id":k.id,"key":raw,"name":k.name};secret_response=record_secret_response(s,tenant_id=ctx["tenant"],resource_type="API_KEY",resource_id=k.id,action="ROTATE",payload=result,actor=ctx["sub"]);audit(s,ctx,"api_key.rotated");s.commit();return {**result,**response_metadata(secret_response)}
@app.delete("/v1/api-keys/{kid}",status_code=204)
def revoke(kid:str,ctx=Depends(require("platform_admin","tenant_admin")),s:Session=Depends(db)):
    k=s.scalar(select(ApiKey).where(ApiKey.id==kid,ApiKey.tenant_id==ctx["tenant"]));
    if not k: raise HTTPException(404,"not_found")
    k.revoked=True; audit(s,ctx,"api_key.revoked"); s.commit()
@app.get("/v1/domains")
def domains(ctx=Depends(auth),s:Session=Depends(db)): return s.scalars(select(Domain).where(Domain.tenant_id==ctx["tenant"])).all()
@app.post("/v1/domains")
def domain_add(x:DomainIn,ctx=Depends(require("platform_admin","tenant_admin")),s:Session=Depends(db)):
    d=Domain(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],domain=x.domain.lower(),token=secrets.token_urlsafe(24)); s.add(d); s.commit(); return {"id":d.id,"domain":d.domain,"verified":False,"dns":{"type":"TXT","name":"_klyrow-verification."+d.domain,"value":"klyrow="+d.token}}
@app.post("/v1/domains/{did}/verify")
def domain_verify(did:str,ctx=Depends(require("platform_admin","tenant_admin")),s:Session=Depends(db)):
    from .business_events import enqueue_named_event
    import socket
    d=s.scalar(select(Domain).where(Domain.id==did,Domain.tenant_id==ctx["tenant"]));
    if not d: raise HTTPException(404,"not_found")
    try:
        import dns.resolver; values=[str(r).strip('"') for r in dns.resolver.resolve("_klyrow-verification."+d.domain,"TXT")]; d.verified=("klyrow="+d.token) in values
    except Exception: d.verified=False
    enqueue_named_event(s,event_type="klyrow.domain.status",tenant_id=ctx["tenant"],aggregate_id=d.id,causation_id=did,data={"domain_id":d.id,"domain":d.domain,"status":"VERIFIED" if d.verified else "DNS_REQUIRED","verified_at":datetime.now(timezone.utc) if d.verified else None});s.commit();return {"verified":d.verified}
@app.post("/v1/messages",status_code=202)
@app.post("/v1/email/send",status_code=202,include_in_schema=False)
async def send(x:MailIn,ctx=Depends(auth),s:Session=Depends(db),idempotency_key:Optional[str]=Header(default=None)):
    return await _send(x,ctx,s,idempotency_key)

@app.post("/v1/internal/email/beyvra/send",status_code=202)
async def beyvra_send(x:MailIn,ctx=Depends(beyvra_service_auth),s:Session=Depends(db),idempotency_key:Optional[str]=Header(default=None)):
    if x.stream!="transactional":raise HTTPException(403,"transactional_only")
    allowed={"no-reply@beyvra.com","security@beyvra.com","trading@beyvra.com","statements@beyvra.com","support@beyvra.com"}
    if x.sender.lower() not in allowed:raise HTTPException(403,"sender_spoofing_denied")
    return await _send(x,ctx,s,idempotency_key)

def queue_email_lifecycle_event(s:Session, *, kind:str, tenant_id:str, message_id:str,
                                operation_id:str, correlation_id:str,
                                provider_message_id:Optional[str]=None,
                                recipient:Optional[str]=None, attempt:int=1)->str:
    """Persist one sanitized Middleware callback event before async delivery."""
    from .provider import ProviderEvent
    event_id=str(uuid.uuid5(uuid.NAMESPACE_URL,f"klyrow:{kind}:{message_id}:{attempt}"))
    if s.get(ProviderEvent,event_id):return event_id
    occurred_at=datetime.now(timezone.utc).isoformat()
    payload={"event_id":event_id,"schema_version":"1.0","tenant_id":tenant_id,
        "operation_id":operation_id,"correlation_id":correlation_id,
        "provider_message_id":provider_message_id or message_id,"message_id":message_id,
        "event_type":kind,"occurred_at":occurred_at,"status":kind.rsplit(".",1)[-1],
        "provider":"postal","attempt":attempt,
        "recipient_reference":"sha256:"+hashlib.sha256((recipient or "").lower().encode()).hexdigest()}
    payload["payload_hash"]=hashlib.sha256(json.dumps(payload,separators=(",",":"),sort_keys=True).encode()).hexdigest()
    s.add(ProviderEvent(id=event_id,tenant_id=tenant_id,message_id=message_id,kind=kind,
        payload_json=json.dumps(payload,separators=(",",":"),sort_keys=True)))
    if os.getenv("KLYROW_BUSINESS_EVENTS_ENABLED", "false").lower() == "true":
        from .business_events import EventEnvelope, KLYROW_EVENTS, enqueue_event
        event_type = "klyrow." + kind
        if event_type in KLYROW_EVENTS:
            enqueue_event(s, EventEnvelope(
                id=event_id, type=event_type, version=1, source="klyrow", tenant_id=tenant_id,
                correlation_id=correlation_id, causation_id=operation_id,
                occurred_at=occurred_at, data={"message_id": message_id},
            ))
    return event_id


async def _send(x:MailIn,ctx,s,idempotency_key):
    if not idempotency_key: raise HTTPException(400,"idempotency_key_required")
    resource="messages"
    storage_key=scoped_idempotency_key(ctx,idempotency_key,action="message.send",resource=resource)
    request_hash=semantic_request_hash(action="message.send",resource=resource,payload=x.model_dump(mode="json"))
    prior=s.scalar(select(Idempotency).where(Idempotency.key==storage_key,Idempotency.tenant_id==ctx["tenant"]))
    if prior is None:
        legacy=s.scalar(select(Idempotency).where(Idempotency.key==idempotency_key,Idempotency.tenant_id==ctx["tenant"]))
        if legacy:
            if legacy.request_hash!=sha(x.model_dump_json()):raise HTTPException(409,"idempotency_key_payload_mismatch")
            return read_control_response(legacy)
    if prior:
        if prior.request_hash!=request_hash:raise HTTPException(409,"idempotency_key_payload_mismatch")
        return read_control_response(prior)
    from .operations import enforce_tenant_send_gate
    enforce_tenant_send_gate(s,ctx["tenant"])
    from .agent_mailboxes import authorize_agent_sender
    authorize_agent_sender(s,ctx,x.sender,x.campaign_id,x.reply_to)
    from .guards import authorize_send
    authorization=authorize_send(s,tenant_id=ctx["tenant"],sender=str(x.sender),recipient=str(x.to),stream=x.stream,sandbox=SAFE_MODE,campaign_id=x.campaign_id,topic=x.topic)
    enforce_production_canary(x,s)
    if x.stream=="marketing" and (x.campaign_id or not SAFE_MODE):enforce_campaign_canary(x,ctx,s)
    sender=x.sender.lower();domain=sender.rsplit("@",1)[1]
    from .delivery_controls import enforce_delivery_controls
    enforce_delivery_controls(s,ctx["tenant"],sender,x.stream)
    allowed=s.scalar(select(Domain).where(Domain.tenant_id==ctx["tenant"],Domain.domain==domain,Domain.verified==True))
    if not allowed: raise HTTPException(422,"sender_domain_not_verified")
    if ctx.get("role")!="codestra-email-agent":
        exact=s.scalar(select(AllowedSender).where(AllowedSender.tenant_id==ctx["tenant"],AllowedSender.address==sender,AllowedSender.enabled==True))
        if not exact:raise HTTPException(403,"sender_address_not_allowed")
    since=datetime.now(timezone.utc)-timedelta(days=1); count=len(s.scalars(select(Message).where(Message.tenant_id==ctx["tenant"],Message.created_at>=since)).all()); tenant=s.get(Tenant,ctx["tenant"])
    if count>=tenant.quota: raise HTTPException(429,"daily_quota_exceeded")
    from .billing import UsageEvent
    from .guards import billing_identity
    subscription_id,price_id=billing_identity(s,ctx["tenant"],sandbox=SAFE_MODE)
    mid=str(uuid.uuid4()); status="accepted" if SAFE_MODE else "queued"
    operation_id=str(x.callback_metadata.get("operation_id") or mid);correlation_id=str(x.callback_metadata.get("correlation_id") or mid)
    result={"id":mid,"provider_message_id":mid,"status":status,"safe_mode":SAFE_MODE,"stream":x.stream}; s.add(Message(id=mid,tenant_id=ctx["tenant"],recipient=authorization["recipient"],sender=authorization["sender"],subject=x.subject,status=status)); s.add(Event(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],message_id=mid,kind="klyrow.email.accepted",payload=json.dumps({"stream":x.stream})));queue_email_lifecycle_event(s,kind="email.accepted",tenant_id=ctx["tenant"],message_id=mid,operation_id=operation_id,correlation_id=correlation_id,recipient=x.to.lower());s.add(Idempotency(key=storage_key,tenant_id=ctx["tenant"],request_hash=request_hash,resource_id=mid,response_json=seal_control_response(result,tenant_id=ctx["tenant"],storage_key=storage_key,request_hash=request_hash,resource_id=mid)));s.add(UsageEvent(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],subscription_id=subscription_id,message_id=mid,event_key="accepted:api:"+storage_key,unit="accepted_message",quantity=1,price_id=price_id))
    if not SAFE_MODE:
        from .preferences import one_click_unsubscribe_headers
        delivery_payload={"to":[str(x.to)],"from":str(x.sender),"subject":x.subject,"html_body":x.html,"plain_body":x.text,"campaign_id":x.campaign_id,"stream":x.stream}
        delivery_headers=dict(x.headers)
        if x.stream=="marketing":delivery_headers.update(one_click_unsubscribe_headers(ctx["tenant"],str(x.to)))
        if delivery_headers:delivery_payload["headers"]=delivery_headers
        from .guards import stream_priority
        s.add(EmailOutbox(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],message_id=mid,operation_id=operation_id,correlation_id=correlation_id,payload=json.dumps(delivery_payload,separators=(",",":"),sort_keys=True),priority=stream_priority(x.stream)))
        queue_email_lifecycle_event(s,kind="email.queued",tenant_id=ctx["tenant"],message_id=mid,operation_id=operation_id,correlation_id=correlation_id,recipient=x.to.lower())
    s.commit(); MAIL.labels("queued").inc(); return result

def command_payload(x:MiddlewareCommandIn)->dict:
    payload=dict(x.payload or {})
    if "from" in payload and "sender" not in payload:payload["sender"]=payload["from"]
    return payload

def command_request_hash(x:MiddlewareCommandIn)->str:
    return sha(json.dumps({"command":x.command,"payload":x.payload,"target":x.target,"tenant_id":x.tenant_id},separators=(",",":"),sort_keys=True))

def command_response(item:MiddlewareCommandOperation)->dict:
    return {"command_id":item.command_id,"tenant_id":item.tenant_id,"command":item.command,"state":item.state,"correlation_id":item.correlation_id,"result":json.loads(item.result_json or "{}"),"error":item.error}

def reconcile_email_command_operation(item:MiddlewareCommandOperation,s:Session)->MiddlewareCommandOperation:
    """Project the durable message/outbox outcome onto command read-back."""
    if item.command!="email.message.send.v1":return item
    try:message_id=str(json.loads(item.result_json or "{}").get("id") or "")
    except (TypeError,ValueError):message_id=""
    if not message_id:return item
    message=s.scalar(select(Message).where(Message.id==message_id,Message.tenant_id==item.tenant_id))
    outbox=s.scalar(select(EmailOutbox).where(EmailOutbox.message_id==message_id,EmailOutbox.tenant_id==item.tenant_id))
    message_state=str(message.status if message else "").lower()
    outbox_state=str(outbox.state if outbox else "").lower()
    state={
        "accepted":"accepted","queued":"queued","submitted":"submitted",
        "provider_accepted":"submitted","delivered":"delivered","deferred":"deferred",
        "bounced":"bounced","complained":"complained","suppressed":"rejected",
        "failed":"failed","cancelled":"cancelled","indeterminate":"unknown_outcome",
    }.get(message_state,item.state)
    if outbox_state=="sending":state="processing"
    elif outbox_state=="indeterminate":state="unknown_outcome"
    elif outbox_state=="failed" and state not in {"delivered","bounced","complained","cancelled"}:state="failed"
    elif outbox_state=="cancelled":state="cancelled"
    if item.state!=state:
        item.state=state;item.updated_at=datetime.now(timezone.utc);s.commit()
    return item

def ensure_command_tenant(ctx:dict,tenant_id:str):
    if not tenant_id or tenant_id!=ctx["tenant"]:raise HTTPException(403,"tenant_mismatch")

async def execute_middleware_command(command_id:str, *, raise_errors:bool=False)->dict:
    """Claim and execute one durably recorded Middleware command exactly once."""
    with DB() as s:
        item=s.scalar(select(MiddlewareCommandOperation).where(
            MiddlewareCommandOperation.command_id==command_id,
        ).with_for_update())
        if not item:raise RuntimeError("middleware_command_missing")
        if item.state!="accepted":return command_response(reconcile_email_command_operation(item,s))
        item.state="processing";item.updated_at=datetime.now(timezone.utc);s.commit()
        tenant_id=item.tenant_id;command=item.command;idempotency_key=item.idempotency_key
        correlation_id=item.correlation_id
        try:
            recorded=json.loads(item.request_json or "{}")
            payload=recorded["payload"]
            if not isinstance(payload,dict):raise ValueError("invalid payload")
        except (KeyError,TypeError,ValueError):
            item.state="failed";item.error="invalid_command_payload";item.updated_at=datetime.now(timezone.utc);s.commit()
            if raise_errors:raise HTTPException(422,"invalid_command_payload")
            return command_response(item)
        ctx={"tenant":tenant_id,"sub":"middleware-service","service":True,
            "scopes":{"klyrow.middleware.command.write"}}
        try:
            if command=="email.message.send.v1":
                mail=MailIn(**payload);mail.callback_metadata={**mail.callback_metadata,
                    "operation_id":item.command_id,"correlation_id":correlation_id}
                result=await _send(mail,ctx,s,idempotency_key);item.state="queued";item.result_json=json.dumps(result,separators=(",",":"),sort_keys=True)
            elif command=="email.message.cancel.v1":
                message_id=str(payload.get("message_id") or "");message=s.scalar(select(Message).where(Message.id==message_id,Message.tenant_id==tenant_id).with_for_update())
                if not message:raise HTTPException(404,"not_found")
                outbox=s.scalar(select(EmailOutbox).where(EmailOutbox.message_id==message.id,EmailOutbox.tenant_id==tenant_id).with_for_update())
                if message.status=="cancelled" and (not outbox or outbox.state=="cancelled"):
                    item.state="cancelled";item.result_json=json.dumps({"message_id":message.id,"status":"cancelled","outbox_state":outbox.state if outbox else None})
                else:
                    if message.status in {"delivered","bounced","complained","failed"}:raise HTTPException(409,"terminal_message_cannot_cancel")
                    if outbox and outbox.state not in {"pending","retry"}:raise HTTPException(409,"provider_submission_requires_reconciliation")
                    if not outbox and message.status!="accepted":raise HTTPException(409,"message_not_cancellable")
                    if outbox:outbox.state="cancelled";outbox.last_error="cancelled_by_middleware_command";outbox.updated_at=datetime.now(timezone.utc)
                    set_core_message_status(message,"cancelled");item.state="cancelled";item.result_json=json.dumps({"message_id":message.id,"status":"cancelled","outbox_state":outbox.state if outbox else None});queue_email_lifecycle_event(s,kind="email.cancelled",tenant_id=tenant_id,message_id=message.id,operation_id=item.command_id,correlation_id=correlation_id,provider_message_id=outbox.provider_message_id if outbox else None,recipient=message.recipient)
            elif command=="email.domain.verify.v1":
                domain_id=str(payload.get("domain_id") or "");domain=s.scalar(select(Domain).where(Domain.id==domain_id,Domain.tenant_id==tenant_id))
                if not domain:raise HTTPException(404,"not_found")
                domain_verify(domain.id,ctx,s);item.state="completed";item.result_json=json.dumps({"domain_id":domain.id,"verified":domain.verified})
            elif command=="email.suppression.upsert.v1":
                email=str(payload.get("email") or payload.get("recipient") or "").lower();reason=str(payload.get("reason") or "policy")[:100]
                if not email or "@" not in email:raise HTTPException(422,"email_required")
                suppression=s.scalar(select(Suppression).where(Suppression.tenant_id==tenant_id,Suppression.email==email)) or Suppression(id=str(uuid.uuid4()),tenant_id=tenant_id,email=email,reason=reason)
                suppression.reason=reason;s.add(suppression);item.state="completed";item.result_json=json.dumps({"email":email,"reason":reason})
            elif command=="email.reputation.snapshot.request.v1":
                item.state="completed";item.result_json=json.dumps({"status":"accepted","safe_mode":SAFE_MODE})
            else:raise HTTPException(422,"unsupported_command")
            item.error=None;item.updated_at=datetime.now(timezone.utc);s.commit();return command_response(item)
        except HTTPException as exc:
            item.state="failed";item.error=str(exc.detail);item.updated_at=datetime.now(timezone.utc);s.commit()
            if raise_errors:raise
            return command_response(item)
        except ValidationError:
            item.state="failed";item.error="invalid_command_payload";item.updated_at=datetime.now(timezone.utc);s.commit()
            if raise_errors:raise HTTPException(422,"invalid_command_payload")
            return command_response(item)
        except Exception:
            s.rollback()
            failed=s.get(MiddlewareCommandOperation,command_id)
            if failed and failed.state=="processing":
                failed.state="accepted";failed.error="command_execution_interrupted";failed.updated_at=datetime.now(timezone.utc);s.commit()
            if raise_errors:raise
            return command_response(failed) if failed else {"command_id":command_id,"state":"accepted"}


async def recover_middleware_commands(limit:int=20)->int:
    """Reclaim interrupted commands; provider send replay remains idempotent."""
    cutoff=datetime.now(timezone.utc)-timedelta(minutes=5)
    with DB() as s:
        stale=list(s.scalars(select(MiddlewareCommandOperation).where(
            MiddlewareCommandOperation.state=="processing",
            MiddlewareCommandOperation.updated_at<cutoff,
        ).with_for_update(skip_locked=True).limit(limit)).all())
        for item in stale:
            item.state="accepted";item.error="command_execution_lease_expired";item.updated_at=datetime.now(timezone.utc)
        s.commit()
        command_ids=list(s.scalars(select(MiddlewareCommandOperation.command_id).where(
            MiddlewareCommandOperation.state=="accepted",
        ).order_by(MiddlewareCommandOperation.created_at).limit(limit)).all())
    for pending_id in command_ids:
        await execute_middleware_command(pending_id)
    return len(command_ids)


@app.post("/v1/commands",status_code=202)
async def middleware_command(x:MiddlewareCommandIn,ctx=Depends(auth),s:Session=Depends(db),idempotency_key:str=Header(alias="Idempotency-Key",min_length=8,max_length=200),x_tenant_id:str=Header(alias="X-Tenant-ID",min_length=1,max_length=120),x_correlation_id:str=Header(alias="X-Correlation-ID",min_length=8,max_length=200)):
    ensure_command_tenant(ctx,x_tenant_id);require_middleware_command_scope(ctx)
    if x.tenant_id and x.tenant_id!=x_tenant_id:raise HTTPException(403,"tenant_mismatch")
    if x.correlation_id and x.correlation_id!=x_correlation_id:raise HTTPException(409,"correlation_id_mismatch")
    if x.command not in MIDDLEWARE_ALLOWED_COMMANDS:raise HTTPException(422,"unsupported_command")
    request_hash=command_request_hash(x)
    prior=s.scalar(select(MiddlewareCommandOperation).where(MiddlewareCommandOperation.tenant_id==ctx["tenant"],MiddlewareCommandOperation.idempotency_key==idempotency_key))
    if prior:
        if prior.request_hash!=request_hash:raise HTTPException(409,"idempotency_key_payload_mismatch")
        return command_response(prior)
    item=MiddlewareCommandOperation(command_id=str(uuid.uuid4()),tenant_id=ctx["tenant"],command=x.command,idempotency_key=idempotency_key,correlation_id=x_correlation_id,request_hash=request_hash,request_json=json.dumps({"payload":command_payload(x),"target":x.target},separators=(",",":"),sort_keys=True),state="accepted")
    s.add(item)
    try:s.commit()
    except IntegrityError:
        s.rollback();prior=s.scalar(select(MiddlewareCommandOperation).where(MiddlewareCommandOperation.tenant_id==ctx["tenant"],MiddlewareCommandOperation.idempotency_key==idempotency_key))
        if not prior:raise
        if prior.request_hash!=request_hash:raise HTTPException(409,"idempotency_key_payload_mismatch")
        return command_response(reconcile_email_command_operation(prior,s))
    return await execute_middleware_command(item.command_id,raise_errors=True)

@app.get("/v1/operations/{command_id}")
def middleware_operation(command_id:str,ctx=Depends(auth),s:Session=Depends(db),x_tenant_id:str=Header(alias="X-Tenant-ID",min_length=1,max_length=120)):
    ensure_command_tenant(ctx,x_tenant_id)
    item=s.scalar(select(MiddlewareCommandOperation).where(MiddlewareCommandOperation.command_id==command_id,MiddlewareCommandOperation.tenant_id==ctx["tenant"]))
    if item:return command_response(reconcile_email_command_operation(item,s))
    from .operations import IntegrationOutbox
    from .production_api import _authorize_operation_read, _operation_json
    integration=s.scalar(select(IntegrationOutbox).where(IntegrationOutbox.id==command_id,IntegrationOutbox.tenant_id==ctx["tenant"]))
    if not integration:raise HTTPException(404,"not_found")
    _authorize_operation_read(ctx,integration)
    return _operation_json(integration,s)

@app.post("/v1/email/bulk",status_code=202)
async def bulk(x:BulkMailIn,ctx=Depends(auth),s:Session=Depends(db),idempotency_key:Optional[str]=Header(default=None)):
    if not SAFE_MODE:raise HTTPException(403,"bulk_delivery_disabled_during_canary")
    if not idempotency_key: raise HTTPException(400,"idempotency_key_required")
    results=[]
    for index,item in enumerate(x.messages): results.append(await send(item,ctx,s,f"{idempotency_key}:{index}"))
    return {"accepted":len(results),"messages":results}
@app.get("/v1/email/{mid}")
def message(mid:str,ctx=Depends(auth),s:Session=Depends(db)):
    m=s.scalar(select(Message).where(Message.id==mid,Message.tenant_id==ctx["tenant"]));
    if not m: raise HTTPException(404,"not_found")
    return m
@app.get("/v1/messages/{mid}")
def message_alias(mid:str,ctx=Depends(auth),s:Session=Depends(db)):
    return message(mid,ctx,s)
@app.get("/v1/messages")
def messages(ctx=Depends(auth),s:Session=Depends(db),status:Optional[str]=None,limit:int=50,offset:int=0):
    limit=max(1,min(limit,200));offset=max(0,offset);query=select(Message).where(Message.tenant_id==ctx["tenant"])
    if status:query=query.where(Message.status==status)
    return s.scalars(query.order_by(Message.created_at.desc()).offset(offset).limit(limit)).all()
@app.get("/v1/email/{mid}/events")
def events(mid:str,ctx=Depends(auth),s:Session=Depends(db)): return s.scalars(select(Event).where(Event.message_id==mid,Event.tenant_id==ctx["tenant"])).all()
@app.post("/v1/webhooks/postal",status_code=202)
async def postal_hook(request:Request,x_klyrow_timestamp:str=Header(),x_klyrow_event_id:str=Header(),x_klyrow_signature:str=Header(),s:Session=Depends(db)):
    body=await request.body(); secret=runtime_secret("KLYROW_WEBHOOK_SECRET").encode()
    try: ts=int(x_klyrow_timestamp)
    except: raise HTTPException(401,"invalid_timestamp")
    if abs(time.time()-ts)>300: raise HTTPException(401,"expired_signature")
    expected=hmac.new(secret,x_klyrow_timestamp.encode()+b"."+x_klyrow_event_id.encode()+b"."+body,hashlib.sha256).hexdigest()
    if not secret or not hmac.compare_digest(expected,x_klyrow_signature): raise HTTPException(401,"invalid_signature")
    if s.get(Replay,x_klyrow_event_id): return {"accepted":True,"duplicate":True}
    s.add(Replay(id=x_klyrow_event_id)); payload=json.loads(body); mid=str(payload.get("message_id") or ""); tenant=payload.get("tenant_id")
    supplied_event=str(payload.get("event") or "");local_event=supplied_event.removeprefix("klyrow.")
    event_type=SMTP_EVENT_MAP.get(local_event)
    if not event_type:raise HTTPException(422,"unsupported_postal_event")
    local_type=event_type.removeprefix("klyrow.")
    correlation=str(payload.get("correlation_id") or mid or x_klyrow_event_id)
    if tenant:persist_email_event(s,event_id=x_klyrow_event_id,tenant_id=tenant,message_id=mid,correlation_id=correlation,event_type=local_type,recipient=payload.get("recipient"),raw_status=payload.get("status"),payload=body.decode())
    else:s.commit()
    normalized={**payload,"event_id":x_klyrow_event_id,"customer_id":tenant,"canonical_status":TERMINAL_MESSAGE_STATUSES.get(local_type,local_type.rsplit(".",1)[-1])}
    delivery=s.get(PostalEvent,x_klyrow_event_id)
    if not delivery:
        delivery=PostalEvent(id=x_klyrow_event_id,event_type=local_type,correlation_id=correlation,message_id=mid,tenant_id=tenant,payload=json.dumps(normalized,separators=(",",":"),sort_keys=True),state="pending",attempts=0);s.add(delivery)
    delivery.attempts=(delivery.attempts or 0)+1;delivery.updated_at=datetime.now(timezone.utc);s.commit()
    if await emit_middleware(event_type,normalized):
        delivery=s.get(PostalEvent,x_klyrow_event_id);delivery.state="delivered";delivery.last_error=None;s.commit();return {"accepted":True,"duplicate":False}
    delivery=s.get(PostalEvent,x_klyrow_event_id);delivery.state="dlq" if delivery.attempts>=5 else "retry";delivery.last_error="middleware_delivery_failed";s.commit();raise HTTPException(503,"middleware_delivery_pending")

@app.get("/v1/contacts")
def contacts(ctx=Depends(auth),s:Session=Depends(db)): return s.scalars(select(Contact).where(Contact.tenant_id==ctx["tenant"])).all()
@app.post("/v1/contacts")
def contact_upsert(x:ContactIn,ctx=Depends(require("platform_admin","tenant_admin")),s:Session=Depends(db)):
    item=s.scalar(select(Contact).where(Contact.tenant_id==ctx["tenant"],Contact.email==x.email.lower())) or Contact(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],email=x.email.lower()); item.name=x.name; item.subscribed=x.subscribed; item.metadata_json=json.dumps(x.metadata); s.add(item); audit(s,ctx,"contact.upserted"); s.commit(); return item
@app.get("/v1/campaigns")
def campaigns(ctx=Depends(auth),s:Session=Depends(db)): return s.scalars(select(Campaign).where(Campaign.tenant_id==ctx["tenant"])).all()
@app.post("/v1/campaigns",status_code=201)
async def campaign_create(x:CampaignIn,ctx=Depends(require("platform_admin","tenant_admin")),s:Session=Depends(db),idempotency_key:Optional[str]=Header(default=None)):
    if not SAFE_MODE:raise HTTPException(403,"campaign_delivery_disabled_during_canary")
    if not idempotency_key: raise HTTPException(400,"idempotency_key_required")
    request_hash=sha(x.model_dump_json())
    prior=s.scalar(select(Idempotency).where(Idempotency.key==idempotency_key,Idempotency.tenant_id==ctx["tenant"]));
    if prior:
        if prior.request_hash!=request_hash:raise HTTPException(409,"idempotency_key_payload_mismatch")
        return read_control_response(prior)
    c=Campaign(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],name=x.name,subject=x.subject); result={"id":c.id,"name":c.name,"status":c.status}; s.add(c); s.add(Idempotency(key=idempotency_key,tenant_id=ctx["tenant"],request_hash=request_hash,resource_id=c.id,response_json=seal_control_response(result,tenant_id=ctx["tenant"],storage_key=idempotency_key,request_hash=request_hash,resource_id=c.id))); audit(s,ctx,"campaign.created"); s.commit(); return result
@app.get("/v1/campaigns/{cid}")
def campaign_get(cid:str,ctx=Depends(auth),s:Session=Depends(db)):
    c=s.scalar(select(Campaign).where(Campaign.id==cid,Campaign.tenant_id==ctx["tenant"]));
    if not c:raise HTTPException(404,"not_found")
    return c
@app.get("/v1/suppressions")
def suppressions(ctx=Depends(auth),s:Session=Depends(db)): return s.scalars(select(Suppression).where(Suppression.tenant_id==ctx["tenant"])).all()
@app.get("/v1/audit")
def audits(ctx=Depends(require("platform_admin","tenant_admin")),s:Session=Depends(db)): return s.scalars(select(Audit).where(Audit.tenant_id==ctx["tenant"]).order_by(Audit.created_at.desc()).limit(200)).all()
@app.get("/v1/usage")
def usage(ctx=Depends(auth),s:Session=Depends(db)): return {"sent_24h":len(s.scalars(select(Message).where(Message.tenant_id==ctx["tenant"],Message.created_at>=datetime.now(timezone.utc)-timedelta(days=1))).all()),"quota":s.get(Tenant,ctx["tenant"]).quota}
@app.post("/v1/webhooks")
def webhook_add(x:WebhookIn,ctx=Depends(require("platform_admin","tenant_admin")),s:Session=Depends(db)):
    from .secret_responses import record_secret_response,response_metadata
    url=safe_webhook_url(x.url);raw=secrets.token_urlsafe(32);item=WebhookEndpoint(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],url=url,secret_hash=sha(raw));s.add(item);result={"id":item.id,"url":item.url,"secret":raw};secret_response=record_secret_response(s,tenant_id=ctx["tenant"],resource_type="WEBHOOK_SECRET",resource_id=item.id,action="CREATE",payload=result,actor=ctx["sub"]);audit(s,ctx,"webhook.created");s.commit();return {**result,**response_metadata(secret_response)}
@app.post("/v1/webhooks/{wid}/rotate")
def webhook_rotate(wid:str,ctx=Depends(require("platform_admin","tenant_admin")),s:Session=Depends(db)):
    from .secret_responses import record_secret_response,response_metadata
    item=s.scalar(select(WebhookEndpoint).where(WebhookEndpoint.id==wid,WebhookEndpoint.tenant_id==ctx["tenant"],WebhookEndpoint.enabled==True))
    if not item:raise HTTPException(404,"not_found")
    raw=secrets.token_urlsafe(32);item.secret_hash=sha(raw);item.rotated_at=datetime.now(timezone.utc);result={"id":item.id,"secret":raw,"rotated_at":item.rotated_at.isoformat()};secret_response=record_secret_response(s,tenant_id=ctx["tenant"],resource_type="WEBHOOK_SECRET",resource_id=item.id,action="ROTATE",payload=result,actor=ctx["sub"]);audit(s,ctx,"webhook.rotated");s.commit();return {"id":item.id,"secret":raw,"rotated_at":item.rotated_at,**response_metadata(secret_response)}
@app.get("/v1/admin/tenants")
def admin_tenants(ctx=Depends(require("platform_admin")),s:Session=Depends(db)): return s.scalars(select(Tenant)).all()
@app.post("/v1/admin/tenants",status_code=201)
def admin_tenant_create(x:TenantIn,ctx=Depends(require("platform_admin")),s:Session=Depends(db)):
    from .business_events import enqueue_named_event
    item=Tenant(id=str(uuid.uuid4()),name=x.name,quota=x.quota);s.add(item);enqueue_named_event(s,event_type="klyrow.tenant.created",tenant_id=item.id,aggregate_id=item.id,data={"tenant_id":item.id,"name":item.name,"enabled":True});audit(s,ctx,"tenant.created");s.commit();return item
@app.post("/v1/admin/tenants/{tid}/suspend")
def admin_suspend(tid:str,ctx=Depends(require("platform_admin")),s:Session=Depends(db)):
    from .business_events import enqueue_named_event
    item=s.get(Tenant,tid)
    if not item:raise HTTPException(404,"not_found")
    item.enabled=False;enqueue_named_event(s,event_type="klyrow.tenant.updated",tenant_id=item.id,aggregate_id=item.id,data={"tenant_id":item.id,"name":item.name,"enabled":False});audit(s,ctx,"tenant.suspended");s.commit();return {"id":tid,"enabled":False}
@app.post("/v1/admin/tenants/{tid}/quota")
def admin_quota(tid:str,x:QuotaIn,ctx=Depends(require("platform_admin")),s:Session=Depends(db)):
    from .business_events import enqueue_named_event
    item=s.get(Tenant,tid)
    if not item:raise HTTPException(404,"not_found")
    item.quota=x.quota;enqueue_named_event(s,event_type="klyrow.tenant.updated",tenant_id=item.id,aggregate_id=item.id,data={"tenant_id":item.id,"name":item.name,"enabled":item.enabled});audit(s,ctx,"tenant.quota_changed");s.commit();return {"id":tid,"quota":item.quota}
@app.get("/",include_in_schema=False)
def auth_home(): return RedirectResponse("/login",status_code=302)
@app.get("/portal",response_class=HTMLResponse,include_in_schema=False)
def portal(): return Path(__file__).with_name("portal.html").read_text()
@app.get("/login",include_in_schema=False)
@app.get("/signup",include_in_schema=False)
@app.get("/verify-email",include_in_schema=False)
@app.get("/verification-expired",include_in_schema=False)
@app.get("/verification-success",include_in_schema=False)
@app.get("/forgot-password",include_in_schema=False)
@app.get("/reset-sent",include_in_schema=False)
@app.get("/reset-password",include_in_schema=False)
@app.get("/reset-expired",include_in_schema=False)
@app.get("/reset-success",include_in_schema=False)
@app.get("/invite",include_in_schema=False)
@app.get("/logged-out",include_in_schema=False)
@app.get("/service-error",include_in_schema=False)
@app.get("/account-disabled",include_in_schema=False)
def auth_page():
    index=AUTH_WEB_DIST/"index.html"
    if not index.exists(): raise HTTPException(503,"authentication_ui_not_built")
    return FileResponse(index,media_type="text/html",headers={"Cache-Control":"no-store"})
@app.get("/assets/portal.js",include_in_schema=False)
def portal_js():return FileResponse(Path(__file__).with_name("portal.js"),media_type="application/javascript")
@app.get("/admin",response_class=HTMLResponse,include_in_schema=False)
def admin_portal():return Path(__file__).with_name("admin.html").read_text()
@app.get("/assets/admin.js",include_in_schema=False)
def admin_js():return FileResponse(Path(__file__).with_name("admin.js"),media_type="application/javascript")

from .saas import router as saas_router
app.include_router(saas_router)
from .agent_mailboxes import router as agent_mailbox_router
app.include_router(agent_mailbox_router)
from .billing import router as billing_router
app.include_router(billing_router)
from .tenancy import router as tenancy_router
app.include_router(tenancy_router)
from .messaging import router as messaging_router
app.include_router(messaging_router)
from .operations import router as operations_router
app.include_router(operations_router)
from .observability import router as observability_router
app.include_router(observability_router)
from .production_api import router as production_api_router
app.include_router(production_api_router)
from .middleware_email import router as middleware_email_router
app.include_router(middleware_email_router)
from .middleware_sender_identities import router as middleware_sender_identities_router
app.include_router(middleware_sender_identities_router)
from .reseller import router as reseller_router
app.include_router(reseller_router)
from .platform_owner_api import router as platform_owner_api_router
app.include_router(platform_owner_api_router)
from .delivery_controls import router as delivery_controls_router
app.include_router(delivery_controls_router)
from .preferences import router as preferences_router
app.include_router(preferences_router)
from . import business_events as _business_events
from . import campaign_dispatcher as _campaign_dispatcher
from .secret_responses import router as secret_responses_router
app.include_router(secret_responses_router)
from . import webmail_models as _webmail_models
from .provider import provider_worker_loop, reconcile_legacy_registry, router as provider_router, status_router as provider_status_router
app.include_router(provider_router)
app.include_router(provider_status_router)

@app.on_event("startup")
def reconcile_provider_registry_on_startup():
    with DB() as s:
        reconcile_legacy_registry(s)
        from .webmail import sync_verified_mailboxes
        sync_verified_mailboxes(s)

@app.on_event("startup")
async def start_provider_worker():
    if os.getenv("KLYROW_EMBEDDED_WORKERS","true").lower()=="true":asyncio.create_task(provider_worker_loop())
