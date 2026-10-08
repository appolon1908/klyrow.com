# Amazon SES / SNS / SQS / Klyrow reconciliation

The optional consumer processes AWS-signed SES status events. It does not
send email or expose any inbound ports. Live sending remains disabled.

Runtime checks:
- Exact SNS TopicArn and matching SQS queue in us-east-1; AWS HTTPS signing
  certificate and RSA signature checked before event fields are trusted.
- Match SES mail.messageId to local EmailOutbox.provider_message_id and verify
  local sender/recipient/tenant. Reject all unknown provider messages.
- Keep replay/audit records and tenant-specific permanent-bounce/complaint
  suppressions; never downgrade final status due to late Send events.
- SQS DeleteMessage only after durable DB commit or confirmed replay. Invalid,
  unmatched and failed events are retained for DLQ review.
- AWS queue should be SSE-encrypted, scoped to one SNS topic and configured
  with DLQ and alarms. Do not remove existing SQS messages.
- Explicit Compose profile ses-feedback plus
  KLYROW_SES_FEEDBACK_CONSUMER_ENABLED=true required before startup.
- Use workload or secret-injected credentials with only one-queue SQS
  ReceiveMessage, DeleteMessage, and GetQueueAttributes permissions.
- The application currently authorizes transactional sender identities from
  codestra.agency and klyrow.com only, despite 14 SES-verified GoDaddy domains.
- Independent review, CI, signature/DLQ test and production GO decision
  remain mandatory. Do not enable based on tests alone.

Run focused tests with:
python -m pytest -q tests/test_ses_feedback.py tests/test_ses_sqs_worker.py
