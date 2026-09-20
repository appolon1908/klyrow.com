BEGIN;

CREATE TABLE IF NOT EXISTS klyrow_media_assets (
    id VARCHAR NOT NULL PRIMARY KEY,
    tenant_id VARCHAR NOT NULL,
    original_filename VARCHAR NOT NULL,
    safe_filename VARCHAR NOT NULL,
    media_kind VARCHAR NOT NULL,
    declared_content_type VARCHAR NOT NULL,
    detected_content_type VARCHAR,
    size_bytes BIGINT NOT NULL CHECK (size_bytes > 0),
    sha256_digest VARCHAR(64),
    width INTEGER,
    height INTEGER,
    storage_provider VARCHAR NOT NULL,
    storage_object_key VARCHAR,
    status VARCHAR NOT NULL DEFAULT 'PENDING_UPLOAD',
    quarantine_reason VARCHAR,
    created_by VARCHAR NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    validated_at TIMESTAMP WITH TIME ZONE,
    ready_at TIMESTAMP WITH TIME ZONE,
    archived_at TIMESTAMP WITH TIME ZONE,
    deleted_at TIMESTAMP WITH TIME ZONE,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    upload_reference TEXT,
    idempotency_key VARCHAR,
    CONSTRAINT ck_media_asset_status CHECK (status IN ('PENDING_UPLOAD','UPLOADED','VALIDATING','READY','REJECTED','QUARANTINED','ARCHIVED','DELETED')),
    CONSTRAINT uq_media_asset_tenant_idempotency UNIQUE (tenant_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS ix_klyrow_media_assets_tenant_id ON klyrow_media_assets (tenant_id);
CREATE INDEX IF NOT EXISTS ix_klyrow_media_assets_status ON klyrow_media_assets (tenant_id, status);
CREATE INDEX IF NOT EXISTS ix_klyrow_media_assets_type ON klyrow_media_assets (tenant_id, detected_content_type);
CREATE INDEX IF NOT EXISTS ix_klyrow_media_assets_digest ON klyrow_media_assets (sha256_digest);
CREATE INDEX IF NOT EXISTS ix_klyrow_media_assets_tenant_digest ON klyrow_media_assets (tenant_id, sha256_digest);
CREATE INDEX IF NOT EXISTS ix_klyrow_media_assets_lifecycle ON klyrow_media_assets (tenant_id, created_at, status);

CREATE TABLE IF NOT EXISTS klyrow_media_asset_events (
    id VARCHAR NOT NULL PRIMARY KEY,
    asset_id VARCHAR NOT NULL,
    tenant_id VARCHAR NOT NULL,
    event_type VARCHAR NOT NULL,
    from_status VARCHAR,
    to_status VARCHAR NOT NULL,
    reason_code VARCHAR,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    correlation_id VARCHAR NOT NULL,
    created_by VARCHAR NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_media_asset_event_asset FOREIGN KEY (asset_id) REFERENCES klyrow_media_assets(id),
    CONSTRAINT ck_media_asset_event_to_status CHECK (to_status IN ('PENDING_UPLOAD','UPLOADED','VALIDATING','READY','REJECTED','QUARANTINED','ARCHIVED','DELETED'))
);

CREATE INDEX IF NOT EXISTS ix_klyrow_media_asset_events_asset_id ON klyrow_media_asset_events (tenant_id, asset_id, created_at);
CREATE INDEX IF NOT EXISTS ix_klyrow_media_asset_events_tenant_id ON klyrow_media_asset_events (tenant_id, created_at);
CREATE INDEX IF NOT EXISTS ix_klyrow_media_asset_events_correlation_id ON klyrow_media_asset_events (correlation_id);

COMMIT;