"""Offline contract acceptance vectors; no gateway, DB or provider is started."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / 'contracts/campaign-execution'


def test_contract_artifacts_exist():
    for name in ('execution.v1.schema.json', 'execution.openapi.json',
                 'event-mapping.v1.json', 'examples/plan.json', 'examples/readback.json'):
        assert (CONTRACT / name).is_file(), name


@pytest.fixture
def reference():
    path = ROOT / 'scripts/validate_campaign_execution_contracts.py'
    assert path.is_file(), 'contract reference validator is required'
    spec = importlib.util.spec_from_file_location('mcr_contract', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def plan():
    return json.loads((CONTRACT / 'examples/plan.json').read_text())


def test_offline_bundle(reference):
    reference.validate_bundle()


def test_key_pins_every_natural_identity_field(reference, plan):
    touch = plan['touch']
    original = reference.exposure_key(touch)
    assert original == plan['exposure_idempotency_key']
    for key, value in {'tenant_id': 'tenant-b', 'lead_id': '100-L-00000002',
                       'campaign_id': 'klyrow:other', 'campaign_version': 2,
                       'touch_index': 2}.items():
        assert reference.exposure_key({**touch, key: value}) != original
    assert reference.exposure_key(dict(reversed(list(touch.items())))) == original


@pytest.mark.parametrize('field,value', [('campaign_id', 'cmp_raw'),
    ('campaign_id', 'whatsapp:cmp'), ('campaign_version', 0),
    ('campaign_version', True), ('touch_index', 0), ('touch_index', 1001),
    ('channel', 'sms'), ('lead_id', 'arbitrary'), ('tenant_id', '')])
def test_invalid_identity(reference, plan, field, value):
    plan['touch'][field] = value
    with pytest.raises(ValueError):
        reference.validate_plan(plan, tenant='tenant-a')


@pytest.mark.parametrize('change', ['tenant', 'key', 'extra', 'live', 'schedule', 'journey'])
def test_plan_rejects_binding_and_safety_errors(reference, plan, change):
    if change == 'tenant':
        plan['touch']['tenant_id'] = 'tenant-b'
    elif change == 'key':
        plan['exposure_idempotency_key'] = 'mcr1:' + '0' * 64
    elif change == 'extra':
        plan['recipients'] = ['someone@example.test']
    elif change == 'live':
        plan['provider_effects'] = 'enabled'
    elif change == 'schedule':
        plan['schedule']['expires_at'] = plan['schedule']['not_before']
    else:
        plan['journey'] = {'journey_id': 'j'}
    with pytest.raises(ValueError):
        reference.validate_plan(plan, tenant='tenant-a')


def test_readback_requires_exact_original_binding(reference, plan):
    result = json.loads((CONTRACT / 'examples/readback.json').read_text())
    reference.validate_readback(plan, result, tenant='tenant-a')
    for key in ('command_id', 'request_hash', 'exposure_idempotency_key'):
        changed = copy.deepcopy(result)
        changed[key] = '00000000-0000-4000-8000-000000000009' if key == 'command_id' else ('mcr1:' if key.startswith('exposure') else '') + '0' * 64
        with pytest.raises(ValueError):
            reference.validate_readback(plan, changed, tenant='tenant-a')
    result['touch']['tenant_id'] = 'tenant-b'
    with pytest.raises(ValueError):
        reference.validate_readback(plan, result, tenant='tenant-a')


def test_request_hash_conflict_on_content_change(reference, plan):
    digest = reference.request_hash(plan)
    assert reference.replay_binding(digest, reference.request_hash(copy.deepcopy(plan))) == 'duplicate'
    plan['template_version'] += 1
    with pytest.raises(ValueError, match='idempotency_conflict'):
        reference.replay_binding(digest, reference.request_hash(plan))


@pytest.mark.parametrize('raw,expected', [('accepted','accepted'), ('queued','queued'),
    ('submitted','dispatched'), ('sent','dispatched'), ('delivered','delivered'),
    ('deferred','deferred'), ('complained','complaint'), ('unsubscribed','unsubscribe'),
    ('opened','open'), ('clicked','click'), ('failed',None), ('rejected',None),
    ('cancelled',None), ('unknown_outcome',None)])
def test_event_mapping(reference, raw, expected):
    assert reference.map_event('klyrow.email.' + raw)['normalized_event'] == expected


@pytest.mark.parametrize('bounce,expected', [('soft','soft_bounce'), ('hard','hard_bounce'),
                                           ('unknown','hard_bounce'), (None,'hard_bounce')])
def test_bounce_fail_closed(reference, bounce, expected):
    mapped = reference.map_event('klyrow.email.bounced', bounce)
    assert mapped['normalized_event'] == expected
    assert mapped['block_address'] == (expected == 'hard_bounce')
    assert mapped['suppression_reason'] is None


@pytest.mark.parametrize('raw', ['complained', 'unsubscribed'])
def test_suppression_handoff(reference, raw):
    mapped = reference.map_event('klyrow.email.' + raw)
    assert mapped['block_address'] is True
    assert mapped['suppression_scope'] == 'channel'
    assert mapped['suppression_reason'] in {'complaint', 'unsubscribe'}


def test_unrecognized_delivery_facts_quarantined(reference):
    with pytest.raises(ValueError, match='unmapped_event'):
        reference.map_event('klyrow.email.reply')
    with pytest.raises(ValueError, match='invalid_bounce_class'):
        reference.map_event('klyrow.email.bounced', 'typo')


@pytest.mark.parametrize('outcome,attempt,expected', [('transient_before_admission',1,'retry'),
    ('transient_before_admission',5,'dead_letter'), ('unknown_outcome',1,'reconcile'),
    ('readback_mismatch',1,'reconcile'), ('permanent_failure',1,'dead_letter'),
    ('delivered',1,'no_send'), ('suppressed',1,'no_send')])
def test_recovery_never_blindly_resends(reference, outcome, attempt, expected):
    assert reference.recovery(outcome, attempt)['action'] == expected


def test_backoff_and_retry_after(reference):
    assert [reference.recovery('transient_before_admission', i)['delay_seconds']
            for i in range(1,5)] == [30,60,120,240]
    assert reference.recovery('transient_before_admission', 1, 800)['delay_seconds'] == 800
    assert reference.recovery('transient_before_admission', 1, 901)['action'] == 'dead_letter'
    for invalid in (0, -1, True, 6):
        with pytest.raises(ValueError):
            reference.recovery('transient_before_admission', invalid)


@pytest.fixture
def principal():
    return {'verified': True, 'issuer': 'https://auth.codestra.co/realms/codestra',
            'audience': 'sandbox-klyrow', 'azp': 'sandbox-middleware',
            'service': True, 'subject': 'sandbox-middleware-service',
            'tenant_id': 'tenant-a', 'scopes': ['klyrow.read']}


def test_verified_service_contract(reference, principal):
    reference.authorize(principal, 'tenant-a', audience='sandbox-klyrow',
                        azp='sandbox-middleware', subject='sandbox-middleware-service')


@pytest.mark.parametrize('field,value', [('verified',False), ('issuer','wrong'),
    ('audience','middleware-api'), ('azp','browser'), ('service',False),
    ('subject','foreign'), ('tenant_id','tenant-b'), ('scopes',[])])
def test_auth_negative_matrix(reference, principal, field, value):
    principal[field] = value
    with pytest.raises(ValueError):
        reference.authorize(principal, 'tenant-a', audience='sandbox-klyrow',
                            azp='sandbox-middleware', subject='sandbox-middleware-service')


def test_unconfigured_auth_denied(reference, principal):
    with pytest.raises(ValueError):
        reference.authorize(principal, 'tenant-a', audience='', azp='', subject='')
