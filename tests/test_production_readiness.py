import re
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_runtime_secret_bootstrap_never_prints_credentials():
    script = (ROOT / "scripts/generate-env").read_text()
    assert '[[ "$EUID" -eq 0 ]]' in script
    assert "KLYROW_RUNTIME_SECRET_DIR:-/etc/klyrow/secrets" in script
    assert 'install -o root -g root -m 0600' in script
    assert "KLYROW_SESSION_SECRET_FILE=$runtime_secret_dir/session-secret" in script
    assert "KLYROW_POSTAL_API_KEY_FILE=$runtime_secret_dir/postal-api-key" in script
    assert "KLYROW_METRICS_TOKEN_FILE=$runtime_secret_dir/metrics-token" in script
    assert "KLYROW_MIDDLEWARE_API_KEY_FILE=$runtime_secret_dir/middleware-api-key" in script
    assert "KLYROW_WEBHOOK_SECRET_FILE=$runtime_secret_dir/webhook-secret" in script
    assert "KLYROW_PROVIDER_CREDENTIAL_KEY_FILE=$runtime_secret_dir/provider-credential-key" in script
    assert "KLYROW_POSTAL_PROVISIONER_TOKEN_FILE=$runtime_secret_dir/postal-provisioner-token" in script
    assert "KLYROW_SECURITY_PAYLOAD_KEY_FILE=$runtime_secret_dir/security-payload-key" in script
    assert "$runtime_secret_dir/database-owner-password" in script
    assert "$runtime_secret_dir/database-runtime-password" in script
    assert "Initial admin password:" not in script
    assert "chown root:root .env" in script


def test_schema_migration_is_a_required_gateway_dependency():
    compose = (ROOT / "docker-compose.yml").read_text()
    runner = (ROOT / "scripts/migrate").read_text()
    assert "gateway-migrate: {condition: service_completed_successfully}" in compose
    assert "2026091302_m07_business_event_runtime.sql" in compose
    assert 'KLYROW_REQUIRE_LEAST_PRIVILEGE_DB: "true"' in compose
    source = (ROOT / "apps/gateway/app/main.py").read_text()
    assert "runtime database role has cluster-level privileges" in source
    least_privilege = (ROOT / "migrations/2026090208_runtime_database_least_privilege.sql").read_text()
    for privilege in ("NOSUPERUSER", "NOCREATEDB", "NOCREATEROLE", "NOREPLICATION", "NOBYPASSRLS"):
        assert privilege in least_privilege
    assert "KLYROW_MIGRATE_IMAGE" in compose
    assert "docker/migrate.Dockerfile" not in compose
    assert "pg_advisory_xact_lock" in runner
    assert "applied migration checksum mismatch" in runner
    canary = (ROOT / "migrations/2026082202_production_canary_claim_ledger.sql").read_text()
    assert "ALTER COLUMN updated_at SET DEFAULT now()" in canary
    assert "claimed_deliveries, updated_at" in canary
    legacy = (ROOT / "migrations/0000_legacy_schema_compat.sql").read_text()
    assert "RENAME COLUMN domain_id TO domain_claim_id" in legacy
    assert "RENAME COLUMN secret_hash TO verifier_hash" in legacy


def test_mail_worker_has_the_canonical_mtls_event_delivery_contract():
    compose = (ROOT / "docker-compose.yml").read_text()
    worker = compose.split("  worker:", 1)[1].split("  scheduler:", 1)[0]
    assert "KLYROW_EMAIL_EVENT_URL: https://middleware-email-events.internal.codestra.agency:18080/internal/provider-events/klyrow" in worker
    assert "KLYROW_SERVER_A_CA_FILE: /run/codestra-mtls/server-a/root-ca.crt" in worker
    assert "KLYROW_SERVER_A_CLIENT_CERT_FILE: /run/codestra-mtls/server-a/client-fullchain.crt" in worker
    assert "KLYROW_SERVER_A_CLIENT_KEY_FILE: /run/codestra-mtls/server-a/client.key" in worker
    assert "/etc/codestra/mtls/server-a-event-client:/run/codestra-mtls/server-a:ro" in worker


def test_upgrade_migrates_legacy_environment_secrets_before_compose():
    update = (ROOT / "scripts/update").read_text()
    deploy = (ROOT / "scripts/deploy").read_text()
    migration = (ROOT / "scripts/migrate-runtime-secrets").read_text()
    assert "scripts/migrate-runtime-secrets" in update
    assert "scripts/migrate-runtime-secrets" in deploy
    assert 'os.geteuid() != 0' in migration
    assert 'os.chmod(target, 0o600)' in migration
    assert 'removed = set(specs)' in migration
    assert '"POSTGRES_PASSWORD": ("KLYROW_DATABASE_OWNER_PASSWORD_FILE"' in migration
    assert "KLYROW_DATABASE_URL=postgresql+psycopg://klyrow_runtime@postgres:5432/klyrow" in migration


