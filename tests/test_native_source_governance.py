"""Source inspection cannot impersonate runtime or controller certification."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    return yaml.load((ROOT / '.github/workflows' / name).read_text(), Loader=yaml.BaseLoader)


def test_source_workflows_have_no_status_publication_authority():
    hierarchy = workflow('codestra-hierarchy-governance.yml')
    integrity = workflow('required-exact-sha-ci.yml')
    assert set(hierarchy['jobs']) == {'hierarchy-governance'}
    for data in (hierarchy, integrity):
        assert data['permissions'] == {'contents': 'read'}
        for job in data['jobs'].values():
            assert job['runs-on'] == 'ubuntu-24.04'
            assert 'permissions' not in job and 'environment' not in job
            scripts = '\n'.join(step.get('run', '') for step in job['steps'])
            assert '/statuses/' not in scripts
            assert 'gh api' not in scripts
            assert 'GH_TOKEN' not in scripts
    assert integrity['jobs']['exact-sha']['name'] == 'Source integrity'


@pytest.mark.parametrize('mutation', [
    ('promotion', 'chain', ['development', 'production']),
    ('promotion', 'force_push_allowed', True),
    ('promotion', 'require_exact_sha', False),
    ('promotion', 'require_clean_worktree', False),
    ('promotion', 'require_review', False),
    ('authority', 'source', 'custom-controller'),
    ('effects', 'production_go', True),
    ('effects', 'live_capabilities_enabled', True),
    ('effects', 'external_effects_enabled', True),
])
def test_manifest_validator_rejects_weakened_source_and_runtime_rules(tmp_path, mutation):
    data = yaml.safe_load((ROOT / '.codestra/control-plane.yml').read_text())
    script = next(step['run'] for step in workflow('codestra-hierarchy-governance.yml')
                  ['jobs']['hierarchy-governance']['steps'] if 'run' in step)
    manifest = tmp_path / '.codestra/control-plane.yml'
    manifest.parent.mkdir()
    manifest.write_text(yaml.safe_dump(data))
    valid = subprocess.run([sys.executable, '-c', script], cwd=tmp_path, capture_output=True)
    assert valid.returncode == 0, valid.stderr.decode()
    changed = copy.deepcopy(data)
    section, key, value = mutation
    changed[section][key] = value
    manifest.write_text(yaml.safe_dump(changed))
    invalid = subprocess.run([sys.executable, '-c', script], cwd=tmp_path, capture_output=True)
    assert invalid.returncode != 0, f'validator accepted weakened {section}.{key}'


def test_integrity_inspection_checks_real_git_head_and_diff(tmp_path):
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=tmp_path, text=True).strip()

    def commit(text):
        (tmp_path / 'sample.txt').write_text(text)
        git('add', 'sample.txt')
        git('-c', 'commit.gpgsign=false', '-c', 'user.name=Fixture',
            '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
        return git('rev-parse', 'HEAD')

    git('init', '-q')
    base = commit('base\n')
    head = commit('candidate\n')
    steps = workflow('required-exact-sha-ci.yml')['jobs']['exact-sha']['steps']
    prove = next(step['run'] for step in steps if step['name'] == 'Prove exact checkout')
    inspect = next(step['run'] for step in steps
                   if step['name'] == 'Validate committed candidate diff and repository integrity')
    env = dict(os.environ, EVENT_NAME='pull_request', BASE_SHA=base, EXPECTED_SHA=head)
    assert subprocess.run(['bash', '-c', prove], cwd=tmp_path, env=env).returncode == 0
    assert subprocess.run(['bash', '-c', inspect], cwd=tmp_path, env=env).returncode == 0
    assert subprocess.run(['bash', '-c', prove], cwd=tmp_path,
                          env=dict(env, EXPECTED_SHA=base)).returncode != 0
    bad = commit('trailing whitespace \n')
    assert subprocess.run(['bash', '-c', inspect], cwd=tmp_path,
                          env=dict(env, EXPECTED_SHA=bad), capture_output=True).returncode != 0


@pytest.mark.parametrize('event,sha,branch,accepted', [
    ('pull_request', 'a' * 40, 'topic', True),
    ('workflow_dispatch', 'a' * 40, 'development', True),
    ('workflow_dispatch', 'a' * 40, 'main', False),
    ('pull_request', 'refs/heads/development', 'topic', False),
    ('workflow_dispatch', 'a' * 39, 'development', False),
])
def test_integrity_inputs_require_full_sha_and_preserve_dispatch_lane(event, sha, branch, accepted):
    script = workflow('required-exact-sha-ci.yml')['jobs']['exact-sha']['steps'][0]['run']
    result = subprocess.run(['bash', '-c', script], env=dict(
        os.environ, EVENT_NAME=event, EXPECTED_SHA=sha, GITHUB_REF_NAME=branch,
    ), capture_output=True)
    assert (result.returncode == 0) is accepted


def test_native_policy_keeps_review_and_runtime_boundaries():
    policy = json.loads((ROOT / '.codestra/governance/policy.json').read_text())
    assert 'required_status' not in policy
    assert policy['source_authority'] == 'github_actions_and_repository_protections'
    assert policy['merge_requirements'] == {
        'required_checks': 'effective_repository_protections',
        'independent_review': True,
        'signed_commits': True,
        'resolved_review_threads': True,
    }
    assert policy['production'] == {
        'PRODUCTION_GO': 'NO', 'LIVE_CAPABILITIES_ENABLED': 'NO', 'EXTERNAL_EFFECTS': 'false',
    }
    assert policy['force_push'] is False and policy['direct_protected_branch_writes'] is False
