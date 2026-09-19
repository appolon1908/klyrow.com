import asyncio,base64,hashlib,hmac,json,os,subprocess,sys,tempfile,time,uuid
from pathlib import Path
from unittest.mock import AsyncMock,patch
import httpx
import pytest
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import padding,rsa
SERVICE_TOKEN_FILE=str(Path(tempfile.gettempdir()) / "klyrow-beyvra-test-token")
Path(SERVICE_TOKEN_FILE).parent.mkdir(parents=True, exist_ok=True)
Path(SERVICE_TOKEN_FILE).write_text("bounded-beyvra-test-token",encoding="utf-8")
os.environ.update(KLYROW_DATABASE_URL="sqlite:///./test.db",KLYROW_SESSION_SECRET="test-session-secret-at-least-32-bytes",KLYROW_WEBHOOK_SECRET="hook-secret",KLYROW_MIDDLEWARE_API_KEY="middleware-command-test-token",KLYROW_SAFE_MODE="true",KLYROW_ADMIN_EMAIL="admin@example.com",KLYROW_ADMIN_PASSWORD="correct-horse-battery-staple",BEYVRA_EMAIL_SERVICE_TOKEN_FILE=SERVICE_TOKEN_FILE,BEYVRA_EMAIL_TENANT_ID="a",KLYROW_AUTH_RATE_PER_MINUTE="1000")
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from apps.gateway.app.main import AllowedSender,Audit,Base,DB,Domain,EmailOutbox,Event,Message,MiddlewareCommandIn,MiddlewareCommandOperation,PostalEvent,Suppression,Tenant,User,app,engine,middleware_command,ph,recover_middleware_commands,runtime_secret

def setup_module():
    Base.metadata.drop_all(engine); Base.metadata.create_all(engine)
    with DB() as s:
        for n in ("a","b"):
            t=Tenant(id=n,name=n,quota=10); s.add(t); s.add(User(id=n,tenant_id=n,email=f"{n}@example.com",password_hash=ph.hash("long-enough-password"),role="tenant_admin")); s.add(Domain(id=n,tenant_id=n,domain=f"{n}.example.com",token=n,verified=True));s.add(AllowedSender(id=n,tenant_id=n,address=f"sender@{n}.example.com",role="support"))
        s.commit()
client=TestClient(app)
def login(n): return client.post("/v1/auth/login",json={"email":f"{n}@example.com","password":"long-enough-password"}).json()["access_token"]
def hdr(n): return {"Authorization":"Bearer "+login(n)}
def middleware_hdr(n,correlation,idempotency): return {"Authorization":"Bearer middleware-command-test-token","X-Klyrow-Tenant-Id":n,"X-Tenant-ID":n,"X-Correlation-ID":correlation,"Idempotency-Key":idempotency}
def test_unauthorized(): assert client.get("/v1/domains").status_code==401
def test_resolver_outage_is_reported_as_authorization_unavailable():
    response=httpx.Response(503,json={"message":"core_web_api_maintenance"})
    with patch.dict(os.environ,{"KLYROW_TENANT_RESOLVER_URL":"https://resolver.test/resolve"}),patch("apps.gateway.app.main.httpx.get",return_value=response):
        result=client.get("/v1/domains",headers={"Authorization":"Bearer approved-service-token"})
    assert result.status_code==503
    assert result.json()=={"detail":"authorization_unavailable"}
def test_resolver_network_failure_is_reported_as_authorization_unavailable():
    request=httpx.Request("GET","https://resolver.test/resolve")
    with patch.dict(os.environ,{"KLYROW_TENANT_RESOLVER_URL":"https://resolver.test/resolve"}),patch("apps.gateway.app.main.httpx.get",side_effect=httpx.ConnectError("unavailable",request=request)):
        result=client.get("/v1/domains",headers={"Authorization":"Bearer approved-service-token"})
    assert result.status_code==503
    assert result.json()=={"detail":"authorization_unavailable"}
def test_logout_revokes_active_session_after_authenticated_access():
    access=login("a");h={"Authorization":"Bearer "+access}
    assert client.get("/v1/me",headers=h).status_code==200
    assert client.post("/v1/auth/logout",headers=h).status_code==204
    assert client.get("/v1/me",headers=h).status_code==401