def test_standard_launchers_use_the_complete_digest_pinned_release():
    for relative_path in ("scripts/start", "scripts/deploy", "scripts/update"):
        source = (ROOT / relative_path).read_text()
        assert "-f docker-compose.web.yml" in source
        assert "-f docker-compose.postal-provisioning.yml" in source
        assert "scripts/validate-production-images" in source
        assert "scripts/verify-release-authority" in source
        assert " build " not in source
        assert "--build" not in source
        assert "scripts/migrate\n" not in source


def test_production_compose_forbids_local_builds_and_requires_release_images():
    base = (ROOT / "docker-compose.yml").read_text()
    web = (ROOT / "docker-compose.web.yml").read_text()
    provisioning = (ROOT / "docker-compose.postal-provisioning.yml").read_text()
    combined = base + web + provisioning
    assert "build:" not in combined
    for variable in (
        "KLYROW_GATEWAY_IMAGE",
        "KLYROW_WEB_IMAGE",
        "KLYROW_MIGRATE_IMAGE",
        "KLYROW_POSTAL_PROVISIONER_IMAGE",
    ):
        assert variable in combined
    assert "./migrations:/migrations:ro" not in base
    gateway_override = provisioning.split("  postal-provisioner:", 1)[0]
    assert "KLYROW_PROVIDER_CREDENTIAL_KEY_FILE" in gateway_override
    assert "KLYROW_POSTAL_PROVISIONER_TOKEN_FILE" in gateway_override
    assert "secrets: [klyrow_provider_credential_key, klyrow_postal_provisioner_token]" in gateway_override


def test_production_image_validator_requires_digests_and_canonical_repositories():
    source = (ROOT / "scripts/validate-production-images").read_text()
    assert 'if "build" in service' in source
    assert "@sha256:[0-9a-f]{64}" in source
    assert "ghcr.io/appolon1908-hue/klyrow-gateway" in source
    assert "ghcr.io/appolon1908-hue/klyrow-migrate" in source
    assert "ghcr.io/appolon1908-hue/klyrow-web" in source
    assert "ghcr.io/appolon1908-hue/klyrow-postal-provisioner" in source


def test_deploy_requires_protected_source_config_and_rollback_authority():
    source = (ROOT / "scripts/verify-release-authority").read_text()
    validator = (ROOT / "scripts/verify-release-authority.py").read_text()
    assert "PUBLISH_SOURCE_SHA" in source and "PUBLISH_SHA256SUMS" in source
    assert "python3 scripts/verify-email-activation" in source
    assert "git status --porcelain" in source
    assert "refs/remotes/origin/main" in source
    assert "Runtime configuration checksum mismatch" in source
    assert "rollback authority must reference a prior source SHA" in validator
    assert "org.opencontainers.image.revision" in source
    checksum = (ROOT / "scripts/config-checksum").read_text()
    assert "config --format json" in checksum and "hashlib.sha256" in checksum


def test_database_passwords_are_file_only_and_runtime_role_is_separate():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "POSTGRES_PASSWORD:" not in compose
    assert "postgresql+psycopg://klyrow:${POSTGRES_PASSWORD" not in compose
    assert "POSTGRES_PASSWORD_FILE: /run/secrets/klyrow_database_owner_password" in compose
    assert compose.count("postgresql+psycopg://klyrow_runtime@postgres:5432/klyrow") >= 6
    assert "KLYROW_DATABASE_RUNTIME_PASSWORD_FILE" in compose


def test_outbox_recovers_abandoned_sending_leases():
    source = (ROOT / "apps/gateway/app/main.py").read_text()
    assert 'EmailOutbox.state=="sending"' in source
    assert "EmailOutbox.updated_at<stale" in source
    assert '"Idempotency-Key":"klyrow:"+snapshot[1]' in source


def test_outbox_retries_back_off_and_terminal_failure_updates_message():
    source = (ROOT / "apps/gateway/app/main.py").read_text()
    migration = (ROOT / "migrations/2026082201_email_outbox_and_tenant_idempotency.sql").read_text()
    assert "EmailOutbox.next_attempt_at<=current" in source
    assert "failed=item.attempts>=5" in source
    assert "if first_attempt:" in source
    assert "min(300,2**max(item.attempts,1))" in source
    assert 'set_core_message_status(message,"failed")' in source
    assert 'kind="klyrow.email.failed"' in source
    assert "next_attempt_at timestamptz" in migration


