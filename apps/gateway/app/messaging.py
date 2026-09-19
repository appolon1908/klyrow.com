"""Tenant-safe email product primitives above Postal transport."""
import hashlib, hmac, html, json, re, secrets, uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .main import AllowedSender, Base, Domain, Event, Message, Suppression, Tenant, audit, auth, db, safe_webhook_url, sha
from .capabilities import require_permission

router=APIRouter(prefix="/v1",tags=["Email SaaS"])
now=lambda:datetime.now(timezone.utc)
STREAMS={"TRANSACTIONAL","MARKETING","SECURITY","SYSTEM","BULK"}

def resolve_txt(name:str)->list[str]:
    import dns.resolver
    return [b"".join(record.strings).decode() for record in dns.resolver.resolve(name,"TXT")]

class DomainClaim(Base):
    __tablename__="domain_claims"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); domain:Mapped[str]=mapped_column(String,unique=True,index=True); state:Mapped[str]=mapped_column(String,default="DNS_REQUIRED"); challenge_hash:Mapped[str]=mapped_column(String); dkim_selector:Mapped[str]=mapped_column(String); dkim_version:Mapped[int]=mapped_column(Integer,default=1); return_path:Mapped[str]=mapped_column(String); tracking_domain:Mapped[str]=mapped_column(String); verified_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True); suspended_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
class DkimKeyVersion(Base):
    __tablename__="dkim_key_versions"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); domain_claim_id:Mapped[str]=mapped_column(String,index=True); selector:Mapped[str]=mapped_column(String); version:Mapped[int]=mapped_column(Integer); public_key:Mapped[str]=mapped_column(Text); private_key_reference:Mapped[str]=mapped_column(String); active:Mapped[bool]=mapped_column(Boolean,default=True); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); retired_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True); __table_args__=(UniqueConstraint("domain_claim_id","version",name="uq_dkim_domain_version"),)
class SenderIdentity(Base):
    __tablename__="saas_sender_identities"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); domain_claim_id:Mapped[str]=mapped_column(String,index=True); address:Mapped[str]=mapped_column(String,index=True); display_name:Mapped[str]=mapped_column(String); reply_to:Mapped[Optional[str]]=mapped_column(String,nullable=True); stream:Mapped[str]=mapped_column(String); status:Mapped[str]=mapped_column(String,default="PENDING"); verified:Mapped[bool]=mapped_column(Boolean,default=False); __table_args__=(UniqueConstraint("tenant_id","address",name="uq_saas_sender_identity"),)
class MessageStream(Base):
    __tablename__="message_streams"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); name:Mapped[str]=mapped_column(String); kind:Mapped[str]=mapped_column(String); rate_limit:Mapped[int]=mapped_column(Integer); retention_days:Mapped[int]=mapped_column(Integer); tracking_enabled:Mapped[bool]=mapped_column(Boolean,default=False); suppression_policy:Mapped[str]=mapped_column(String); reputation_state:Mapped[str]=mapped_column(String,default="GOOD"); enabled:Mapped[bool]=mapped_column(Boolean,default=True); __table_args__=(UniqueConstraint("tenant_id","name",name="uq_message_stream"),)
class Template(Base):
    __tablename__="templates"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); slug:Mapped[str]=mapped_column(String); name:Mapped[str]=mapped_column(String); status:Mapped[str]=mapped_column(String,default="DRAFT"); current_version:Mapped[int]=mapped_column(Integer,default=1); locale:Mapped[str]=mapped_column(String,default="en"); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); __table_args__=(UniqueConstraint("tenant_id","slug",name="uq_template_slug"),)
class TemplateVersion(Base):
    __tablename__="template_versions"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); template_id:Mapped[str]=mapped_column(String,index=True); version:Mapped[int]=mapped_column(Integer); subject:Mapped[str]=mapped_column(String); html_body:Mapped[str]=mapped_column(Text); text_body:Mapped[str]=mapped_column(Text); variables_json:Mapped[str]=mapped_column(Text,default="[]"); created_by:Mapped[str]=mapped_column(String); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); __table_args__=(UniqueConstraint("template_id","version",name="uq_template_version"),)
class CampaignDefinition(Base):
    __tablename__="campaign_definitions"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); name:Mapped[str]=mapped_column(String); sender_id:Mapped[str]=mapped_column(String); template_id:Mapped[str]=mapped_column(String); segment_id:Mapped[Optional[str]]=mapped_column(String,nullable=True); status:Mapped[str]=mapped_column(String,default="DRAFT"); timezone:Mapped[str]=mapped_column(String,default="UTC"); scheduled_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True); frequency_cap:Mapped[int]=mapped_column(Integer,default=1); tracking_json:Mapped[str]=mapped_column(Text,default="{}"); test_sent_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
