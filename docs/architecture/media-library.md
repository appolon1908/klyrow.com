# Secure Tenant Media Library

The Media Library is a tenant-scoped metadata and lifecycle boundary. The authenticated browser session supplies the tenant; clients cannot select a tenant or choose a storage key.

## Contract

- Asset IDs are server-generated UUIDs.
- Owned tables are `klyrow_media_assets` and `klyrow_media_asset_events`.
- Migration: `2026091902_media_library_foundation.sql`.
- Allowed content types are `image/png`, `image/jpeg`, and `image/webp`.
- Maximum size is 10 MiB and maximum width or height is 8192 pixels.
- Only `READY` assets are usable by downstream features.
- Events are append-only, tenant-scoped, and record the exact status transition.

The storage interface is provider-neutral. Local and test runs use an in-process fake adapter. Production selects the fail-closed unavailable adapter until a reviewed provider implementation exists; no credentials or public URLs are stored in the database or browser.

## Lifecycle

`PENDING_UPLOAD` -> `UPLOADED` -> `VALIDATING` -> `READY`

Validation failures become `REJECTED`. Review or malware integrations may use `QUARANTINED`. Ready assets may become `ARCHIVED` or `DELETED`; terminal states are never silently reused.

## Browser API

All routes are same-origin browser routes and use the browser session plus the session-bound `X-Klyrow-CSRF` header. Queries are tenant-filtered in SQL.

- `GET /app/api/media`
- `POST /app/api/media/uploads`
- `POST /app/api/media/{asset_id}/complete`
- `GET /app/api/media/{asset_id}`
- `GET /app/api/media/{asset_id}/events`
- `POST /app/api/media/{asset_id}/archive`
- `DELETE /app/api/media/{asset_id}`
