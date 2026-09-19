"""Explicit tenant membership and credential lifecycle authority."""
import hashlib, hmac, json, secrets, uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .main import Base, SECRET, Tenant, User, audit, auth, db, ph, require, sha
from .saas import SessionRecord

router=APIRouter(prefix="/v1",tags=["Tenant authority"])
now=lambda:datetime.now(timezone.utc)

ROLE_PERMISSIONS={
 "OWNER":{"*"},"ADMIN":{"tenant.manage","member.manage","credential.manage","mail.send","mail.read","billing.read"},
 "DEVELOPER":{"credential.manage","mail.send","mail.read","webhook.manage"},"BILLING":{"billing.read","billing.manage"},
 "SUPPORT":{"mail.read","support.manage"},"MARKETING":{"mail.send","campaign.manage","contact.manage"},
 "ANALYST":{"mail.read","analytics.read"},"READ_ONLY":{"mail.read","analytics.read","billing.read"},
}

class Organization(Base):
    __tablename__="organizations"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(ForeignKey("tenants.id"),unique=True,index=True); name:Mapped[str]=mapped_column(String); slug:Mapped[str]=mapped_column(String,unique=True); status:Mapped[str]=mapped_column(String,default="ACTIVE"); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
class TenantMember(Base):
    __tablename__="tenant_members"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); user_id:Mapped[str]=mapped_column(String,index=True); role:Mapped[str]=mapped_column(String); active:Mapped[bool]=mapped_column(Boolean,default=True); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); __table_args__=(UniqueConstraint("tenant_id","user_id",name="uq_tenant_member"),)
class TenantInvitation(Base):
    __tablename__="tenant_invitations"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); email:Mapped[str]=mapped_column(String,index=True); role:Mapped[str]=mapped_column(String); token_hash:Mapped[str]=mapped_column(String,unique=True); expires_at:Mapped[datetime]=mapped_column(DateTime(timezone=True)); accepted_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True); revoked_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True); created_by:Mapped[str]=mapped_column(String); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
class TenantSetting(Base):
    __tablename__="tenant_settings"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); key:Mapped[str]=mapped_column(String); value_json:Mapped[str]=mapped_column(Text); updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); __table_args__=(UniqueConstraint("tenant_id","key",name="uq_tenant_setting"),)
class TenantFeature(Base):
    __tablename__="tenant_features"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); key:Mapped[str]=mapped_column(String); enabled:Mapped[bool]=mapped_column(Boolean,default=False); source:Mapped[str]=mapped_column(String,default="plan"); __table_args__=(UniqueConstraint("tenant_id","key",name="uq_tenant_feature"),)
class TenantLimit(Base):
    __tablename__="tenant_limits"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); key:Mapped[str]=mapped_column(String); value:Mapped[int]=mapped_column(Integer); source:Mapped[str]=mapped_column(String,default="plan"); __table_args__=(UniqueConstraint("tenant_id","key",name="uq_tenant_limit"),)
class ServiceAccount(Base):
    __tablename__="service_accounts"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); name:Mapped[str]=mapped_column(String); client_id:Mapped[str]=mapped_column(String,unique=True); secret_hash:Mapped[str]=mapped_column(String); scopes_json:Mapped[str]=mapped_column(Text); expires_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True); revoked_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True); rotated_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True); created_by:Mapped[str]=mapped_column(String); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
class ScopedApiKey(Base):
    __tablename__="scoped_api_keys"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); name:Mapped[str]=mapped_column(String); prefix:Mapped[str]=mapped_column(String,index=True); verifier_hash:Mapped[str]=mapped_column(String,unique=True); scopes_json:Mapped[str]=mapped_column(Text); environment:Mapped[str]=mapped_column(String); ip_allowlist_json:Mapped[str]=mapped_column(Text,default="[]"); created_by:Mapped[str]=mapped_column(String); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); last_used_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True); expires_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True); revoked_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True); rotated_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True)