class InboundRoute(Base):
    __tablename__="inbound_routes"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); domain_claim_id:Mapped[str]=mapped_column(String,index=True); recipient:Mapped[str]=mapped_column(String,index=True); wildcard:Mapped[bool]=mapped_column(Boolean,default=False); destination_kind:Mapped[str]=mapped_column(String); destination_ref:Mapped[str]=mapped_column(String); enabled:Mapped[bool]=mapped_column(Boolean,default=False); max_bytes:Mapped[int]=mapped_column(Integer,default=26214400); malware_scan_required:Mapped[bool]=mapped_column(Boolean,default=True); __table_args__=(UniqueConstraint("domain_claim_id","recipient",name="uq_inbound_route_recipient"),)
class InboundMessage(Base):
    __tablename__="inbound_messages"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); route_id:Mapped[str]=mapped_column(String,index=True); message_id:Mapped[str]=mapped_column(String); sender:Mapped[str]=mapped_column(String); recipient:Mapped[str]=mapped_column(String); in_reply_to:Mapped[Optional[str]]=mapped_column(String,nullable=True); references_json:Mapped[str]=mapped_column(Text,default="[]"); headers_json:Mapped[str]=mapped_column(Text,default="{}"); attachment_manifest_json:Mapped[str]=mapped_column(Text,default="[]"); spam_score:Mapped[int]=mapped_column(Integer,default=0); malware_status:Mapped[str]=mapped_column(String,default="PENDING"); state:Mapped[str]=mapped_column(String,default="RECEIVED"); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); __table_args__=(UniqueConstraint("tenant_id","message_id",name="uq_inbound_message_id"),)
class WebhookSubscription(Base):
    __tablename__="webhook_subscriptions"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); url:Mapped[str]=mapped_column(String); events_json:Mapped[str]=mapped_column(Text); secret_hash:Mapped[str]=mapped_column(String); encrypted_secret_ref:Mapped[str]=mapped_column(String); enabled:Mapped[bool]=mapped_column(Boolean,default=True); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); rotated_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True)
class WebhookAttempt(Base):
    __tablename__="webhook_attempts"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); subscription_id:Mapped[str]=mapped_column(String,index=True); event_id:Mapped[str]=mapped_column(String,index=True); event_type:Mapped[str]=mapped_column(String); state:Mapped[str]=mapped_column(String,default="PENDING"); attempts:Mapped[int]=mapped_column(Integer,default=0); next_attempt_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); last_status:Mapped[Optional[int]]=mapped_column(Integer,nullable=True); last_error:Mapped[Optional[str]]=mapped_column(String,nullable=True); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); __table_args__=(UniqueConstraint("subscription_id","event_id",name="uq_webhook_attempt_event"),)
class DeliveryJob(Base):
    __tablename__="delivery_jobs"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); message_id:Mapped[str]=mapped_column(String,unique=True,index=True); state:Mapped[str]=mapped_column(String,default="QUEUED"); attempts:Mapped[int]=mapped_column(Integer,default=0); lease_owner:Mapped[Optional[str]]=mapped_column(String,nullable=True); lease_expires_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True); next_attempt_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); error_class:Mapped[Optional[str]]=mapped_column(String,nullable=True); dead_lettered_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True)
class ReputationSnapshot(Base):
    __tablename__="reputation_snapshots"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); domain_claim_id:Mapped[Optional[str]]=mapped_column(String,nullable=True); stream_id:Mapped[Optional[str]]=mapped_column(String,nullable=True); sent:Mapped[int]=mapped_column(Integer); delivered:Mapped[int]=mapped_column(Integer); hard_bounces:Mapped[int]=mapped_column(Integer); complaints:Mapped[int]=mapped_column(Integer); invalid:Mapped[int]=mapped_column(Integer); state:Mapped[str]=mapped_column(String); measured_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)