def test_tenant_isolation():
    assert [d["domain"] for d in client.get("/v1/domains",headers=hdr("a")).json()]==["a.example.com"]
def test_api_key_revoke():
    h=hdr("a"); made=client.post("/v1/api-keys",headers=h,json={"name":"ci"}).json(); kh={"Authorization":"Bearer "+made["key"]}; assert client.get("/v1/domains",headers=kh).status_code==200; assert client.delete("/v1/api-keys/"+made["id"],headers=h).status_code==204; assert client.get("/v1/domains",headers=kh).status_code==401
def test_logout_revokes_active_session():
    token=login("a");headers={"Authorization":"Bearer "+token};assert client.post("/v1/auth/logout",headers=headers).status_code==204;assert client.get("/v1/me",headers=headers).status_code==401
def test_safe_send_and_suppression():
    h={**hdr("a"),"Idempotency-Key":"send-1"}; x={"to":"ok@example.net","sender":"sender@a.example.com","subject":"test","html":"<p>test</p>"}; r=client.post("/v1/messages",headers=h,json=x); assert r.status_code==202 and r.json()["safe_mode"]; assert client.post("/v1/messages",headers=h,json=x).json()["id"]==r.json()["id"]
    with DB() as s:s.add(Suppression(id="s",tenant_id="a",email="blocked@example.net",reason="hard_bounce"));s.commit()
    x["to"]="blocked@example.net"; assert client.post("/v1/email/send",headers={**hdr("a"),"Idempotency-Key":"send-2"},json=x).status_code==422

def test_middleware_command_submit_readback_and_replay():
    h=middleware_hdr("a","command-correlation-0001","command-send-0001")
    payload={"command":"email.message.send.v1","tenant_id":"a","correlation_id":"command-correlation-0001","payload":{"to":"command@example.net","sender":"sender@a.example.com","subject":"command","html":"<p>command</p>"}}
    with patch("apps.gateway.app.main.emit_middleware",new=AsyncMock(return_value=True)):
        first=client.post("/v1/commands",headers=h,json=payload);replay=client.post("/v1/commands",headers=h,json=payload)
    assert first.status_code==202 and replay.status_code==202
    assert first.json()["command_id"]==replay.json()["command_id"]
    assert first.json()["state"]=="queued"
    command_id=first.json()["command_id"]
    read=client.get(f"/v1/operations/{command_id}",headers={"Authorization":h["Authorization"],"X-Klyrow-Tenant-Id":"a","X-Tenant-ID":"a"})
    assert read.status_code==200 and read.json()["result"]["status"]=="accepted"
    assert read.json()["state"]=="accepted"
    assert client.post("/v1/commands",headers={**h,"X-Tenant-ID":"b"},json=payload).status_code==403

def test_middleware_command_cancel_and_suppression_upsert():
    with DB() as s:
        s.add(Message(id="cancel-command-message",tenant_id="a",recipient="person@example.net",sender="sender@a.example.com",subject="cancel",status="queued"))
        s.add(EmailOutbox(id="cancel-command-outbox",tenant_id="a",message_id="cancel-command-message",payload="{}",state="pending"))
        s.commit()
    base=middleware_hdr("a","command-correlation-0002","command-cancel-0001")
    cancel=client.post("/v1/commands",headers=base,json={"command":"email.message.cancel.v1","payload":{"message_id":"cancel-command-message"}})
    assert cancel.status_code==202 and cancel.json()["state"]=="cancelled"
    upsert=client.post("/v1/commands",headers={**base,"X-Correlation-ID":"command-correlation-0003","Idempotency-Key":"command-suppress-0001"},json={"command":"email.suppression.upsert.v1","payload":{"email":"stop@example.net","reason":"manual"}})
    assert upsert.status_code==202 and upsert.json()["state"]=="completed"
    with DB() as s:
        assert s.get(Message,"cancel-command-message").status=="cancelled"
        assert s.get(EmailOutbox,"cancel-command-outbox").state=="cancelled"
        assert s.scalar(select(Suppression).where(Suppression.tenant_id=="a",Suppression.email=="stop@example.net")).reason=="manual"