class SmtpCredential(Base):
    __tablename__="tenant_smtp_credentials"; id:Mapped[str]=mapped_column(String,primary_key=True); tenant_id:Mapped[str]=mapped_column(String,index=True); username:Mapped[str]=mapped_column(String,unique=True); verifier_hash:Mapped[str]=mapped_column(String); scopes_json:Mapped[str]=mapped_column(Text); created_by:Mapped[str]=mapped_column(String); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); expires_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True); revoked_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True); rotated_at:Mapped[Optional[datetime]]=mapped_column(DateTime(timezone=True),nullable=True)
class OidcIdentity(Base):
    __tablename__="oidc_identities"; id:Mapped[str]=mapped_column(String,primary_key=True); issuer:Mapped[str]=mapped_column(String,index=True); subject:Mapped[str]=mapped_column(String,index=True); user_id:Mapped[str]=mapped_column(String,index=True); default_tenant_id:Mapped[Optional[str]]=mapped_column(String,nullable=True); identity_type:Mapped[str]=mapped_column(String,default="KLYROW_ONLY"); enabled:Mapped[bool]=mapped_column(Boolean,default=True); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); __table_args__=(UniqueConstraint("issuer","subject",name="uq_oidc_issuer_subject"),)

class OrgIn(BaseModel): name:str=Field(min_length=2,max_length=120); slug:str=Field(pattern=r"^[a-z0-9][a-z0-9-]{1,61}$")
class InviteIn(BaseModel): email:EmailStr; role:str; expires_hours:int=Field(default=72,ge=1,le=720)
class AcceptIn(BaseModel): token:str; display_email:Optional[EmailStr]=None
class RoleIn(BaseModel): role:str
class ServiceIn(BaseModel): name:str=Field(min_length=2,max_length=100); scopes:list[str]=Field(min_length=1,max_length=30); expires_at:Optional[datetime]=None
class KeyIn(BaseModel): name:str=Field(min_length=2,max_length=100); scopes:list[str]=Field(min_length=1,max_length=30); environment:str=Field(pattern="^(test|development|staging|production)$"); ip_allowlist:list[str]=Field(default_factory=list,max_length=50); expires_at:Optional[datetime]=None
class SmtpIn(BaseModel): scopes:list[str]=Field(default=["smtp.send"],min_length=1,max_length=10); expires_at:Optional[datetime]=None
class OidcIdentityIn(BaseModel):subject:str=Field(min_length=8,max_length=200);user_id:str;default_tenant_id:Optional[str]=None;identity_type:str=Field(pattern="^(KLYROW_ONLY|KLYROW_ODOO_PORTAL|INTERNAL_EMPLOYEE|SERVICE_ACCOUNT)$")

def member(s,tenant,user):return s.scalar(select(TenantMember).where(TenantMember.tenant_id==tenant,TenantMember.user_id==user,TenantMember.active==True))
def manage(ctx,s):
    m=member(s,ctx["tenant"],ctx["sub"])
    if ctx.get("role")=="platform_admin":return None
    if not m or m.role not in {"OWNER","ADMIN"}:raise HTTPException(403,"tenant_management_denied")
    return m
def validate_role(role):
    role=role.upper()
    if role not in ROLE_PERMISSIONS:raise HTTPException(422,"invalid_tenant_role")
    return role
def validate_scopes(scopes):
    allowed={"mail.read","mail.send","smtp.send","domain.read","domain.manage","sender.manage","template.manage","contact.manage","campaign.manage","webhook.manage","analytics.read","billing.read","billing.manage"}
    if not set(scopes)<=allowed:raise HTTPException(422,"invalid_scope")

