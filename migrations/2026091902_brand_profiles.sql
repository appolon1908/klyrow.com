-- BRAND-01 reservation. Media foreign keys are intentionally deferred until MEDIA-01 merges.
CREATE TABLE IF NOT EXISTS klyrow_brand_profiles (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    company_name TEXT NOT NULL,
    website_url TEXT,
    support_email TEXT,
    logo_asset_id TEXT,
    icon_asset_id TEXT,
    primary_color TEXT NOT NULL,
    secondary_color TEXT NOT NULL,
    accent_color TEXT NOT NULL,
    background_color TEXT NOT NULL,
    text_color TEXT NOT NULL,
    heading_font TEXT NOT NULL,
    body_font TEXT NOT NULL,
    footer_text TEXT,
    physical_address TEXT,
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT', 'ACTIVE', 'ARCHIVED')),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at TIMESTAMPTZ,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0)
);
CREATE INDEX IF NOT EXISTS ix_klyrow_brand_profiles_tenant ON klyrow_brand_profiles (tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_klyrow_brand_default_active_tenant ON klyrow_brand_profiles (tenant_id) WHERE is_default = TRUE AND status = 'ACTIVE';

CREATE TABLE IF NOT EXISTS klyrow_brand_profile_versions (
    id TEXT PRIMARY KEY,
    brand_profile_id TEXT NOT NULL REFERENCES klyrow_brand_profiles(id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    snapshot_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_klyrow_brand_profile_version UNIQUE (brand_profile_id, version_number)
);
CREATE INDEX IF NOT EXISTS ix_klyrow_brand_versions_tenant_profile ON klyrow_brand_profile_versions (tenant_id, brand_profile_id);