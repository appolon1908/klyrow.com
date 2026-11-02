"""SQS acknowledgement controls: never delete unverified or unmatched events."""
import json
import sys
from types import SimpleNamespace
import pytest
import requests

from apps.gateway.app import ses_sqs_worker as worker
from apps.gateway.app.ses_feedback import SESFeedback, SESFeedbackDenied

TOPIC="arn:aws:sns:us-east-1:123456789012:codestra-klyrow-ses-events"
QUEUE="https://sqs.us-east-1.amazonaws.com/123456789012/codestra-klyrow-ses-events"


@pytest.mark.parametrize("overrides,reason", [
    ({}, "disabled"),
    ({"KLYROW_SES_FEEDBACK_CONSUMER_ENABLED":"true"},"queue_or_topic"),
    ({"KLYROW_SES_FEEDBACK_CONSUMER_ENABLED":"true","KLYROW_SES_SNS_TOPIC_ARN":TOPIC,
      "KLYROW_SES_SQS_QUEUE_URL":"https://sqs.us-east-1.amazonaws.com/999999999999/codestra-klyrow-ses-events",
      "KLYROW_EMAIL_TRANSPORT":"ses"},"queue_or_topic"),
    ({"KLYROW_SES_FEEDBACK_CONSUMER_ENABLED":"true","KLYROW_SES_SNS_TOPIC_ARN":TOPIC,
      "KLYROW_SES_SQS_QUEUE_URL":QUEUE,"KLYROW_EMAIL_TRANSPORT":"postal"},"transport"),
])
def test_reject_unsafe_runtime_configuration(overrides,reason):
    with pytest.raises(RuntimeError,match=reason):
        worker.checked_configuration(overrides)


def test_accept_explicit_restricted_binding():
    config={"KLYROW_SES_FEEDBACK_CONSUMER_ENABLED":"true","KLYROW_SES_SNS_TOPIC_ARN":TOPIC,
            "KLYROW_SES_SQS_QUEUE_URL":QUEUE,"KLYROW_EMAIL_TRANSPORT":"ses"}
    assert worker.checked_configuration(config)==(TOPIC,QUEUE)


class SQS:
    def __init__(self):self.deleted=[]
    def delete_message(self,**kwargs):self.deleted.append(kwargs)


def feedback():
    return SESFeedback("sns-id","ses-id","Delivery","delivered","noreply@codestra.agency","receiver@example.net","",False)


@pytest.mark.parametrize("outcome,expected",[
    ("ack",True),("duplicate",True),("unmatched",False)
])
def test_only_acknowledged_or_duplicate_records_are_deleted(monkeypatch,outcome,expected):
    sqs=SQS()
    monkeypatch.setattr(worker,"verified_ses_event",lambda *a,**kw:feedback())
    monkeypatch.setattr(worker,"reconcile_feedback",lambda *a,**kw:outcome)
    response=worker.process_batch(sqs,topic_arn=TOPIC,queue_url=QUEUE,
          messages=[{"Body":"synthetic","ReceiptHandle":"receipt-example"}])
    assert len(sqs.deleted)==int(expected)
    if expected:
        assert sqs.deleted[0]=={"QueueUrl":QUEUE,"ReceiptHandle":"receipt-example"}
    assert response[outcome]==1


def test_invalid_signature_stays_in_queue(monkeypatch):
    sqs=SQS()
    monkeypatch.setattr(worker,"verified_ses_event",lambda *a,**kw: (_ for _ in ()).throw(SESFeedbackDenied("tampered")))
    x=worker.process_batch(sqs,topic_arn=TOPIC,queue_url=QUEUE,messages=[{"Body":"bad","ReceiptHandle":"r"}])
    assert not sqs.deleted and x["invalid"]==1


def test_database_failure_stays_in_queue(monkeypatch):
    sqs=SQS()
    monkeypatch.setattr(worker,"verified_ses_event",lambda *a,**kw:feedback())
    monkeypatch.setattr(worker,"reconcile_feedback",lambda *a,**kw: (_ for _ in ()).throw(RuntimeError("DB unavailable")))
    x=worker.process_batch(sqs,topic_arn=TOPIC,queue_url=QUEUE,messages=[{"Body":"valid","ReceiptHandle":"r"}])
    assert not sqs.deleted and x["failed"]==1


def test_missing_receipt_never_deleted(monkeypatch):
    sqs=SQS()
    x=worker.process_batch(sqs,topic_arn=TOPIC,queue_url=QUEUE,messages=[{"Body":"synthetic"}])
    assert not sqs.deleted and x["invalid"]==1


def test_certificate_outage_retains_record_and_continues_batch(monkeypatch):
    sqs = SQS()

    def verify(body, **kwargs):
        if body == "unavailable":
            raise requests.ConnectionError("certificate service unavailable")
        return feedback()

    monkeypatch.setattr(worker, "verified_ses_event", verify)
    monkeypatch.setattr(worker, "reconcile_feedback", lambda *a, **kw: "ack")
    counts = worker.process_batch(sqs, topic_arn=TOPIC, queue_url=QUEUE, messages=[
        {"Body": "unavailable", "ReceiptHandle": "retain"},
        {"Body": "valid", "ReceiptHandle": "acknowledge"},
    ])
    assert counts["failed"] == 1 and counts["ack"] == 1
    assert sqs.deleted == [{"QueueUrl": QUEUE, "ReceiptHandle": "acknowledge"}]


@pytest.mark.parametrize("first_result", [OSError("private outage details"), None, {"Messages": None}])
def test_poll_failure_retries_without_deleting_or_leaking_details(monkeypatch, capsys, first_result):
    sqs = SQS()
    results = iter([first_result, {}, KeyboardInterrupt()])

    def receive(**kwargs):
        result = next(results)
        if isinstance(result, BaseException):
            raise result
        return result

    sqs.receive_message = receive
    monkeypatch.setattr(worker, "checked_configuration", lambda: (TOPIC, QUEUE))
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=lambda *a, **kw: sqs))
    pauses = []
    monkeypatch.setattr(worker.time, "sleep", pauses.append)
    with pytest.raises(KeyboardInterrupt):
        worker.run()
    assert len(pauses) == 1 and 0 < pauses[0] <= 60
    assert not sqs.deleted
    assert "private outage details" not in capsys.readouterr().out
