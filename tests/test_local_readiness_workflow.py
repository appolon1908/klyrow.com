"""Local reuse must preserve release boundaries and exact signing authority."""
from pathlib import Path
import re
import subprocess
import sys

import pytest
import yaml

from scripts.readiness_signing_identity import signing_identity_pattern

ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    return yaml.load((ROOT / '.github/workflows' / name).read_text(), Loader=yaml.BaseLoader)


def test_local_reuse_is_bound_to_same_commit_and_has_no_ambient_secrets():
    caller = workflow('codestra-deploy-readiness.yml')
    job = caller['jobs']['deploy-readiness']
    assert job['uses'] == './.github/workflows/reusable-codestra-deploy-readiness.yml'
    assert 'secrets' not in job
    assert job['with']['artifact_strategy'] == 'source-bundle'
    assert job['with']['run_stack_tests'] == 'false'
    local = workflow('reusable-codestra-deploy-readiness.yml')
    assert set(local['on']) == {'workflow_call'}
    assert local['permissions'] == {'contents': 'read'}
    assert local['jobs']['immutable-candidate']['needs'] == ['source-ci', 'secret-scan']


@pytest.mark.parametrize('job,environment,confirmation', [
    ('staging-readonly', 'staging-readonly', 'DEPLOY_STAGING_READONLY'),
    ('production-readonly-canary', 'production-readonly-canary', 'PROMOTE_PRODUCTION_READONLY'),
])
def test_deployment_remains_explicit_protected_and_read_only(job, environment, confirmation):
    data = workflow('reusable-codestra-deploy-readiness.yml')['jobs'][job]
    assert "github.event_name == 'workflow_dispatch'" in data['if']
    assert data['environment']['name'] == environment
    assert 'self-hosted' in data['runs-on']
    scripts = '\n'.join(step.get('run', '') for step in data['steps'])
    assert confirmation in scripts
    assert '--deny-external-effects' in scripts
    if job == 'production-readonly-canary':
        for gate in ('--allowed-methods GET,HEAD', '--require-staging-certification',
                     '--require-backup', '--require-isolated-restore', '--require-rollback-rehearsal'):
            assert gate in scripts
        assert '0 < value <= 1' in scripts


def test_all_artifact_verification_uses_exact_identity_and_oidc_issuer():
    steps = workflow('reusable-codestra-deploy-readiness.yml')['jobs']['immutable-candidate']['steps']
    scripts = '\n'.join(step.get('run', '') for step in steps)
    assert scripts.count('--certificate-identity-regexp "$identity_pattern"') == 4
    assert scripts.count('--certificate-oidc-issuer "https://token.actions.githubusercontent.com"') == 4
    assert 'Infustruction-repo' not in scripts
    assert '--severity HIGH,CRITICAL' in scripts
    assert '--exit-code 1' in scripts
    assert 'validate_provenance(' in scripts


@pytest.mark.parametrize('ref', ['main', 'staging', 'production'])
@pytest.mark.parametrize('name', ['codestra-deploy-readiness.yml', 'reusable-codestra-deploy-readiness.yml'])
def test_identity_accepts_only_current_repository_and_release_ref(ref, name):
    pattern = signing_identity_pattern('appolon1908-hue/klyrow.com', f'refs/heads/{ref}')
    identity = f'https://github.com/appolon1908-hue/klyrow.com/.github/workflows/{name}@refs/heads/{ref}'
    assert re.fullmatch(pattern, identity)
    for wrong in (identity.replace('klyrow.com', 'klyrowXcom'), identity + '/extra',
                  identity.replace('appolon1908-hue', 'another-owner'),
                  identity.replace(f'refs/heads/{ref}', 'refs/heads/unreviewed'),
                  identity.replace(name, 'other.yml')):
        assert not re.fullmatch(pattern, wrong)


@pytest.mark.parametrize('repository,ref', [
    ('owner/repo', 'refs/heads/topic'), ('owner/repo', 'refs/tags/main'),
    ('owner/repo/extra', 'refs/heads/main'), ('owner/repo\n', 'refs/heads/main'),
])
def test_identity_rejects_untrusted_release_context(repository, ref):
    with pytest.raises(ValueError):
        signing_identity_pattern(repository, ref)


def test_embedded_shell_scripts_parse_without_execution(tmp_path):
    for job in workflow('reusable-codestra-deploy-readiness.yml')['jobs'].values():
        for step in job['steps']:
            if 'run' in step:
                script = tmp_path / 'step.sh'
                script.write_text(step['run'])
                subprocess.run(['bash', '-n', str(script)], check=True, capture_output=True)