def test_middleware_command_rejects_cancellation_after_submission_starts():
    with DB() as s:
        s.add(Message(id="inflight-command-message",tenant_id="a",recipient="person@example.net",sender="sender@a.example.com",subject="inflight",status="submitted"))
        s.add(EmailOutbox(id="inflight-command-outbox",tenant_id="a",message_id="inflight-command-message",payload="{}",state="sending"))
        s.commit()
    headers=middleware_hdr("a","command-correlation-inflight","command-cancel-inflight")
    response=client.post("/v1/commands",headers=headers,json={"command":"email.message.cancel.v1","payload":{"message_id":"inflight-command-message"}})
    assert response.status_code==409 and response.json()["detail"]=="provider_submission_requires_reconciliation"
    with DB() as s:
        assert s.get(Message,"inflight-command-message").status=="submitted"
        assert s.get(EmailOutbox,"inflight-command-outbox").state=="sending"
        operation=s.scalar(select(MiddlewareCommandOperation).where(MiddlewareCommandOperation.idempotency_key=="command-cancel-inflight"))
        assert operation.state=="failed" and operation.error=="provider_submission_requires_reconciliation"

def test_middleware_commands_require_command_scope_and_record_bad_payload():
    user_headers={**hdr("a"),"X-Tenant-ID":"a","X-Correlation-ID":"command-correlation-0004","Idempotency-Key":"command-scope-denied"}
    payload={"command":"email.message.send.v1","payload":{"sender":"sender@a.example.com","subject":"missing recipient","html":"<p>bad</p>"}}
    assert client.post("/v1/commands",headers=user_headers,json=payload).status_code==403
    service_headers=middleware_hdr("a","command-correlation-0005","command-invalid-payload")
    response=client.post("/v1/commands",headers=service_headers,json=payload)
    assert response.status_code==422 and response.json()["detail"]=="invalid_command_payload"
    with DB() as s:
        operation=s.scalar(select(MiddlewareCommandOperation).where(MiddlewareCommandOperation.idempotency_key=="command-invalid-payload"))
        assert operation and operation.state=="failed" and operation.error=="invalid_command_payload"

def test_middleware_send_readback_persists_unknown_outcome_without_retry():
    with DB() as s:
        s.add(Message(id="unknown-command-message",tenant_id="a",recipient="person@example.net",sender="sender@a.example.com",subject="unknown",status="indeterminate"))
        s.add(EmailOutbox(id="unknown-command-outbox",tenant_id="a",message_id="unknown-command-message",payload="{}",state="INDETERMINATE"))
        s.add(MiddlewareCommandOperation(command_id="unknown-command",tenant_id="a",command="email.message.send.v1",idempotency_key="unknown-command-key",correlation_id="unknown-command-correlation",request_hash="hash",state="queued",result_json='{"id":"unknown-command-message"}'))
        s.commit()
    headers=middleware_hdr("a","unknown-command-correlation","unused-read-key")
    result=client.get("/v1/operations/unknown-command",headers=headers)
    assert result.status_code==200 and result.json()["state"]=="unknown_outcome"
    with DB() as s:
        assert s.get(MiddlewareCommandOperation,"unknown-command").state=="unknown_outcome"
        assert s.get(EmailOutbox,"unknown-command-outbox").state=="INDETERMINATE"

def test_concurrent_middleware_command_insert_returns_durable_winner():
    class ConcurrentSession:
        def __init__(self):self.calls=0;self.winner=None;self.rolled_back=False
        def scalar(self,statement):
            del statement;self.calls+=1
            return None if self.calls==1 else self.winner
        def add(self,item):
            if isinstance(item,MiddlewareCommandOperation):self.winner=item
        def commit(self):raise IntegrityError("INSERT middleware_command_operations",{},RuntimeError("concurrent unique key"))
        def rollback(self):self.rolled_back=True
    session=ConcurrentSession()
    payload=MiddlewareCommandIn(command="email.reputation.snapshot.request.v1",payload={})
    response=asyncio.run(middleware_command(payload,{"tenant":"a","sub":"middleware-service","service":True,"identity_type":"SERVICE","permissions":["klyrow.middleware.command.write"]},session,"concurrent-command-key","a","concurrent-correlation"))
    assert response["command_id"]==session.winner.command_id and response["state"]=="accepted"
    assert session.rolled_back is True