class DomainClaimIn(BaseModel):domain:str=Field(pattern=r"^[a-z0-9][a-z0-9.-]+$")
class SenderIn(BaseModel):domain_claim_id:str;email:EmailStr;display_name:str=Field(min_length=1,max_length=150);reply_to:Optional[EmailStr]=None;stream:str
class StreamIn(BaseModel):name:str=Field(min_length=2,max_length=80);kind:str;rate_limit:int=Field(gt=0,le=1000000);retention_days:int=Field(ge=1,le=3650);tracking_enabled:bool=False;suppression_policy:str="STANDARD"
class TemplateIn(BaseModel):slug:str=Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,79}$");name:str;locale:str="en";subject:str=Field(max_length=998);html_body:str=Field(max_length=500000);text_body:str=Field(max_length=500000);variables:list[str]=Field(default_factory=list,max_length=100)
class TemplateUpdate(BaseModel):subject:str=Field(max_length=998);html_body:str=Field(max_length=500000);text_body:str=Field(max_length=500000);variables:list[str]=Field(default_factory=list,max_length=100)
class RenderIn(BaseModel):variables:dict=Field(default_factory=dict)
class CampaignIn(BaseModel):name:str;sender_id:str;template_id:str;segment_id:Optional[str]=None;timezone:str="UTC";frequency_cap:int=Field(default=1,ge=1,le=100);tracking:dict=Field(default_factory=dict)
class PreflightIn(BaseModel):estimated_recipients:int=Field(ge=0);estimated_suppressed:int=Field(ge=0);estimated_invalid:int=Field(ge=0);quota_remaining:int=Field(ge=0);estimated_unit_cost:str="0"
class ScheduleIn(BaseModel):scheduled_at:datetime
class InboundRouteIn(BaseModel):domain_claim_id:str;recipient:EmailStr;wildcard:bool=False;destination_kind:str=Field(pattern="^(WEBHOOK|APPLICATION|ODOO|SUPPORT)$");destination_ref:str;max_bytes:int=Field(default=26214400,ge=1024,le=52428800)
class InboundFixture(BaseModel):recipient:EmailStr;sender:EmailStr;message_id:str=Field(min_length=3,max_length=998);in_reply_to:Optional[str]=None;references:list[str]=Field(default_factory=list,max_length=100);headers:dict=Field(default_factory=dict);attachments:list[dict]=Field(default_factory=list,max_length=50);size_bytes:int=Field(ge=0);spam_score:int=Field(default=0,ge=0,le=100);malware_status:str=Field(default="CLEAN",pattern="^(CLEAN|INFECTED|ERROR)$")
class WebhookIn(BaseModel):url:str;events:list[str]=Field(min_length=1,max_length=30)
class EventIn(BaseModel):event_id:str;event_type:str;payload:dict=Field(default_factory=dict)
class LeaseIn(BaseModel):worker_id:str;lease_seconds:int=Field(default=60,ge=5,le=600)
class FailIn(BaseModel):error_class:str=Field(pattern="^(NETWORK_FAILURE|PROVIDER_TEMPORARY|RATE_LIMIT|SOFT_BOUNCE|INVALID_RECIPIENT|HARD_BOUNCE|SUPPRESSED|POLICY_DENIAL)$")

def tenant_get(s,model,item_id,tenant):
    item=s.scalar(select(model).where(model.id==item_id,model.tenant_id==tenant))
    if not item:raise HTTPException(404,"not_found")
    return item
def validate_html(value):
    if re.search(r"<(script|iframe|object|embed)\b|on\w+\s*=|javascript:",value,re.I):raise HTTPException(422,"unsafe_template_content")
def render_version(version,variables):
    required=json.loads(version.variables_json);missing=[key for key in required if key not in variables]
    if missing:raise HTTPException(422,"missing_template_variables",headers={"X-Missing-Variables":",".join(missing)})
    def apply(value):
        for key in required:value=value.replace("{{"+key+"}}",html.escape(str(variables[key]),quote=True))
        return value
    return {"subject":apply(version.subject),"html":apply(version.html_body),"text":apply(version.text_body)}

@router.get("/domains/claims")
def domain_claims(ctx=Depends(auth),s:Session=Depends(db)):
    return s.scalars(select(DomainClaim).where(DomainClaim.tenant_id==ctx["tenant"]).order_by(DomainClaim.created_at.desc())).all()

@router.post("/domains/claims",status_code=201)
def domain_claim(x:DomainClaimIn,ctx=Depends(auth),s:Session=Depends(db)):
    require_permission(ctx, "domain.manage")
    name=x.domain.lower().rstrip(".")
    if s.scalar(select(DomainClaim).where(DomainClaim.domain==name)):raise HTTPException(409,"domain_already_claimed")
    challenge=secrets.token_urlsafe(24);selector="kly"+secrets.token_hex(4);item=DomainClaim(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],domain=name,challenge_hash=sha(challenge),dkim_selector=selector,return_path="bounce."+name,tracking_domain="track."+name);s.add(item);audit(s,ctx,"domain.claimed");s.commit();return {"id":item.id,"state":item.state,"dns":{"ownership":{"type":"TXT","name":"_klyrow-verification."+name,"value":"klyrow="+challenge},"spf":{"type":"TXT","name":name,"recommended":"v=spf1 include:spf.klyrow.com -all"},"dkim":{"selector":selector},"dmarc":{"type":"TXT","name":"_dmarc."+name,"recommended":"v=DMARC1; p=none; rua=mailto:dmarc@klyrow.com"},"return_path":item.return_path,"tracking":item.tracking_domain,"mx":"mail.klyrow.com"}}
