import os
import socket
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.gateway.app.main import AllowedSender, Base, DB, Message, Tenant, User, app, engine, ph, rate_buckets
from apps.gateway.app import messaging

client=TestClient(app)
tokens={}

def setup_module():
    rate_buckets.clear()
    tokens.clear();Base.metadata.drop_all(engine);Base.metadata.create_all(engine)
    with DB() as s:
        for tid in ("a","b"):
            s.add(Tenant(id=tid,name=tid,quota=10000));s.add(User(id=tid,tenant_id=tid,email=f"{tid}@example.com",password_hash=ph.hash("long-enough-password"),role="tenant_admin"))
        s.commit()
def headers(tid):
    if tid in tokens:return tokens[tid]
    response=client.post("/v1/auth/login",json={"email":f"{tid}@example.com","password":"long-enough-password"});assert response.status_code==200;tokens[tid]={"Authorization":"Bearer "+response.json()["access_token"]};return tokens[tid]

def claim_domain(tid="a",domain="tenant-a.example"):
    h=headers(tid);created=client.post("/v1/domains/claims",headers=h,json={"domain":domain});assert created.status_code==201,created.text
    record=created.json()["dns"]["ownership"]["value"];messaging.resolve_txt=lambda _name:[record];verified=client.post(f"/v1/domains/claims/{created.json()['id']}/verify",headers=h,json={"challenge":record.split("=",1)[1]});assert verified.status_code==200
    return h,created.json()["id"]

def test_domain_global_ownership_dkim_rotation_and_sender_spoof_denial(tmp_path):
    h,claim=claim_domain();assert client.post("/v1/domains/claims",headers=headers("b"),json={"domain":"tenant-a.example"}).status_code==409
    with patch.dict(os.environ,{"KLYROW_DKIM_KEY_DIR":str(tmp_path)}):
        rotated=client.post(f"/v1/domains/claims/{claim}/dkim/rotate",headers=h)
    assert rotated.status_code==201 and rotated.json()["private_key_exported"] is False
    denied=client.post("/v1/senders",headers=h,json={"domain_claim_id":claim,"email":"spoof@other.example","display_name":"Spoof","stream":"MARKETING"});assert denied.status_code==403
    sender=client.post("/v1/senders",headers=h,json={"domain_claim_id":claim,"email":"news@tenant-a.example","display_name":"News","stream":"MARKETING"});assert sender.status_code==201
    with DB() as s:assert s.scalar(__import__('sqlalchemy').select(AllowedSender).where(AllowedSender.tenant_id=="a",AllowedSender.address=="news@tenant-a.example",AllowedSender.enabled==True))
    assert client.post("/v1/senders",headers=headers("b"),json={"domain_claim_id":claim,"email":"bad@tenant-a.example","display_name":"Bad","stream":"MARKETING"}).status_code==404
    assert {row["domain"] for row in client.get("/v1/domains/claims",headers=h).json()}=={"tenant-a.example"}
    assert {row["address"] for row in client.get("/v1/senders",headers=h).json()}=={"news@tenant-a.example"}
    assert client.get("/v1/domains/claims",headers=headers("b")).json()==[]

