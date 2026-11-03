"""M0 parity matrix for API, SMTP, and provider mail submission paths."""

import inspect

import pytest

from apps.gateway.app.main import (
    DB,
    AllowedSender,
    Domain,
    Message,
    Suppression,
    Tenant,
    app,
    auth,
    rate_buckets,
)
from apps.gateway.app import main, provider, smtp_relay
from apps.gateway.app.billing import UsageEvent as BillingUsageEvent
from apps.gateway.app.provider import (
    ProviderDomain,
    ProviderMessage,
    SenderIdentity,
    TenantMailPolicy,
)
from tests.mail_path_clients import (
    ALL_PATHS,
    CONTEXT,
    EXTERNAL,
    SINK,
    TENANT_ID,
    MailPathClients,
    seed_mail_paths,
)

CLIENTS = MailPathClients()


@pytest.fixture(autouse=True)
def isolated_mail_paths():
    rate_buckets.clear()
    seed_mail_paths()
    app.dependency_overrides[auth] = lambda: CONTEXT
    yield
    app.dependency_overrides.pop(auth, None)


def submit(path: str, **kwargs):
    return CLIENTS.submit(path, **kwargs)


@pytest.mark.parametrize("path", ALL_PATHS)
def test_suppressed_recipient_rejected(path):
    with DB() as session:
        session.add(
            Suppression(
                id="parity-hard-bounce", tenant_id=TENANT_ID, email=SINK, reason="hard_bounce"
            )
        )
        session.commit()
    assert submit(path).accepted is False


@pytest.mark.parametrize(
    "path",
    ALL_PATHS,
)
def test_marketing_unsubscribe_does_not_block_transactional(path):
    with DB() as session:
        session.add(
            Suppression(
                id="parity-unsubscribe",
                tenant_id=TENANT_ID,
                email=SINK,
                reason="unsubscribe_marketing",
            )
        )
        session.commit()
    assert submit(path).accepted is True


@pytest.mark.parametrize(
    "path",
    ALL_PATHS,
)
def test_marketing_requires_authoritative_consent(path):
    result = submit(
        path,
        stream="MARKETING",
        key="marketing-no-authoritative-consent",
        marketing_consent_granted=True,
    )
    assert result.accepted is False


@pytest.mark.parametrize(
    "path",
    ALL_PATHS,
)
def test_exactly_one_canonical_usage_event_on_acceptance(path):
    assert submit(path, key="usage-" + path).accepted is True
    with DB() as session:
        assert session.query(BillingUsageEvent).filter_by(tenant_id=TENANT_ID).count() == 1


@pytest.mark.parametrize(
    "path",
    ALL_PATHS,
)
def test_unverified_sender_domain_rejected(path):
    with DB() as session:
        session.get(Domain, "m0-api-domain").verified = False
        session.get(ProviderDomain, "m0-provider-domain").status = "PENDING"
        session.commit()
    assert submit(path, key="unverified-" + path).accepted is False


@pytest.mark.parametrize("path", ALL_PATHS)
def test_quota_exhaustion_rejected(path):
    with DB() as session:
        session.get(Tenant, TENANT_ID).quota = 0
        policy = session.get(TenantMailPolicy, TENANT_ID)
        policy.hourly_limit = 0
        policy.daily_limit = 0
        policy.warmup_hourly_limit = 0
        policy.warmup_daily_limit = 0
        session.commit()
    assert submit(path, key="quota-" + path).accepted is False


@pytest.mark.parametrize(
    "path",
    ALL_PATHS,
)
def test_production_canary_guard_is_present(path):
    functions = {
        "api": main._send,
        "smtp": smtp_relay.GovernedRelay.handle_DATA,
        "provider": provider.preflight,
    }
    source = inspect.getsource(functions[path])
    assert "enforce_production_canary" in source or "authorize_send(" in source


@pytest.mark.parametrize("path", ALL_PATHS)
def test_unauthorized_sender_rejected(path):
    with DB() as session:
        session.query(AllowedSender).delete()
        session.query(SenderIdentity).delete()
        session.commit()
    assert submit(path, key="sender-" + path).accepted is False


@pytest.mark.parametrize(
    "path",
    ALL_PATHS,
)
def test_disabled_tenant_rejected_inside_send_path(path):
    with DB() as session:
        session.get(Tenant, TENANT_ID).enabled = False
        session.commit()
    assert submit(path, key="disabled-" + path).accepted is False


@pytest.mark.parametrize(
    "path",
    ALL_PATHS,
)
def test_sandbox_external_recipient_rejected(path):
    assert submit(path, recipient=EXTERNAL, key="sandbox-" + path).accepted is False


@pytest.mark.parametrize("path", ALL_PATHS)
def test_identical_replay_persists_one_message(path):
    first = submit(path, key="replay-" + path)
    second = submit(path, key="replay-" + path)
    assert first.accepted is True and second.accepted is True
    assert first.resource_id == second.resource_id
    with DB() as session:
        count = (
            session.query(Message).filter_by(tenant_id=TENANT_ID).count()
            if path == "api"
            else session.query(ProviderMessage).filter_by(tenant_id=TENANT_ID).count()
        )
        assert count == 1


@pytest.mark.parametrize(
    "path",
    ALL_PATHS,
)
def test_altered_replay_is_rejected(path):
    first = submit(path, key="altered-" + path, subject="Original")
    altered = submit(path, key="altered-" + path, subject="Changed")
    assert first.accepted is True
    assert altered.accepted is False
