# Media Asset Lifecycle Operations

Media uploads are prepared with a short-lived opaque reference. The reference is bound to the authenticated tenant and server-generated asset ID. The browser never receives storage credentials or an unrestricted object URL.

## States

`PENDING_UPLOAD` is metadata awaiting provider upload. Completion inspects bytes rather than trusting the browser declaration, verifies the digest and dimensions, and records immutable events for `UPLOADED`, `VALIDATING`, and `READY`.

Malformed, mismatched, zero-byte, oversized, executable, markup, SVG, or dimension-limit violations become `REJECTED`. Provider outages fail closed with `media_storage_unavailable`.

## Mutation rules

Archive and delete require the current optimistic `version`. A stale version returns `media_version_conflict`. Tenant mismatch is indistinguishable from not-found. Deleted assets are excluded from listings and cannot be selected by downstream consumers.

The fake adapter is for tests and local development only. Do not add production credentials, public buckets, remote URL imports, archive extraction, or raw object content to logs.