def test_interrupted_middleware_command_replays_from_durable_request():
    request={"payload":{"to":"recovered@example.net","sender":"sender@a.example.com","subject":"recovered","html":"<p>recovered</p>"},"target":None}
    with DB() as s:
        s.add(MiddlewareCommandOperation(command_id="recovered-command",tenant_id="a",command="email.message.send.v1",idempotency_key="recovered-command-key",correlation_id="recovered-command-correlation",request_hash="durable-request-hash",request_json=json.dumps(request,separators=(",",":"),sort_keys=True),state="accepted"))
        s.commit()
    assert asyncio.run(recover_middleware_commands())>=1
    with DB() as s:
        operation=s.get(MiddlewareCommandOperation,"recovered-command")
        result=json.loads(operation.result_json)
        assert operation.state=="queued" and result["status"]=="accepted"
        assert s.scalar(select(Message).where(Message.id==result["id"],Message.tenant_id=="a"))
    assert asyncio.run(recover_middleware_commands())==0
def test_unapproved_local_part_is_denied():
    x={"to":"ok@example.net","sender":"admin@a.example.com","subject":"test","html":"<p>test</p>"}
    assert client.post("/v1/email/send",headers={**hdr("a"),"Idempotency-Key":"unapproved-local"},json=x).status_code==403

def test_idempotency_key_is_tenant_scoped_and_changed_payload_conflicts():
    payload_a={"to":"a@example.net","sender":"sender@a.example.com","subject":"same","html":"<p>a</p>"}
    payload_b={"to":"b@example.net","sender":"sender@b.example.com","subject":"same","html":"<p>b</p>"}
    assert client.post("/v1/email/send",headers={**hdr("a"),"Idempotency-Key":"shared-key"},json=payload_a).status_code==202
    assert client.post("/v1/email/send",headers={**hdr("b"),"Idempotency-Key":"shared-key"},json=payload_b).status_code==202
    changed={**payload_a,"subject":"changed"};r=client.post("/v1/email/send",headers={**hdr("a"),"Idempotency-Key":"shared-key"},json=changed)
    assert r.status_code==409 and r.json()["detail"]=="idempotency_key_payload_mismatch"

def test_exact_replay_precedes_mutable_send_guards():
    payload={"to":"stable-replay@example.net","sender":"sender@a.example.com","subject":"stable","html":"<p>stable</p>"}
    headers={**hdr("a"),"Idempotency-Key":"stable-policy-replay"}
    with patch("apps.gateway.app.main.emit_middleware",new=AsyncMock(return_value=True)):
        first=client.post("/v1/email/send",headers=headers,json=payload)
    assert first.status_code==202
    try:
        with DB() as s:
            s.add(Suppression(id="stable-policy-suppression",tenant_id="a",email="stable-replay@example.net",reason="hard_bounce"))
            s.get(Tenant,"a").quota=0
            s.commit()
        with patch("apps.gateway.app.main.emit_middleware",new=AsyncMock(return_value=True)):
            replay=client.post("/v1/email/send",headers=headers,json=payload)
            conflict=client.post("/v1/email/send",headers=headers,json={**payload,"subject":"changed"})
        assert replay.status_code==202 and replay.json()==first.json()
        assert conflict.status_code==409 and conflict.json()["detail"]=="idempotency_key_payload_mismatch"
    finally:
        with DB() as s:
            suppression=s.get(Suppression,"stable-policy-suppression")
            if suppression:s.delete(suppression)
            s.get(Tenant,"a").quota=10
            s.commit()

def test_idempotency_key_is_tenant_scoped():
    body={"to":"same@example.net","subject":"same","html":"<p>same</p>"}
    for tenant in ("a","b"):
        payload={**body,"sender":f"sender@{tenant}.example.com"}
        assert client.post("/v1/email/send",headers={**hdr(tenant),"Idempotency-Key":"tenant-scope-key"},json=payload).status_code==202