@router.post("/domains/claims/{item_id}/verify")
def domain_verify(item_id:str,x:dict,ctx=Depends(auth),s:Session=Depends(db)):
    require_permission(ctx, "domain.manage")
    item=tenant_get(s,DomainClaim,item_id,ctx["tenant"])
    try:records=resolve_txt("_klyrow-verification."+item.domain)
    except Exception:records=[]
    verified=any(value.startswith("klyrow=") and hmac.compare_digest(item.challenge_hash,sha(value.split("=",1)[1])) for value in records)
    if not verified:raise HTTPException(422,"dns_ownership_not_verified")
    item.state="VERIFIED";item.verified_at=now()
    legacy=s.scalar(select(Domain).where(Domain.tenant_id==ctx["tenant"],Domain.domain==item.domain))
    if legacy is None:s.add(Domain(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],domain=item.domain,token="dns-verified",verified=True))
    else:legacy.verified=True
    audit(s,ctx,"domain.verified");s.commit();return {"id":item.id,"state":item.state}
@router.post("/domains/claims/{item_id}/dkim/rotate",status_code=201)
def dkim_rotate(item_id:str,ctx=Depends(auth),s:Session=Depends(db)):
    require_permission(ctx, "domain.manage")
    item=tenant_get(s,DomainClaim,item_id,ctx["tenant"])
    if item.state not in {"VERIFIED","SENDING_ENABLED"}:raise HTTPException(409,"domain_not_verified")
    for key in s.scalars(select(DkimKeyVersion).where(DkimKeyVersion.domain_claim_id==item.id,DkimKeyVersion.active==True)).all():key.active=False;key.retired_at=now()
    item.dkim_version+=1
    from .provider import generate_dkim_material
    selector,public,private_reference=generate_dkim_material(item,item.dkim_version)
    item.dkim_selector=selector
    key=DkimKeyVersion(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],domain_claim_id=item.id,selector=selector,version=item.dkim_version,public_key=public,private_key_reference=private_reference)
    s.add(key);audit(s,ctx,"domain.dkim_rotated");s.commit();return {"selector":key.selector,"version":key.version,"public_record":key.public_key,"private_key_exported":False}
@router.post("/senders",status_code=201)
def sender_create(x:SenderIn,ctx=Depends(auth),s:Session=Depends(db)):
    require_permission(ctx, "sender.manage")
    claim=tenant_get(s,DomainClaim,x.domain_claim_id,ctx["tenant"]);kind=x.stream.upper()
    if claim.state not in {"VERIFIED","SENDING_ENABLED"}:raise HTTPException(409,"verified_domain_required")
    if x.email.lower().rsplit("@",1)[1]!=claim.domain:raise HTTPException(403,"sender_spoofing_denied")
    if kind not in STREAMS:raise HTTPException(422,"invalid_message_stream")
    item=SenderIdentity(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],domain_claim_id=claim.id,address=x.email.lower(),display_name=x.display_name,reply_to=str(x.reply_to).lower() if x.reply_to else None,stream=kind,status="ACTIVE",verified=True);s.add(item)
    allowed=s.scalar(select(AllowedSender).where(AllowedSender.tenant_id==ctx["tenant"],AllowedSender.address==item.address))
    if allowed is None:s.add(AllowedSender(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],address=item.address,role="customer_sender",enabled=True))
    else:allowed.enabled=True
    audit(s,ctx,"sender.created");s.commit();return {"id":item.id,"status":item.status,"verified":item.verified}
@router.get("/senders")
def senders(ctx=Depends(auth),s:Session=Depends(db)):
    return s.scalars(select(SenderIdentity).where(SenderIdentity.tenant_id==ctx["tenant"]).order_by(SenderIdentity.address)).all()
