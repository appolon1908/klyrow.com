# Brand profiles

Brand Profiles are tenant-scoped presentation records. The browser BFF derives `tenant_id` from the authenticated session and never accepts it from a request body. A profile is `DRAFT`, `ACTIVE`, or `ARCHIVED`; only one active profile can be default in a tenant.

Publishing creates an immutable normalized snapshot in `klyrow_brand_profile_versions`. Restore copies a selected snapshot into the current profile and appends another immutable version. PATCH requests require the current optimistic `version` and fail with `409` on stale writes.

Before MEDIA-01 merges, `logo_asset_id` and `icon_asset_id` are optional opaque identifiers. No upload, storage, or asset validity assertion exists in this mission. Media integration must add canonical foreign keys and same-tenant `READY` status validation before this branch can merge.