def test_production_startup_fails_without_session_secret():
    env={**os.environ,"KLYROW_ENV":"production"};env.pop("KLYROW_SESSION_SECRET",None);env.pop("KLYROW_SESSION_SECRET_FILE",None)
    result=subprocess.run([sys.executable,"-c","import apps.gateway.app.main"],env=env,capture_output=True,text=True)
    assert result.returncode!=0 and "production requires KLYROW_SESSION_SECRET_FILE" in result.stderr

def test_production_runtime_secrets_require_independent_files(tmp_path,monkeypatch):
    monkeypatch.setenv("KLYROW_ENV","production")
    monkeypatch.setenv("KLYROW_MIDDLEWARE_API_KEY","environment-secret-is-denied")
    monkeypatch.delenv("KLYROW_MIDDLEWARE_API_KEY_FILE",raising=False)
    with pytest.raises(RuntimeError,match="production requires KLYROW_MIDDLEWARE_API_KEY_FILE"):
        runtime_secret("KLYROW_MIDDLEWARE_API_KEY")
    secret_file=tmp_path/"middleware-api-key"
    secret_file.write_text("file-backed-test-secret",encoding="utf-8")
    monkeypatch.setenv("KLYROW_MIDDLEWARE_API_KEY_FILE",str(secret_file))
    assert runtime_secret("KLYROW_MIDDLEWARE_API_KEY")=="file-backed-test-secret"

def test_production_runtime_secret_rejects_symlink(tmp_path,monkeypatch):
    target=tmp_path/"webhook-secret"
    target.write_text("file-backed-test-secret",encoding="utf-8")
    link=tmp_path/"webhook-secret-link"
    link.symlink_to(target)
    monkeypatch.setenv("KLYROW_ENV","production")
    monkeypatch.setenv("KLYROW_WEBHOOK_SECRET_FILE",str(link))
    with pytest.raises(RuntimeError,match="must be absolute and not a symlink"):
        runtime_secret("KLYROW_WEBHOOK_SECRET")

def test_webhook_ssrf_targets_are_rejected():
    assert client.post("/v1/webhooks",headers=hdr("a"),json={"url":"https://127.0.0.1/hook"}).status_code==422

def test_beyvra_service_scope_sender_policy_and_idempotency():
    base={"Authorization":"Bearer bounded-beyvra-test-token","X-Service-Identity":"codestra-server-a:beyvra-email-production","X-Service-Scopes":"email.send email.status","Idempotency-Key":"beyvra-1"}
    payload={"to":"synthetic@example.net","sender":"support@beyvra.com","subject":"Synthetic","html":"<p>Synthetic</p>","text":"Synthetic","stream":"transactional"}
    assert client.post("/v1/internal/email/beyvra/send",headers={**base,"X-Service-Scopes":"email.status"},json=payload).status_code==403
    assert client.post("/v1/internal/email/beyvra/send",headers={**base,"Idempotency-Key":"beyvra-spoof"},json={**payload,"sender":"spoof@beyvra.com"}).status_code==403
    with DB() as s:s.add(Domain(id="beyvra-domain",tenant_id="a",domain="beyvra.com",token="fixture",verified=True));s.add(AllowedSender(id="beyvra-support",tenant_id="a",address="support@beyvra.com",role="support"));s.commit()
    with patch("apps.gateway.app.main.emit_middleware",new=AsyncMock(return_value=True)):
        first=client.post("/v1/internal/email/beyvra/send",headers=base,json=payload);second=client.post("/v1/internal/email/beyvra/send",headers=base,json=payload)
    assert first.status_code==202 and first.json()["provider_message_id"]==second.json()["provider_message_id"]
def test_contacts_campaigns_and_isolation():
    a,b=hdr("a"),hdr("b"); assert client.post("/v1/contacts",headers=a,json={"email":"one@example.net","name":"One"}).status_code==200; assert len(client.get("/v1/contacts",headers=a).json())==1; assert client.get("/v1/contacts",headers=b).json()==[]
    c=client.post("/v1/campaigns",headers={**a,"Idempotency-Key":"campaign-1"},json={"name":"Mock campaign","subject":"Test"}); assert c.status_code==201; cid=c.json()["id"]; assert client.get("/v1/campaigns/"+cid,headers=b).status_code==404
