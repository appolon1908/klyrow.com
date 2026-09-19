# Campaign scheduling runtime

M07 implements scheduled public Klyrow campaigns behind the disabled-by-default `KLYROW_CAMPAIGN_DISPATCHER_ENABLED` feature flag. A disabled dispatcher rejects new schedules with HTTP 409 `campaign_dispatcher_unavailable` and does not replay previously sealed success responses.

An enabled schedule freezes the campaign version, published template version, sender, and deduplicated audience in the same database transaction. The worker uses a lease owner plus monotonically increasing fence token, evaluates global and campaign suppressions, and admits each recipient through the existing Klyrow provider message pipeline. It does not contain a second delivery engine.

The durable recipient identity is:

```text
campaign_id + campaign_version + sha256(normalized recipient)
```

Retries reuse that identity as the message-admission idempotency key. A crash after message admission therefore recovers the existing queued message rather than creating a duplicate.

Pause clears active dispatch leases and preserves pending work. Resume continues the frozen version. Cancel marks undispatched recipient items cancelled and creates one terminal `klyrow.campaign.summary` business event. Normal completion creates the same bounded business summary without recipient addresses.

The optional Compose profile keeps sandbox delivery enabled and explicitly keeps live delivery disabled. M07 does not authorize production activation.
