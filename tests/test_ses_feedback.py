"""No-network tests for signed SES/SNS feedback and tenant-bound outcomes."""
import base64
import json
from datetime import datetime, timezone, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.x509.oid import NameOID
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.gateway.app.ses_feedback import (
    SESFeedback, SESFeedbackDenied, verified_ses_event, reconcile_feedback,
)

TOPIC = "arn:aws:sns:us-east-1:123456789012:codestra-klyrow-ses-events"
CERT_URL = "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-a1b2c3d4.pem"
SEND_ID = "0100000000000000000012345abc"
SNS_ID = "08a1b2c3-d4e5-4f67-b890-123456789abc"


@pytest.fixture
def signer():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SNS fixture")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(priv.public_key())
            .serial_number(123456789).not_valid_before(now-timedelta(days=1))
            .not_valid_after(now+timedelta(days=1)).sign(priv, hashes.SHA256()))
    return priv, cert.public_bytes(serialization.Encoding.PEM)


def signed_body(signer, *, event="Delivery", subject=False, changes=None, envelope_changes=None, version="2"):
    private, _ = signer
    content = {
        "eventType": event,
        "mail": {"messageId": SEND_ID, "source": "noreply@codestra.agency",
                 "destination": ["recipient@example.net"],
                 "tags": {"ses:configuration-set": ["codestra-klyrow-transactional"]}},
    }
    if event == "Bounce":
        content["bounce"] = {"bounceType":"Permanent","bouncedRecipients":[{"emailAddress":"recipient@example.net","status":"5.1.1"}]}
    if event == "Complaint":
        content["complaint"] = {"complainedRecipients":[{"emailAddress":"recipient@example.net"}]}
    if changes:changes(content)
    envelope = {
        "Type": "Notification", "MessageId":SNS_ID, "TopicArn":TOPIC,
        "Timestamp":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
        "Message": json.dumps(content,separators=(",",":")),
        "SignatureVersion":version, "SigningCertURL":CERT_URL,
    }
    if subject:envelope["Subject"]="SES Event"
    if envelope_changes:envelope_changes(envelope)
    signed = "".join(k+"\n"+str(envelope[k])+"\n" for k in (
        "Message","MessageId","Subject","Type","Timestamp","TopicArn"
    ) if k in envelope).encode()
    signature = private.sign(signed,padding.PKCS1v15(),hashes.SHA256() if version=="2" else hashes.SHA1())
    envelope["Signature"]=base64.b64encode(signature).decode()
    return json.dumps(envelope)


@pytest.mark.parametrize("event,status", [
    ("Send","provider_accepted"),("Delivery","delivered"),("Bounce","bounced"),
    ("Complaint","complained"),("Reject","failed"),("DeliveryDelay","deferred"),
    ("Rendering Failure","failed"),
])
def test_real_sns_signature_event_types(signer,event,status):
    feedback=verified_ses_event(signed_body(signer,event=event),topic_arn=TOPIC,certificate_loader=lambda _:signer[1])
    assert feedback.status==status and feedback.sender=="noreply@codestra.agency"


def test_legacy_sns_v1_still_requires_valid_rsa(signer):
    event=verified_ses_event(signed_body(signer,version="1"),topic_arn=TOPIC,certificate_loader=lambda _:signer[1])
    assert event.status=="delivered"


def test_optional_subject_has_canonical_signing(signer):
    event=verified_ses_event(signed_body(signer,subject=True),topic_arn=TOPIC,certificate_loader=lambda _:signer[1])
    assert event.event_type=="Delivery"


@pytest.mark.parametrize("change,matcher", [
    (lambda e:e.update({"TopicArn":"arn:aws:sns:us-west-2:123456789012:codestra-klyrow-ses-events"}),"topic"),
    (lambda e:e.update({"SigningCertURL":"https://evil.example.com/cert.pem"}),"certificate"),
    (lambda e:e.update({"Message":"{}"}),"signature"),
    (lambda e:e.update({"Signature":"QUFBQQ=="}),"signature"),
    (lambda e:e.update({"SignatureVersion":"9"}),"version"),
    (lambda e:e.update({"Type":"SubscriptionConfirmation"}),"topic"),
])
def test_modified_envelope_is_rejected(signer,change,matcher):
    body=signed_body(signer)
    env=json.loads(body);change(env)
    with pytest.raises(SESFeedbackDenied,match=matcher):
        verified_ses_event(json.dumps(env),topic_arn=TOPIC,certificate_loader=lambda _:signer[1])


def test_unrelated_sns_topic_denied_even_when_correctly_signed(signer):
    body=signed_body(signer,envelope_changes=lambda e:e.update({"TopicArn":"arn:aws:sns:us-east-1:123456789012:other-topic"}))
    with pytest.raises(SESFeedbackDenied,match="topic"):
        verified_ses_event(body,topic_arn=TOPIC,certificate_loader=lambda _:signer[1])


def test_sns_subject_nested_json_untrusted(signer):
    def unexpected(p):p["mail"]["tags"]={"ses:configuration-set":["another-config"]}
    body=signed_body(signer,changes=unexpected)
    with pytest.raises(SESFeedbackDenied,match="config_set"):
        verified_ses_event(body,topic_arn=TOPIC,certificate_loader=lambda _:signer[1])