def test_stream_separation_template_version_render_rollback_and_campaign_safety():
    h=headers("a")
    claim_response=client.post("/v1/domains/claims",headers=h,json={"domain":"campaign.example"});record=claim_response.json()["dns"]["ownership"]["value"];messaging.resolve_txt=lambda _name:[record];claim=claim_response.json()["id"];client.post(f"/v1/domains/claims/{claim}/verify",headers=h,json={"challenge":record.split("=",1)[1]})
    sender=client.post("/v1/senders",headers=h,json={"domain_claim_id":claim,"email":"campaign@campaign.example","display_name":"Campaign","stream":"MARKETING"}).json()["id"]
    assert client.post("/v1/streams",headers=h,json={"name":"security","kind":"SECURITY","rate_limit":100,"retention_days":90,"suppression_policy":"MARKETING_GLOBAL"}).status_code==422
    assert client.post("/v1/streams",headers=h,json={"name":"marketing","kind":"MARKETING","rate_limit":100,"retention_days":90,"suppression_policy":"MARKETING_GLOBAL"}).status_code==201
    template=client.post("/v1/templates",headers=h,json={"slug":"welcome","name":"Welcome","subject":"Hello {{name}}","html_body":"<h1>Hello {{name}}</h1>","text_body":"Hello {{name}}","variables":["name"]});assert template.status_code==201
    tid=template.json()["id"];updated=client.put(f"/v1/templates/{tid}",headers=h,json={"subject":"Welcome {{name}}","html_body":"<p>Welcome {{name}}</p>","text_body":"Welcome {{name}}","variables":["name"]});assert updated.json()["version"]==2
    rendered=client.post(f"/v1/templates/{tid}/render",headers=h,json={"variables":{"name":"<Alice>"}});assert "&lt;Alice&gt;" in rendered.json()["html"]
    rollback=client.post(f"/v1/templates/{tid}/rollback/1",headers=h);assert rollback.json()["source_version"]==1
    client.post(f"/v1/templates/{tid}/publish",headers=h)
    campaign=client.post("/v1/campaign-definitions",headers=h,json={"name":"Launch","sender_id":sender,"template_id":tid,"timezone":"UTC"});assert campaign.status_code==201,campaign.text;cid=campaign.json()["id"]
    assert client.post(f"/v1/campaign-definitions/{cid}/preflight",headers=h,json={"estimated_recipients":10,"estimated_suppressed":1,"estimated_invalid":1,"quota_remaining":100}).status_code==409
    test=client.post(f"/v1/campaign-definitions/{cid}/test",headers=h);assert test.json()["provider_submission"] is False
    preflight=client.post(f"/v1/campaign-definitions/{cid}/preflight",headers=h,json={"estimated_recipients":10,"estimated_suppressed":1,"estimated_invalid":1,"quota_remaining":100,"estimated_unit_cost":"0.001"});assert preflight.json()["eligible"]==8 and preflight.json()["allowed"] is True
    scheduled=client.post(f"/v1/campaign-definitions/{cid}/schedule",headers=h,json={"scheduled_at":(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()})
    assert scheduled.status_code==409 and scheduled.json()["detail"]=="campaign_dispatcher_unavailable"
    assert client.post(f"/v1/campaign-definitions/{cid}/cancel",headers=h).json()["status"]=="CANCELLED"
    assert len(client.get("/v1/templates",headers=h).json())==1
    assert len(client.get("/v1/streams",headers=h).json())==1
    assert len(client.get("/v1/campaign-definitions",headers=h).json())==1
    assert client.get("/v1/templates",headers=headers("b")).json()==[]

def test_echoed_domain_challenge_without_dns_control_is_denied():
    h=headers("a");created=client.post("/v1/domains/claims",headers=h,json={"domain":"unowned.example"});record=created.json()["dns"]["ownership"]["value"]
    messaging.resolve_txt=lambda _name:[]
    response=client.post(f"/v1/domains/claims/{created.json()['id']}/verify",headers=h,json={"challenge":record.split("=",1)[1]})
    assert response.status_code==422 and response.json()["detail"]=="dns_ownership_not_verified"

def test_exact_inbound_routing_duplicate_protection_and_quarantine():
    h,claim=claim_domain("a","inbound.example")
    route=client.post("/v1/inbound/routes",headers=h,json={"domain_claim_id":claim,"recipient":"support@inbound.example","destination_kind":"SUPPORT","destination_ref":"queue:support"});assert route.status_code==201
    base={"recipient":"support@inbound.example","sender":"person@outside.example","message_id":"<message-1@outside.example>","references":["<root@outside.example>"],"headers":{"X-Test":"safe"},"attachments":[],"size_bytes":1000,"spam_score":1,"malware_status":"CLEAN"}
    accepted=client.post("/v1/inbound/fixtures",headers=h,json=base);duplicate=client.post("/v1/inbound/fixtures",headers=h,json=base);assert accepted.json()["state"]=="ROUTED" and duplicate.json()["duplicate"] is True
    infected=client.post("/v1/inbound/fixtures",headers=h,json={**base,"message_id":"<message-2@outside.example>","malware_status":"INFECTED"});assert infected.json()["state"]=="QUARANTINED"
    assert client.post("/v1/inbound/fixtures",headers=headers("b"),json=base).status_code==404
    assert len(client.get("/v1/inbound/routes",headers=h).json())==1
    assert len(client.get("/v1/inbound/messages",headers=h).json())==2
    assert client.get("/v1/inbound/messages",headers=headers("b")).json()==[]

def test_webhook_event_idempotency_and_delivery_retry_policy():
    h=headers("a")
    # Exercise the real URL/IP policy with a deterministic public DNS answer.
    addresses=[(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,"",("93.184.216.34",443))]
    with patch("apps.gateway.app.main.socket.getaddrinfo",return_value=addresses):
        webhook=client.post("/v1/webhook-subscriptions",headers=h,json={"url":"https://example.com/events","events":["message.delivered"]})
    assert webhook.status_code==201,webhook.text;wid=webhook.json()["id"]
    event={"event_id":"event-00000001","event_type":"message.delivered","payload":{"message_id":"m"}}
    first=client.post(f"/v1/webhook-subscriptions/{wid}/test",headers=h,json=event);duplicate=client.post(f"/v1/webhook-subscriptions/{wid}/test",headers=h,json=event);assert first.status_code==202 and duplicate.json()["duplicate"] is True
    listed=client.get("/v1/webhook-subscriptions",headers=h).json();assert listed[0]["id"]==wid and "secret_hash" not in listed[0] and "encrypted_secret_ref" not in listed[0]
    with DB() as s:s.add(Message(id="retry-message",tenant_id="a",recipient="r@example.com",sender="s@example.com",subject="x",status="QUEUED"));s.commit()
    job=client.post("/v1/delivery-jobs/retry-message",headers=h).json();leased=client.post(f"/v1/delivery-jobs/{job['id']}/lease",headers=h,json={"worker_id":"worker-1","lease_seconds":30});assert leased.json()["state"]=="PROCESSING"
    temporary=client.post(f"/v1/delivery-jobs/{job['id']}/fail",headers=h,json={"error_class":"NETWORK_FAILURE"});assert temporary.json()["state"]=="RETRY"
    permanent=client.post(f"/v1/delivery-jobs/{job['id']}/fail",headers=h,json={"error_class":"HARD_BOUNCE"});assert permanent.json()["state"]=="DEAD_LETTER" and permanent.json()["retryable"] is False