@router.post("/organizations",status_code=201)
def organization(x:OrgIn,ctx=Depends(auth),s:Session=Depends(db)):
    if s.scalar(select(Organization).where(Organization.slug==x.slug)):raise HTTPException(409,"organization_slug_taken")
    from .business_events import enqueue_named_event
    tenant=Tenant(id=str(uuid.uuid4()),name=x.name,quota=1000);org=Organization(id=str(uuid.uuid4()),tenant_id=tenant.id,name=x.name,slug=x.slug);membership=TenantMember(id=str(uuid.uuid4()),tenant_id=tenant.id,user_id=ctx["sub"],role="OWNER");s.add_all([tenant,org,membership]);enqueue_named_event(s,event_type="klyrow.tenant.created",tenant_id=tenant.id,aggregate_id=tenant.id,causation_id=org.id,data={"tenant_id":tenant.id,"name":tenant.name,"organization_id":org.id,"enabled":True});audit(s,{**ctx,"tenant":tenant.id},"organization.created");s.commit();return {"id":org.id,"tenant_id":tenant.id,"slug":org.slug}
@router.get("/auth/oidc/config")
def oidc_config():
    issuer="https://auth.codestra.co/realms/codestra"
    return {"issuer":issuer,"authorization_endpoint":issuer+"/protocol/openid-connect/auth","token_endpoint":issuer+"/protocol/openid-connect/token","client_id":"klyrow-portal","response_type":"code","code_challenge_method":"S256","scopes":["openid","profile","email"],"local_password_login":False}
@router.post("/admin/oidc-identities",status_code=201)
def oidc_identity(x:OidcIdentityIn,ctx=Depends(require("platform_admin")),s:Session=Depends(db)):
    issuer="https://auth.codestra.co/realms/codestra";user=s.get(User,x.user_id)
    if not user:raise HTTPException(404,"user_not_found")
    if x.default_tenant_id and not s.get(Tenant,x.default_tenant_id):raise HTTPException(404,"tenant_not_found")
    existing=s.scalar(select(OidcIdentity).where(OidcIdentity.issuer==issuer,OidcIdentity.subject==x.subject))
    if existing:raise HTTPException(409,"oidc_identity_exists")
    item=OidcIdentity(id=str(uuid.uuid4()),issuer=issuer,subject=x.subject,user_id=user.id,default_tenant_id=x.default_tenant_id,identity_type=x.identity_type);s.add(item);audit(s,ctx,"oidc_identity.created");s.commit();return {"id":item.id,"issuer":item.issuer,"subject":item.subject,"identity_type":item.identity_type}
@router.get("/organizations")
def organizations(ctx=Depends(auth),s:Session=Depends(db)):
    ids=select(TenantMember.tenant_id).where(TenantMember.user_id==ctx["sub"],TenantMember.active==True);return s.scalars(select(Organization).where(Organization.tenant_id.in_(ids))).all()
@router.post("/organizations/{tenant_id}/switch")
def switch(tenant_id:str,ctx=Depends(auth),s:Session=Depends(db)):
    m=member(s,tenant_id,ctx["sub"])
    if not m:raise HTTPException(404,"organization_not_found")
    sid=str(uuid.uuid4());s.add(SessionRecord(id=sid,user_id=ctx["sub"],tenant_id=tenant_id));s.commit();raw=jwt.encode({"sub":ctx["sub"],"tenant":tenant_id,"role":m.role,"sid":sid,"exp":now()+timedelta(hours=8)},SECRET,algorithm="HS256");return {"access_token":raw,"token_type":"bearer","tenant_id":tenant_id,"role":m.role}
@router.post("/team/invitations",status_code=201)
def invite(x:InviteIn,ctx=Depends(auth),s:Session=Depends(db)):
    manage(ctx,s);role=validate_role(x.role);raw=secrets.token_urlsafe(32);item=TenantInvitation(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],email=x.email.lower(),role=role,token_hash=sha(raw),expires_at=now()+timedelta(hours=x.expires_hours),created_by=ctx["sub"]);s.add(item);audit(s,ctx,"tenant.invitation.created");s.commit();return {"id":item.id,"token":raw,"expires_at":item.expires_at}