def test_ses_sender_and_recipient_mismatch(signer):
    for fn in (
        lambda e:e["mail"].update({"source":"attacker@evil.example"}),
        lambda e:e["mail"].update({"destination":["victim@example.net","recipient@example.net"]}),
        lambda e:e.update({"eventType":"Untracked"}),
    ):
        body=signed_body(signer,changes=fn)
        with pytest.raises(SESFeedbackDenied):
            verified_ses_event(body,topic_arn=TOPIC,certificate_loader=lambda _:signer[1])


def test_transient_bounce_must_not_suppress(signer):
    body=signed_body(signer,event="Bounce",changes=lambda e:e["bounce"].update({"bounceType":"Transient"}))
    assert verified_ses_event(body,topic_arn=TOPIC,certificate_loader=lambda _:signer[1]).status=="deferred"


def _session_db():
    from apps.gateway.app.main import Base
    engine=create_engine("sqlite://",connect_args={"check_same_thread":False},poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(engine,expire_on_commit=False)


def _seed_db(factory):
    from apps.gateway.app.main import Tenant, Message, EmailOutbox
    with factory() as s:
        s.add(Tenant(id="tenant-a",name="A",quota=100))
        s.add(Tenant(id="tenant-b",name="B",quota=100))
        s.add(Message(id="local-message",tenant_id="tenant-a",recipient="recipient@example.net",
                      sender="noreply@codestra.agency",subject="Example",status="provider_accepted"))
        s.add(EmailOutbox(id="outbox",tenant_id="tenant-a",message_id="local-message",
                          provider_message_id=SEND_ID,payload="{}",state="delivered"))
        s.commit()


def _event(status, *, sns_id=SNS_ID, sender="noreply@codestra.agency", recipient="recipient@example.net"):
    return SESFeedback(sns_message_id=sns_id,provider_message_id=SEND_ID,event_type=status,
        status=status,sender=sender,recipient=recipient,raw_status="",permanent_bounce=status=="bounced")


def test_unmatched_event_does_not_mutate_other_tenant():
    from apps.gateway.app.main import Message,Event
    factory=_session_db();_seed_db(factory)
    assert reconcile_feedback(_event("bounced",recipient="other@example.net"),session_factory=factory)=="unmatched"
    with factory() as s:
        assert s.get(Message,"local-message").status=="provider_accepted"
        assert s.query(Event).count()==0


def test_verified_delivery_ack_and_duplicate():
    from apps.gateway.app.main import Message,Event,Replay
    factory=_session_db();_seed_db(factory)
    e=_event("delivered")
    assert reconcile_feedback(e,session_factory=factory)=="ack"
    assert reconcile_feedback(e,session_factory=factory)=="duplicate"
    with factory() as s:
        assert s.get(Message,"local-message").status=="delivered"
        assert s.query(Event).count()==1 and s.query(Replay).count()==1


def test_bounce_suppresses_only_matched_tenant():
    from apps.gateway.app.main import Suppression,Message
    factory=_session_db();_seed_db(factory)
    assert reconcile_feedback(_event("bounced"),session_factory=factory)=="ack"
    with factory() as s:
        suppression=s.query(Suppression).all()
        assert len(suppression)==1 and suppression[0].tenant_id=="tenant-a"
        assert s.get(Message,"local-message").status=="bounced"


def test_late_send_never_overwrites_delivery():
    from apps.gateway.app.main import Message
    factory=_session_db();_seed_db(factory)
    assert reconcile_feedback(_event("delivered"),session_factory=factory)=="ack"
    assert reconcile_feedback(_event("provider_accepted",sns_id="99887766-5544-4eea-aaaa-111122223333"),session_factory=factory)=="ack"
    with factory() as s:assert s.get(Message,"local-message").status=="delivered"


def test_duplicate_provider_id_across_tenants_fail_closed():
    from apps.gateway.app.main import Tenant, Message, EmailOutbox, Event
    factory=_session_db();_seed_db(factory)
    with factory() as s:
        s.add(Message(id="other-message",tenant_id="tenant-b",recipient="other@example.net",
                      sender="noreply@codestra.agency",subject="Other",status="provider_accepted"))
        s.add(EmailOutbox(id="other-outbox",tenant_id="tenant-b",message_id="other-message",
                          provider_message_id=SEND_ID,payload="{}",state="delivered"))
        s.commit()
    assert reconcile_feedback(_event("complained"),session_factory=factory)=="unmatched"
    with factory() as s:
        assert s.query(Event).count()==0
        assert s.get(Message,"local-message").status=="provider_accepted"


@pytest.mark.parametrize("event,change",[
    ("Complaint",lambda p:p.update({"complaint":None})),
    ("Bounce",lambda p:p["bounce"].update({"bounceType":"Ambiguous"})),
])
def test_malformed_event_content_denied(signer,event,change):
    body=signed_body(signer,event=event,changes=change)
    with pytest.raises(SESFeedbackDenied):
        verified_ses_event(body,topic_arn=TOPIC,certificate_loader=lambda _:signer[1])