@router.post("/streams",status_code=201)
def stream_create(x:StreamIn,ctx=Depends(auth),s:Session=Depends(db)):
    kind=x.kind.upper()
    if kind not in STREAMS:raise HTTPException(422,"invalid_message_stream")
    if kind in {"TRANSACTIONAL","SECURITY"} and x.suppression_policy=="MARKETING_GLOBAL":raise HTTPException(422,"stream_suppression_policy_invalid")
    item=MessageStream(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],name=x.name,kind=kind,rate_limit=x.rate_limit,retention_days=x.retention_days,tracking_enabled=x.tracking_enabled,suppression_policy=x.suppression_policy);s.add(item);audit(s,ctx,"stream.created");s.commit();return {"id":item.id,"kind":item.kind,"reputation_state":item.reputation_state}
@router.get("/streams")
def streams(ctx=Depends(auth),s:Session=Depends(db)):
    return s.scalars(select(MessageStream).where(MessageStream.tenant_id==ctx["tenant"]).order_by(MessageStream.name)).all()

@router.get("/templates")
def templates(ctx=Depends(auth),s:Session=Depends(db)):
    return s.scalars(select(Template).where(Template.tenant_id==ctx["tenant"]).order_by(Template.created_at.desc())).all()
@router.post("/templates",status_code=201)
def template_create(x:TemplateIn,ctx=Depends(auth),s:Session=Depends(db)):
    require_permission(ctx, "template.manage")
    validate_html(x.html_body);item=Template(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],slug=x.slug,name=x.name,locale=x.locale);version=TemplateVersion(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],template_id=item.id,version=1,subject=x.subject,html_body=x.html_body,text_body=x.text_body,variables_json=json.dumps(sorted(set(x.variables))),created_by=ctx["sub"]);s.add_all([item,version]);audit(s,ctx,"template.created");s.commit();return {"id":item.id,"version":1,"status":item.status}
@router.put("/templates/{item_id}")
def template_update(item_id:str,x:TemplateUpdate,ctx=Depends(auth),s:Session=Depends(db)):
    require_permission(ctx, "template.manage")
    item=tenant_get(s,Template,item_id,ctx["tenant"]);validate_html(x.html_body);item.current_version+=1;item.status="DRAFT";version=TemplateVersion(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],template_id=item.id,version=item.current_version,subject=x.subject,html_body=x.html_body,text_body=x.text_body,variables_json=json.dumps(sorted(set(x.variables))),created_by=ctx["sub"]);s.add(version);audit(s,ctx,"template.version_created");s.commit();return {"id":item.id,"version":item.current_version,"status":item.status}
@router.post("/templates/{item_id}/publish")
def template_publish(item_id:str,ctx=Depends(auth),s:Session=Depends(db)):
    require_permission(ctx, "template.manage")
    item=tenant_get(s,Template,item_id,ctx["tenant"]);item.status="PUBLISHED";audit(s,ctx,"template.published");s.commit();return {"status":item.status,"version":item.current_version}
@router.post("/templates/{item_id}/rollback/{version}")
def template_rollback(item_id:str,version:int,ctx=Depends(auth),s:Session=Depends(db)):
    require_permission(ctx, "template.manage")
    item=tenant_get(s,Template,item_id,ctx["tenant"]);source=s.scalar(select(TemplateVersion).where(TemplateVersion.template_id==item.id,TemplateVersion.version==version,TemplateVersion.tenant_id==ctx["tenant"]));
    if not source:raise HTTPException(404,"template_version_not_found")
    item.current_version+=1;copy=TemplateVersion(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],template_id=item.id,version=item.current_version,subject=source.subject,html_body=source.html_body,text_body=source.text_body,variables_json=source.variables_json,created_by=ctx["sub"]);item.status="DRAFT";s.add(copy);audit(s,ctx,"template.rolled_back");s.commit();return {"version":item.current_version,"source_version":version,"status":item.status}
@router.post("/templates/{item_id}/render")
def template_render(item_id:str,x:RenderIn,ctx=Depends(auth),s:Session=Depends(db)):
    item=tenant_get(s,Template,item_id,ctx["tenant"]);version=s.scalar(select(TemplateVersion).where(TemplateVersion.template_id==item.id,TemplateVersion.version==item.current_version));return render_version(version,x.variables)

@router.post("/campaign-definitions",status_code=201)
def campaign_create(x:CampaignIn,ctx=Depends(auth),s:Session=Depends(db)):
    require_permission(ctx, "campaign.manage")
    sender=tenant_get(s,SenderIdentity,x.sender_id,ctx["tenant"]);template=tenant_get(s,Template,x.template_id,ctx["tenant"])
    if not sender.verified or sender.stream!="MARKETING":raise HTTPException(409,"marketing_sender_required")
    if template.status!="PUBLISHED":raise HTTPException(409,"published_template_required")
    item=CampaignDefinition(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],name=x.name,sender_id=sender.id,template_id=template.id,segment_id=x.segment_id,timezone=x.timezone,frequency_cap=x.frequency_cap,tracking_json=json.dumps(x.tracking));s.add(item);audit(s,ctx,"campaign.created");s.commit();return {"id":item.id,"status":item.status}