@router.get("/team/members")
def team_members(ctx=Depends(auth),s:Session=Depends(db)):
    return s.scalars(select(TenantMember).where(TenantMember.tenant_id==ctx["tenant"],TenantMember.active==True).order_by(TenantMember.created_at)).all()
@router.get("/team/invitations")
def team_invitations(ctx=Depends(auth),s:Session=Depends(db)):
    manage(ctx,s);return s.scalars(select(TenantInvitation).where(TenantInvitation.tenant_id==ctx["tenant"]).order_by(TenantInvitation.created_at.desc())).all()
@router.post("/team/invitations/accept",status_code=201)
def accept(x:AcceptIn,s:Session=Depends(db)):
    item=s.scalar(select(TenantInvitation).where(TenantInvitation.token_hash==sha(x.token)))
    if not item or item.revoked_at or item.accepted_at or item.expires_at.replace(tzinfo=timezone.utc)<now():raise HTTPException(410,"invitation_invalid_or_expired")
    user=s.scalar(select(User).where(User.email==item.email));
    if not user:raise HTTPException(409,"keycloak_user_link_required")
    old=s.scalar(select(TenantMember).where(TenantMember.tenant_id==item.tenant_id,TenantMember.user_id==user.id));m=old or TenantMember(id=str(uuid.uuid4()),tenant_id=item.tenant_id,user_id=user.id,role=item.role);m.role=item.role;m.active=True;item.accepted_at=now();s.add(m);s.commit();return {"tenant_id":item.tenant_id,"role":m.role,"user_id":user.id}
@router.patch("/team/members/{user_id}")
def role_change(user_id:str,x:RoleIn,ctx=Depends(auth),s:Session=Depends(db)):
    manage(ctx,s);m=s.scalar(select(TenantMember).where(TenantMember.tenant_id==ctx["tenant"],TenantMember.user_id==user_id));
    if not m:raise HTTPException(404,"member_not_found")
    if m.role=="OWNER" and x.role.upper()!="OWNER":raise HTTPException(409,"owner_transfer_required")
    m.role=validate_role(x.role);audit(s,ctx,"tenant.member.role_changed");s.commit();return {"user_id":user_id,"role":m.role}
@router.delete("/team/members/{user_id}",status_code=204)
def remove_member(user_id:str,ctx=Depends(auth),s:Session=Depends(db)):
    manage(ctx,s);m=s.scalar(select(TenantMember).where(TenantMember.tenant_id==ctx["tenant"],TenantMember.user_id==user_id));
    if not m:raise HTTPException(404,"member_not_found")
    if m.role=="OWNER":raise HTTPException(409,"owner_cannot_be_removed")
    m.active=False;audit(s,ctx,"tenant.member.removed");s.commit()

def new_service_secret():return "klys_"+secrets.token_urlsafe(36)
@router.get("/service-accounts")
def service_accounts(ctx=Depends(auth),s:Session=Depends(db)):
    manage(ctx,s);rows=s.scalars(select(ServiceAccount).where(ServiceAccount.tenant_id==ctx["tenant"]).order_by(ServiceAccount.created_at.desc())).all()
    return [{"id":row.id,"name":row.name,"client_id":row.client_id,"scopes":json.loads(row.scopes_json),"expires_at":row.expires_at,"revoked_at":row.revoked_at,"rotated_at":row.rotated_at,"created_at":row.created_at} for row in rows]
@router.post("/service-accounts",status_code=201)
def service_create(x:ServiceIn,ctx=Depends(auth),s:Session=Depends(db)):
    manage(ctx,s);validate_scopes(x.scopes);raw=new_service_secret();item=ServiceAccount(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],name=x.name,client_id="klyrow_"+secrets.token_hex(12),secret_hash=ph.hash(raw),scopes_json=json.dumps(sorted(set(x.scopes))),expires_at=x.expires_at,created_by=ctx["sub"]);s.add(item);audit(s,ctx,"service_account.created");s.commit();return {"id":item.id,"client_id":item.client_id,"client_secret":raw,"scopes":json.loads(item.scopes_json)}