def test_webhook_signature_and_replay():
    body=json.dumps({"event":"email.delivered","message_id":"m","tenant_id":"a"},separators=(",",":")); ts=str(int(time.time())); eid=str(uuid.uuid4()); sig=hmac.new(b"hook-secret",f"{ts}.{eid}.{body}".encode(),hashlib.sha256).hexdigest(); h={"X-Klyrow-Timestamp":ts,"X-Klyrow-Event-Id":eid,"X-Klyrow-Signature":sig,"Content-Type":"application/json"}
    with patch("apps.gateway.app.main.emit_middleware",new=AsyncMock(return_value=True)):
        assert client.post("/v1/webhooks/postal",headers=h,content=body).status_code==202; replay=client.post("/v1/webhooks/postal",headers=h,content=body); assert replay.status_code==202 and replay.json()["duplicate"] is True
    h["X-Klyrow-Signature"]="bad"; assert client.post("/v1/webhooks/postal",headers=h,content=body).status_code==401

def test_webhook_status_suppression_audit_and_duplicate():
    with DB() as s:
        s.add(Message(id="event-message",tenant_id="a",recipient="bounce@example.net",sender="sender@a.example.com",subject="synthetic",status="accepted"));s.commit()
    body=json.dumps({"event":"email.bounced","message_id":"event-message","correlation_id":"event-message","tenant_id":"a","recipient":"bounce@example.net","status":"HardBounce"},separators=(",",":"));ts=str(int(time.time()));eid=str(uuid.uuid4());sig=hmac.new(b"hook-secret",f"{ts}.{eid}.{body}".encode(),hashlib.sha256).hexdigest();h={"X-Klyrow-Timestamp":ts,"X-Klyrow-Event-Id":eid,"X-Klyrow-Signature":sig,"Content-Type":"application/json"}
    with patch("apps.gateway.app.main.emit_middleware",new=AsyncMock(return_value=True)):
        first=client.post("/v1/webhooks/postal",headers=h,content=body);duplicate=client.post("/v1/webhooks/postal",headers=h,content=body)
    assert first.status_code==202 and duplicate.json()["duplicate"] is True
    with DB() as s:
        assert s.get(Message,"event-message").status=="bounced"
        assert len(list(s.scalars(select(Suppression).where(Suppression.tenant_id=="a",Suppression.email=="bounce@example.net"))))==1
        assert s.get(Event,eid) is not None
        assert s.scalar(select(Audit).where(Audit.action=="email.status.bounced")) is not None

def test_postal_native_signature_normalization_and_idempotency(tmp_path):
    private=rsa.generate_private_key(public_exponent=65537,key_size=2048); public=tmp_path/"postal-public.pem"; public.write_bytes(private.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)); os.environ.update(KLYROW_POSTAL_WEBHOOK_PUBLIC_KEY=str(public),KLYROW_POSTAL_TENANT_ID="a")
    payload={"event":"MessageDeliveryFailed","timestamp":time.time(),"uuid":str(uuid.uuid4()),"payload":{"message":{"id":1,"message_id":"provider-message","tag":"correlation-1","from":"sender@a.example.com","to":"person@example.net"},"status":"HardFail"}}
    body=json.dumps(payload,separators=(",",":")).encode(); sig=base64.b64encode(private.sign(body,padding.PKCS1v15(),hashes.SHA256())).decode(); headers={"X-Postal-Signature-256":sig,"Content-Type":"application/json"}
    with patch("apps.gateway.app.main.emit_middleware",new=AsyncMock(return_value=True)) as emit:
        first=client.post("/v1/webhooks/postal-native",headers=headers,content=body); duplicate=client.post("/v1/webhooks/postal-native",headers=headers,content=body)
    assert first.status_code==202 and duplicate.status_code==202 and duplicate.json()["duplicate"] is True
    sent=emit.await_args.args; assert sent[0]=="klyrow.email.bounced" and sent[1]["correlation_id"]=="correlation-1"
    assert sent[1]["canonical_status"]=="bounced"
    with DB() as s:
        stored=json.loads(s.get(PostalEvent,payload["uuid"]).payload)
        assert stored["canonical_status"]=="bounced"
    assert client.post("/v1/webhooks/postal-native",headers={"X-Postal-Signature-256":"bad"},content=body).status_code==401


