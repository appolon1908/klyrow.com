# Brand versioning operations

Use the Brand Settings portal or its same-origin BFF routes to save a draft, publish it, inspect history, and restore a version. Publishing an already active profile is idempotent. Historical rows are append-only; restore never edits an existing snapshot.

Treat `409 brand_version_conflict` as a refresh-and-review signal. Archived profiles cannot be made default, edited, published, or restored. Audit entries record create, update, publish, and restore operations under the authenticated tenant.

This pre-Media release keeps asset controls unavailable. Do not manually populate opaque asset IDs in production. After MEDIA-01 merges, rebase and add the canonical media foreign keys plus tenant and `READY` state checks before requesting final review.