@router.post("/service-accounts/{item_id}/rotate")
def service_rotate(item_id:str,ctx=Depends(auth),s:Session=Depends(db)):
    manage(ctx,s);item=s.scalar(select(ServiceAccount).where(ServiceAccount.id==item_id,ServiceAccount.tenant_id==ctx["tenant"],ServiceAccount.revoked_at==None));
    if not item:raise HTTPException(404,"service_account_not_found")
    raw=new_service_secret();item.secret_hash=ph.hash(raw);item.rotated_at=now();audit(s,ctx,"service_account.rotated");s.commit();return {"client_id":item.client_id,"client_secret":raw}
@router.delete("/service-accounts/{item_id}",status_code=204)
def service_revoke(item_id:str,ctx=Depends(auth),s:Session=Depends(db)):
    manage(ctx,s);item=s.scalar(select(ServiceAccount).where(ServiceAccount.id==item_id,ServiceAccount.tenant_id==ctx["tenant"]));
    if not item:raise HTTPException(404,"service_account_not_found")
    item.revoked_at=now();audit(s,ctx,"service_account.revoked");s.commit()

@router.post("/developer/api-keys",status_code=201)
def api_key_create(x:KeyIn,ctx=Depends(auth),s:Session=Depends(db)):
    from .secret_responses import record_secret_response,response_metadata
    manage(ctx,s);validate_scopes(x.scopes);raw="kly_live_"+secrets.token_urlsafe(36);item=ScopedApiKey(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],name=x.name,prefix=raw[:16],verifier_hash=sha(raw),scopes_json=json.dumps(sorted(set(x.scopes))),environment=x.environment,ip_allowlist_json=json.dumps(x.ip_allowlist),created_by=ctx["sub"],expires_at=x.expires_at);s.add(item);result={"id":item.id,"secret":raw,"prefix":item.prefix,"scopes":json.loads(item.scopes_json)};secret_response=record_secret_response(s,tenant_id=ctx["tenant"],resource_type="API_KEY",resource_id=item.id,action="CREATE",payload=result,actor=ctx["sub"]);audit(s,ctx,"api_key.created");s.commit();return {**result,**response_metadata(secret_response)}
@router.get("/developer/api-keys")
def api_keys(ctx=Depends(auth),s:Session=Depends(db)):
    manage(ctx,s);rows=s.scalars(select(ScopedApiKey).where(ScopedApiKey.tenant_id==ctx["tenant"]).order_by(ScopedApiKey.created_at.desc())).all()
    return [{"id":row.id,"name":row.name,"prefix":row.prefix,"scopes":json.loads(row.scopes_json),"environment":row.environment,"ip_allowlist":json.loads(row.ip_allowlist_json),"last_used_at":row.last_used_at,"expires_at":row.expires_at,"revoked_at":row.revoked_at,"rotated_at":row.rotated_at,"created_at":row.created_at} for row in rows]
@router.post("/developer/api-keys/{item_id}/rotate")
def api_key_rotate(item_id:str,ctx=Depends(auth),s:Session=Depends(db)):
    from .secret_responses import record_secret_response,response_metadata
    manage(ctx,s);item=s.scalar(select(ScopedApiKey).where(ScopedApiKey.id==item_id,ScopedApiKey.tenant_id==ctx["tenant"],ScopedApiKey.revoked_at==None))
    if not item:raise HTTPException(404,"api_key_not_found")
    raw="kly_live_"+secrets.token_urlsafe(36);item.prefix=raw[:16];item.verifier_hash=sha(raw);item.rotated_at=now();result={"id":item.id,"secret":raw,"prefix":item.prefix,"scopes":json.loads(item.scopes_json)};secret_response=record_secret_response(s,tenant_id=ctx["tenant"],resource_type="API_KEY",resource_id=item.id,action="ROTATE",payload=result,actor=ctx["sub"]);audit(s,ctx,"api_key.rotated");s.commit();return {**result,**response_metadata(secret_response)}