@router.get("/campaign-definitions")
def campaign_definitions(ctx=Depends(auth),s:Session=Depends(db)):
    return s.scalars(select(CampaignDefinition).where(CampaignDefinition.tenant_id==ctx["tenant"]).order_by(CampaignDefinition.created_at.desc())).all()
@router.post("/campaign-definitions/{item_id}/test")
def campaign_test(item_id:str,ctx=Depends(auth),s:Session=Depends(db)):
    require_permission(ctx, "campaign.manage")
    item=tenant_get(s,CampaignDefinition,item_id,ctx["tenant"]);item.status="TESTING";item.test_sent_at=now();audit(s,ctx,"campaign.test_fixture_completed");s.commit();return {"status":item.status,"provider_submission":False,"internal_sink":True}
@router.post("/campaign-definitions/{item_id}/preflight")
def campaign_preflight(item_id:str,x:PreflightIn,ctx=Depends(auth),s:Session=Depends(db)):
    item=tenant_get(s,CampaignDefinition,item_id,ctx["tenant"])
    if not item.test_sent_at:raise HTTPException(409,"campaign_test_required")
    eligible=max(0,x.estimated_recipients-x.estimated_suppressed-x.estimated_invalid);allowed=eligible<=x.quota_remaining;return {"recipients":x.estimated_recipients,"suppressed":x.estimated_suppressed,"invalid":x.estimated_invalid,"eligible":eligible,"quota_impact":eligible,"estimated_usage_cost":str(x.estimated_unit_cost),"allowed":allowed}
@router.post("/campaign-definitions/{item_id}/schedule", responses={409: {"description": "Campaign dispatcher unavailable; scheduling is not accepted."}})
def campaign_schedule(item_id:str,x:ScheduleIn,ctx=Depends(auth),s:Session=Depends(db)):
    require_permission(ctx, "campaign.manage")
    item=tenant_get(s,CampaignDefinition,item_id,ctx["tenant"])
    if not item.test_sent_at:raise HTTPException(409,"campaign_test_required")
    if x.scheduled_at.astimezone(timezone.utc)<=now():raise HTTPException(422,"schedule_must_be_future")
    raise HTTPException(409,"campaign_dispatcher_unavailable")
@router.post("/campaign-definitions/{item_id}/cancel")
def campaign_cancel(item_id:str,ctx=Depends(auth),s:Session=Depends(db)):
    require_permission(ctx, "campaign.manage")
    item=tenant_get(s,CampaignDefinition,item_id,ctx["tenant"])
    if item.status not in {"DRAFT","TESTING","SCHEDULED","PAUSED"}:raise HTTPException(409,"campaign_cannot_be_cancelled")
    item.status="CANCELLED";audit(s,ctx,"campaign.cancelled");s.commit();return {"status":item.status}

@router.post("/inbound/routes",status_code=201)
def inbound_route(x:InboundRouteIn,ctx=Depends(auth),s:Session=Depends(db)):
    claim=tenant_get(s,DomainClaim,x.domain_claim_id,ctx["tenant"]);recipient=str(x.recipient).lower()
    if claim.state not in {"VERIFIED","SENDING_ENABLED"}:raise HTTPException(409,"verified_domain_required")
    if recipient.rsplit("@",1)[1]!=claim.domain:raise HTTPException(403,"inbound_domain_mismatch")
    if x.wildcard and recipient.split("@",1)[0]!="*":raise HTTPException(422,"wildcard_route_must_use_star")
    if x.destination_kind=="WEBHOOK":safe_webhook_url(x.destination_ref)
    item=InboundRoute(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],domain_claim_id=claim.id,recipient=recipient,wildcard=x.wildcard,destination_kind=x.destination_kind,destination_ref=x.destination_ref,max_bytes=x.max_bytes,enabled=True);s.add(item);audit(s,ctx,"inbound.route_created");s.commit();return {"id":item.id,"enabled":item.enabled,"catch_all":item.wildcard}
@router.get("/inbound/routes")
def inbound_routes(ctx=Depends(auth),s:Session=Depends(db)):
    return s.scalars(select(InboundRoute).where(InboundRoute.tenant_id==ctx["tenant"]).order_by(InboundRoute.recipient)).all()