def test_single_domain_canary_is_durable_and_fail_closed():
    source = (ROOT / "apps/gateway/app/main.py").read_text()
    migration = (ROOT / "migrations/2026082201_email_outbox_and_tenant_idempotency.sql").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "canary_configuration_valid()" in source
    assert 'KLYROW_CANARY_ALLOWED_CAMPAIGN' in source
    assert 'with_for_update()' in source
    assert 'production_canary_limit_reached' in source
    assert 'bulk_delivery_disabled_during_canary' in source
    assert 'campaign_delivery_disabled_during_canary' in source
    assert 'production_canary_gate' in migration
    assert 'canary_payload_allowed(payload)' in source
    assert 'payload.get("campaign_id")==allowed_campaign' in source
    assert '"campaign_id":x.campaign_id' in source
    assert 'item.state="quarantined"' in source
    assert 'gate.claimed_deliveries+=1' in source
    assert 'claimed_deliveries integer NOT NULL DEFAULT 0' in migration
    assert 'invalid_outbox_payload' in source
    assert 'normalized_recipients' in source
    assert 'except (TypeError,ValueError):maximum=-1' in source
    assert "KLYROW_CANARY_MAX_DELIVERIES" in compose
    assert 'KLYROW_BULK_DELIVERY_ENABLED: "false"' in compose


def test_base_production_compose_forces_email_delivery_fail_closed():
    compose = (ROOT / "docker-compose.yml").read_text()
    gateway = compose.split("  gateway:", 1)[1].split("  worker:", 1)[0]
    worker = compose.split("  worker:", 1)[1].split("  scheduler:", 1)[0]
    for service in (gateway, worker):
        assert 'KLYROW_SAFE_MODE: "true"' in service
        assert 'KLYROW_PRODUCTION_GATE_APPROVED: "false"' in service
        assert 'LIVE_EMAIL_DELIVERY: "false"' in service
    assert 'KLYROW_SAFE_MODE: "${KLYROW_SAFE_MODE:-true}"' not in gateway + worker


def test_resolver_requires_write_permission_for_mutating_routes():
    source = (ROOT / "apps/gateway/app/main.py").read_text()
    assert 'request.method not in {"GET","HEAD","OPTIONS"}' in source
    assert 'permission="klyrow.webhook" if "webhook" in request.url.path else "klyrow.send"' in source
    assert 'request.url.path=="/v1/integrations/results":permission="klyrow.integration.result.write"' in source


def test_integration_state_transitions_share_row_lock_and_unique_race_recovery():
    operations = (ROOT / "apps/gateway/app/operations.py").read_text()
    production_api = (ROOT / "apps/gateway/app/production_api.py").read_text()
    mautic = (ROOT / "apps/gateway/app/mautic_adapter.py").read_text()
    assert operations.count("locked_integration_outbox(") >= 4
    assert 'item.state not in {"PENDING","RETRY"}' in operations
    assert "except IntegrityError:" in operations
    assert "s.rollback();prior=integration_result_by_key" in operations
    assert production_api.count("_find_operation_for_update(") >= 3
    assert mautic.count(".with_for_update(") >= 4


def test_health_counts_outbox_and_reflects_durable_canary_capacity():
    source = (ROOT / "apps/gateway/app/main.py").read_text()
    assert "select(func.count()).select_from(EmailOutbox)" in source
    assert "gate.reserved_deliveries<maximum" in source
    assert "production_gate_open(s)" in source


def test_every_long_running_production_service_has_meaningful_health():
    compose = (ROOT / "docker-compose.yml").read_text()

    def service_block(name):
        match = re.search(rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)", compose)
        assert match
        return match.group(1)

    for service in ("mautic-cron", "mautic-worker"):
        block = service_block(service)
        assert "doctrine:query:sql" in block and "healthcheck:" in block
    postal_worker = service_block("postal-worker")
    assert "rabbitmq" in postal_worker and "postal-db" in postal_worker
    for service in ("prometheus", "grafana", "node-exporter"):
        block = service_block(service)
        assert "healthcheck:" in block


def test_prometheus_uses_the_private_metrics_credential():
    compose = (ROOT / "docker-compose.yml").read_text()
    prometheus = (ROOT / "config/prometheus.yml").read_text()
    assert "secrets: [klyrow_metrics_token]" in compose
    assert "credentials_file: /run/secrets/klyrow_metrics_token" in prometheus
    alerts=(ROOT/"config/alerts.yml").read_text()
    for name in ("KlyrowEmailQueueStalled","KlyrowN8nDeliveryStalled","KlyrowOdooDeliveryStalled","KlyrowCustomerWebhookFailures","KlyrowHighBounceRate","KlyrowHighComplaintRate","KlyrowDomainDnsInvalid","KlyrowBillingReconciliationFailure","KlyrowHostCpuPressure","KlyrowHostMemoryPressure"):
        assert "alert: "+name in alerts


def test_container_scan_fails_only_for_configured_high_and_critical_findings():
    workflow=(ROOT/".github/workflows/ci.yml").read_text()
    assert "severity: CRITICAL,HIGH" in workflow
    assert "limit-severities-for-sarif: true" in workflow
    assert 'exit-code: "1"' in workflow