@router.delete("/developer/api-keys/{item_id}",status_code=204)
def api_key_revoke(item_id:str,ctx=Depends(auth),s:Session=Depends(db)):
    manage(ctx,s);item=s.scalar(select(ScopedApiKey).where(ScopedApiKey.id==item_id,ScopedApiKey.tenant_id==ctx["tenant"]));
    if not item:raise HTTPException(404,"api_key_not_found")
    item.revoked_at=now();audit(s,ctx,"api_key.revoked");s.commit()
@router.post("/developer/smtp-credentials",status_code=201)
def smtp_create(x:SmtpIn,ctx=Depends(auth),s:Session=Depends(db)):
    from .secret_responses import record_secret_response,response_metadata
    manage(ctx,s)
    if set(x.scopes)!={"smtp.send"}:raise HTTPException(422,"invalid_smtp_scope")
    password=secrets.token_urlsafe(36);item=SmtpCredential(id=str(uuid.uuid4()),tenant_id=ctx["tenant"],username="smtp_"+secrets.token_hex(10),verifier_hash=ph.hash(password),scopes_json='["smtp.send"]',created_by=ctx["sub"],expires_at=x.expires_at);s.add(item);result={"id":item.id,"username":item.username,"password":password,"tls_required":True};secret_response=record_secret_response(s,tenant_id=ctx["tenant"],resource_type="SMTP_CREDENTIAL",resource_id=item.id,action="CREATE",payload=result,actor=ctx["sub"]);audit(s,ctx,"smtp_credential.created");s.commit();return {**result,**response_metadata(secret_response)}
@router.get("/developer/smtp-credentials")
def smtp_credentials(ctx=Depends(auth),s:Session=Depends(db)):
    manage(ctx,s);rows=s.scalars(select(SmtpCredential).where(SmtpCredential.tenant_id==ctx["tenant"]).order_by(SmtpCredential.created_at.desc())).all()
    return [{"id":row.id,"username":row.username,"scopes":json.loads(row.scopes_json),"tls_required":True,"expires_at":row.expires_at,"revoked_at":row.revoked_at,"rotated_at":row.rotated_at,"created_at":row.created_at} for row in rows]
@router.post("/developer/smtp-credentials/{item_id}/rotate")
def smtp_rotate(item_id:str,ctx=Depends(auth),s:Session=Depends(db)):
    from .secret_responses import record_secret_response,response_metadata
    manage(ctx,s);item=s.scalar(select(SmtpCredential).where(SmtpCredential.id==item_id,SmtpCredential.tenant_id==ctx["tenant"],SmtpCredential.revoked_at==None));
    if not item:raise HTTPException(404,"smtp_credential_not_found")
    password=secrets.token_urlsafe(36);item.verifier_hash=ph.hash(password);item.rotated_at=now();result={"username":item.username,"password":password,"tls_required":True};secret_response=record_secret_response(s,tenant_id=ctx["tenant"],resource_type="SMTP_CREDENTIAL",resource_id=item.id,action="ROTATE",payload=result,actor=ctx["sub"]);audit(s,ctx,"smtp_credential.rotated");s.commit();return {**result,**response_metadata(secret_response)}
@router.delete("/developer/smtp-credentials/{item_id}",status_code=204)
def smtp_revoke(item_id:str,ctx=Depends(auth),s:Session=Depends(db)):
    manage(ctx,s);item=s.scalar(select(SmtpCredential).where(SmtpCredential.id==item_id,SmtpCredential.tenant_id==ctx["tenant"]));
    if not item:raise HTTPException(404,"smtp_credential_not_found")
    item.revoked_at=now();audit(s,ctx,"smtp_credential.revoked");s.commit()