@router.get("/inbound/messages")
def inbound_messages(ctx=Depends(auth),s:Session=Depends(db)):
    return s.scalars(select(InboundMessage).where(InboundMessage.tenant_id==ctx["tenant"]).order_by(InboundMessage.created_at.desc()).limit(200)).all()
@router.post("/inbound/fixtures",status_code=202)
def inbound_fixture(x:InboundFixture,ctx=Depends(auth),s:Session=Depends(db)):
    recipient=str(x.recipient).lower();route=s.scalar(select(InboundRoute).where(InboundRoute.tenant_id==ctx["tenant"],InboundRoute.recipient==recipient,InboundRoute.enabled==True))
    if not route:raise HTTPException(404,"inbound_route_not_found")
    if x.size_bytes>route.max_bytes:raise HTTPException(413,"inbound_message_too_large")
    old=s.scalar(select(InboundMessage).where(InboundMessage.tenant_id==ctx["tenant"],InboundMessage.message_id==x.message_id))
    if old:return {"id":old.id,"duplicate":True,"state":old.state}
    state="QUARANTINED" if x.malware_status!="CLEAN" or x.spam_score>=80 else "ROUTED";item=InboundMessage(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],route_id=route.id,message_id=x.message_id,sender=str(x.sender).lower(),recipient=recipient,in_reply_to=x.in_reply_to,references_json=json.dumps(x.references),headers_json=json.dumps(x.headers),attachment_manifest_json=json.dumps(x.attachments),spam_score=x.spam_score,malware_status=x.malware_status,state=state);s.add(item);audit(s,ctx,"inbound."+state.lower());s.commit();return {"id":item.id,"duplicate":False,"state":state,"thread_headers_preserved":True}

@router.post("/webhook-subscriptions",status_code=201)
def webhook_create(x:WebhookIn,ctx=Depends(auth),s:Session=Depends(db)):
    from .secret_responses import record_secret_response,response_metadata
    url=safe_webhook_url(x.url);raw=secrets.token_urlsafe(32);item=WebhookSubscription(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],url=url,events_json=json.dumps(sorted(set(x.events))),secret_hash=sha(raw),encrypted_secret_ref="secret://webhooks/"+str(uuid.uuid4()));s.add(item);result={"id":item.id,"secret":raw,"events":json.loads(item.events_json)};secret_response=record_secret_response(s,tenant_id=ctx["tenant"],resource_type="WEBHOOK_SECRET",resource_id=item.id,action="CREATE",payload=result,actor=ctx["sub"]);audit(s,ctx,"webhook.created");s.commit();return {**result,**response_metadata(secret_response)}
@router.get("/webhook-subscriptions")
def webhook_subscriptions(ctx=Depends(auth),s:Session=Depends(db)):
    rows=s.scalars(select(WebhookSubscription).where(WebhookSubscription.tenant_id==ctx["tenant"]).order_by(WebhookSubscription.created_at.desc())).all()
    return [{"id":row.id,"url":row.url,"events":json.loads(row.events_json),"enabled":row.enabled,"created_at":row.created_at,"rotated_at":row.rotated_at} for row in rows]
@router.post("/webhook-subscriptions/{item_id}/rotate")
def webhook_rotate(item_id:str,ctx=Depends(auth),s:Session=Depends(db)):
    from .secret_responses import record_secret_response,response_metadata
    item=tenant_get(s,WebhookSubscription,item_id,ctx["tenant"]);raw=secrets.token_urlsafe(32);item.secret_hash=sha(raw);item.encrypted_secret_ref="secret://webhooks/"+str(uuid.uuid4());item.rotated_at=now();result={"secret":raw,"rotated_at":item.rotated_at.isoformat()};secret_response=record_secret_response(s,tenant_id=ctx["tenant"],resource_type="WEBHOOK_SECRET",resource_id=item.id,action="ROTATE",payload=result,actor=ctx["sub"]);audit(s,ctx,"webhook.rotated");s.commit();return {"secret":raw,"rotated_at":item.rotated_at,**response_metadata(secret_response)}