def test_postal_outage_retries_without_loss_or_second_canary_claim(tmp_path,monkeypatch):
    from apps.gateway.app import main as gateway
    from datetime import datetime,timezone,timedelta
    key_file=tmp_path/"postal-key";key_file.write_text("synthetic-key")
    monkeypatch.setattr(gateway,"SAFE_MODE",False)
    monkeypatch.setenv("KLYROW_POSTAL_API_KEY_FILE",str(key_file))
    monkeypatch.setenv("KLYROW_POSTAL_API_URL","https://postal.invalid")
    monkeypatch.setenv("KLYROW_POSTAL_API_HOST_HEADER","app.klyrow.com")
    monkeypatch.setenv("KLYROW_CANARY_ALLOWED_DOMAIN","a.example.com")
    monkeypatch.setenv("KLYROW_CANARY_ALLOWED_SENDER","sender@a.example.com")
    monkeypatch.setenv("KLYROW_CANARY_ALLOWED_RECIPIENT","sink@example.net")
    monkeypatch.setenv("KLYROW_CANARY_ALLOWED_CAMPAIGN","COD-WEB-OUT")
    monkeypatch.setenv("KLYROW_CANARY_MAX_DELIVERIES","1")
    payload={"to":["sink@example.net"],"from":"sender@a.example.com","subject":"outage",
        "plain_body":"fixture","campaign_id":"COD-WEB-OUT"}
    with DB() as s:
        s.merge(gateway.ProductionCanaryGate(gate_key=gateway.canary_gate_key(),reserved_deliveries=1,claimed_deliveries=0))
        s.add(Message(id="postal-outage-message",tenant_id="a",recipient="sink@example.net",
            sender="sender@a.example.com",subject="outage",status="queued"))
        s.add(gateway.EmailOutbox(id="postal-outage-outbox",tenant_id="a",message_id="postal-outage-message",
            payload=json.dumps(payload),state="pending",attempts=0))
        s.commit()
    class FailedClient:
        async def __aenter__(self):return self
        async def __aexit__(self,*_):return False
        async def post(self,*_,**__):raise RuntimeError("synthetic_provider_unavailable")
    with patch.object(gateway.asyncio,"sleep",new=AsyncMock(side_effect=[None,asyncio.CancelledError()])), \
         patch.object(gateway.httpx,"AsyncClient",return_value=FailedClient()):
        with pytest.raises(asyncio.CancelledError):asyncio.run(gateway.email_outbox_loop())
    with DB() as s:
        item=s.get(gateway.EmailOutbox,"postal-outage-outbox");gate=s.get(gateway.ProductionCanaryGate,gateway.canary_gate_key())
        assert item.state=="retry" and item.attempts==1 and s.get(Message,"postal-outage-message").status=="deferred"
        assert gate.claimed_deliveries==1
        item.next_attempt_at=datetime.now(timezone.utc)-timedelta(seconds=1);s.commit()
    response=type("Response",(),{"raise_for_status":lambda self:None,
        "json":lambda self:{"data":{"message_id":"synthetic-provider-id"}}})()
    class RecoveredClient(FailedClient):
        async def post(self,*_,**kwargs):
            assert kwargs["headers"]["Host"]=="app.klyrow.com"
            return response
    with patch.object(gateway.asyncio,"sleep",new=AsyncMock(side_effect=[None,asyncio.CancelledError()])), \
         patch.object(gateway.httpx,"AsyncClient",return_value=RecoveredClient()):
        with pytest.raises(asyncio.CancelledError):asyncio.run(gateway.email_outbox_loop())
    with DB() as s:
        item=s.get(gateway.EmailOutbox,"postal-outage-outbox");gate=s.get(gateway.ProductionCanaryGate,gateway.canary_gate_key())
        assert item.state=="delivered" and item.provider_message_id=="synthetic-provider-id" and item.attempts==2
        assert s.get(Message,"postal-outage-message").status=="provider_accepted" and gate.claimed_deliveries==1
