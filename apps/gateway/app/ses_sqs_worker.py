"""Dedicated SES SNS/SQS feedback consumer. Disabled unless expressly provisioned.

Receives verified AWS SNS events, reconciles by SES provider ID inside Klyrow's
database and deletes SQS messages *only* after durable commit or duplicate proof.
Unmatched, tampered or unavailable events remain retained for DLQ/review.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import sys
import time

from .ses_feedback import SESFeedbackDenied, REGION, reconcile_feedback, verified_ses_event

REGION_NAME = "us-east-1"


def checked_configuration(env=None):
    config = dict(os.environ if env is None else env)
    if config.get("KLYROW_SES_FEEDBACK_CONSUMER_ENABLED") != "true":
        raise RuntimeError("ses_feedback_consumer_disabled")
    topic = config.get("KLYROW_SES_SNS_TOPIC_ARN", "")
    queue = config.get("KLYROW_SES_SQS_QUEUE_URL", "")
    topic_match = re.fullmatch(r"arn:aws:sns:us-east-1:([0-9]{12}):codestra-klyrow-ses-events", topic)
    queue_match = re.fullmatch(r"https://sqs\.us-east-1\.amazonaws\.com/([0-9]{12})/codestra-klyrow-ses-events", queue)
    if not topic_match or not queue_match or topic_match.group(1) != queue_match.group(1):
        raise RuntimeError("ses_feedback_queue_or_topic_mismatch")
    if config.get("KLYROW_EMAIL_TRANSPORT") != "ses":
        raise RuntimeError("ses_feedback_transport_not_selected")
    if config.get("AWS_REGION", REGION_NAME) != REGION_NAME:
        raise RuntimeError("ses_feedback_region_mismatch")
    return topic, queue


def process_batch(sqs, *, topic_arn: str, queue_url: str, messages: list) -> dict:
    counts = {"ack": 0, "duplicate": 0, "unmatched": 0, "invalid": 0, "failed": 0}
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("Body"), str) or not message.get("ReceiptHandle"):
            counts["invalid"] += 1
            continue
        try:
            verified = verified_ses_event(message["Body"], topic_arn=topic_arn)
        except (SESFeedbackDenied, ValueError):
            counts["invalid"] += 1
            # Never log raw SNS, SES addresses, or arbitrary attacker-controlled text.
            continue
        try:
            outcome = reconcile_feedback(verified)
            if outcome not in ("ack", "duplicate"):
                counts["unmatched"] += 1
                continue
            # Delete only after DB transaction committed; failure makes SQS retry safe.
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
            counts[outcome] += 1
        except Exception:
            counts["failed"] += 1
    return counts


def run():
    topic, queue = checked_configuration()
    # boto3 is an optional runtime-only dependency; no credentials are read from Git.
    import boto3
    client = boto3.client("sqs", region_name=REGION_NAME)
    while True:
        results = client.receive_message(
            QueueUrl=queue, MaxNumberOfMessages=10, WaitTimeSeconds=15,
            MessageSystemAttributeNames=["ApproximateReceiveCount"],
        )
        records = results.get("Messages", [])
        summary = process_batch(client, topic_arn=topic, queue_url=queue, messages=records)
        if records:
            # Safe bounded counters only. Do not expose email recipients or message bodies.
            print(json.dumps({"component": "klyrow-ses-feedback", "result":summary},sort_keys=True), flush=True)


if __name__ == "__main__":
    run()