@router.post("/webhook-subscriptions/{item_id}/test",status_code=202)
def webhook_test(item_id:str,x:EventIn,ctx=Depends(auth),s:Session=Depends(db)):
    item=tenant_get(s,WebhookSubscription,item_id,ctx["tenant"])
    if x.event_type not in json.loads(item.events_json):raise HTTPException(422,"event_not_subscribed")
    attempt=s.scalar(select(WebhookAttempt).where(WebhookAttempt.subscription_id==item.id,WebhookAttempt.event_id==x.event_id))
    if attempt:return {"id":attempt.id,"duplicate":True,"state":attempt.state}
    attempt=WebhookAttempt(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],subscription_id=item.id,event_id=x.event_id,event_type=x.event_type);s.add(attempt);s.commit();return {"id":attempt.id,"duplicate":False,"state":attempt.state,"signature_contract":"HMAC-SHA256(timestamp.event_id.body)","provider_submission":False}

@router.post("/delivery-jobs/{message_id}",status_code=201)
def job_create(message_id:str,ctx=Depends(auth),s:Session=Depends(db)):
    message=tenant_get(s,Message,message_id,ctx["tenant"]);old=s.scalar(select(DeliveryJob).where(DeliveryJob.message_id==message.id));
    if old:return {"id":old.id,"duplicate":True,"state":old.state}
    item=DeliveryJob(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],message_id=message.id);s.add(item);s.commit();return {"id":item.id,"duplicate":False,"state":item.state}
@router.post("/delivery-jobs/{job_id}/lease")
def job_lease(job_id:str,x:LeaseIn,ctx=Depends(auth),s:Session=Depends(db)):
    item=s.scalar(select(DeliveryJob).where(DeliveryJob.id==job_id,DeliveryJob.tenant_id==ctx["tenant"]).with_for_update())
    if not item:raise HTTPException(404,"delivery_job_not_found")
    if item.state in {"COMPLETED","DEAD_LETTER"}:raise HTTPException(409,"delivery_job_terminal")
    current=now();expires=item.lease_expires_at.replace(tzinfo=timezone.utc) if item.lease_expires_at and item.lease_expires_at.tzinfo is None else item.lease_expires_at
    if expires and expires>current and item.lease_owner!=x.worker_id:raise HTTPException(409,"delivery_job_leased")
    item.state="PROCESSING";item.lease_owner=x.worker_id;item.lease_expires_at=current+timedelta(seconds=x.lease_seconds);s.commit();return {"state":item.state,"lease_expires_at":item.lease_expires_at}
@router.post("/delivery-jobs/{job_id}/fail")
def job_fail(job_id:str,x:FailIn,ctx=Depends(auth),s:Session=Depends(db)):
    item=tenant_get(s,DeliveryJob,job_id,ctx["tenant"]);item.attempts+=1;item.error_class=x.error_class;temporary=x.error_class in {"NETWORK_FAILURE","PROVIDER_TEMPORARY","RATE_LIMIT","SOFT_BOUNCE"}
    if temporary and item.attempts<5:item.state="RETRY";item.next_attempt_at=now()+timedelta(seconds=min(3600,2**item.attempts*30))
    else:item.state="DEAD_LETTER";item.dead_lettered_at=now()
    item.lease_owner=None;item.lease_expires_at=None;s.commit();return {"state":item.state,"attempts":item.attempts,"retryable":temporary and item.state=="RETRY"}

@router.post("/reputation/recalculate")
def reputation(ctx=Depends(auth),s:Session=Depends(db)):
    total=s.scalar(select(func.count(Message.id)).where(Message.tenant_id==ctx["tenant"])) or 0;delivered=s.scalar(select(func.count(Event.id)).where(Event.tenant_id==ctx["tenant"],Event.kind.in_(["email.delivered","klyrow.email.delivered"]))) or 0;bounced=s.scalar(select(func.count(Event.id)).where(Event.tenant_id==ctx["tenant"],Event.kind.in_(["email.bounced","klyrow.email.bounced"]))) or 0;complaints=s.scalar(select(func.count(Event.id)).where(Event.tenant_id==ctx["tenant"],Event.kind.in_(["email.complained","klyrow.email.complained"]))) or 0
    bounce_rate=bounced/total if total else 0;complaint_rate=complaints/total if total else 0;state="SUSPENDED" if complaint_rate>=.005 or bounce_rate>=.10 else ("LIMITED" if complaint_rate>=.002 or bounce_rate>=.05 else ("WATCH" if bounce_rate>=.02 else "GOOD"));snap=ReputationSnapshot(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],sent=total,delivered=delivered,hard_bounces=bounced,complaints=complaints,invalid=0,state=state);s.add(snap)
    if state=="SUSPENDED":s.get(Tenant,ctx["tenant"]).enabled=False
    audit(s,ctx,"reputation.recalculated");s.commit();return {"state":state,"sent":total,"delivery_rate":delivered/total if total else 0,"bounce_rate":bounce_rate,"complaint_rate":complaint_rate}
