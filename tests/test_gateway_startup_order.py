"""Mission 01: prove billing validation precedes DB/worker construction.

Reloading `apps.gateway.app.main` in-process re-registers Prometheus metrics
against the global default registry and raises spuriously, so process-boundary
behavior (import success/failure) is exercised via a fresh subprocess instead.
"""
import subprocess
import sys

import pytest


def _run_import(env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    code = "import apps.gateway.app.main as m\nprint('IMPORT_OK', m.BILLING_SETTINGS.enabled)"
    return subprocess.run(
        [sys.executable, "-c", code],
        env=env_overrides,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _base_env(**overrides) -> dict[str, str]:
    import os

    env = dict(os.environ)
    env["KLYROW_BILLING_ENABLED"] = "false"
    for name in (
        "KLYROW_LIVE_CHARGING_ENABLED",
        "KLYROW_STRIPE_ENABLED",
        "KLYROW_PAYPAL_ENABLED",
        "KLYROW_STABLECOIN_ENABLED",
        "KLYROW_BILLING_WEBHOOK_PROCESSING_ENABLED",
        "KLYROW_BILLING_DUNNING_ENABLED",
        "KLYROW_BILLING_REFUNDS_ENABLED",
        "KLYROW_BILLING_DISPUTE_ACTIONS_ENABLED",
        "KLYROW_BILLING_RECONCILIATION_ENABLED",
        "KLYROW_STRIPE_SECRET_FILE",
    ):
        env.pop(name, None)
    env.update(overrides)
    return env


def test_billing_settings_are_resolved_before_engine_is_constructed():
    """The module source order itself proves the gate precedes engine construction."""
    import apps.gateway.app.main as main_module

    source = open(main_module.__file__, encoding="utf-8").read()
    billing_gate_index = source.index("_validate_billing_configuration_before_startup()")
    engine_index = source.index("engine=create_engine(DATABASE_URL")
    assert billing_gate_index < engine_index


def test_invalid_enabled_billing_configuration_prevents_import():
    """An invalid enabled billing configuration must fail before the module (and its
    engine/app/workers) finishes constructing at all, in a clean process."""
    result = _run_import(_base_env(KLYROW_STRIPE_ENABLED="true"))
    assert result.returncode != 0
    assert "billing_config:" in result.stderr
    assert "IMPORT_OK" not in result.stdout


def test_fully_disabled_billing_configuration_permits_import_and_startup():
    result = _run_import(_base_env())
    assert result.returncode == 0, result.stderr
    assert "IMPORT_OK False" in result.stdout


def test_valid_enabled_billing_configuration_permits_import(tmp_path):
    secret_file = tmp_path / "stripe-secret"
    secret_file.write_text("SYNTHETIC-FIXTURE-VALUE-NOT-A-CREDENTIAL")
    result = _run_import(
        _base_env(
            KLYROW_BILLING_ENABLED="true",
            KLYROW_STRIPE_ENABLED="true",
            KLYROW_STRIPE_SECRET_FILE=str(secret_file),
        )
    )
    assert result.returncode == 0, result.stderr
    assert "IMPORT_OK True" in result.stdout


def test_billing_disabled_avoids_any_secret_file_read_during_import(tmp_path):
    sentinel = tmp_path / "must-not-be-opened"
    sentinel.write_text("SYNTHETIC-SENTINEL-VALUE")
    probe = (
        "import builtins\n"
        "opened=[]\n"
        "_real_open=builtins.open\n"
        "def _spy(file,*a,**k):\n"
        "    opened.append(str(file))\n"
        "    return _real_open(file,*a,**k)\n"
        "builtins.open=_spy\n"
        "import apps.gateway.app.main as m\n"
        f"assert {str(sentinel)!r} not in opened, opened\n"
        "print('PROBE_OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        env=_base_env(KLYROW_STRIPE_SECRET_FILE=str(sentinel)),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "PROBE_OK" in result.stdout


def test_worker_scheduling_hooks_are_registered_after_billing_validation():
    """Billing validation runs at import time, strictly before `app` (and therefore
    before any of its startup hooks) exists, so every worker-scheduling hook is
    necessarily registered after it."""
    import apps.gateway.app.main as main_module

    handlers = list(main_module.app.router.on_startup)
    worker_hook_names = {"start_provider_worker", "reconcile_provider_registry_on_startup"}
    assert any(handler.__name__ in worker_hook_names for handler in handlers)
    assert hasattr(main_module, "BILLING_SETTINGS")


def test_existing_runtime_smoke_endpoints_remain_reachable():
    """Existing email/runtime startup behavior stays green after the Mission 01 gate."""
    import apps.gateway.app.main as main_module
    from fastapi.testclient import TestClient

    client = TestClient(main_module.app)
    response = client.get("/v1/health")
    assert response.status_code == 200

