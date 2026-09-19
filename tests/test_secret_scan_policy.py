from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_secret_scan_policy_allows_only_exact_non_secret_values():
    policy = (ROOT / ".gitleaks.toml").read_text(encoding="utf-8")

    assert "paths =" not in policy
    assert "commits =" not in policy
    assert "[extend]" in policy
    assert "useDefault = true" in policy
    assert "invite_capability_0123456789abcdef" in policy
    assert "1198C0117593497A5EC5C199286AF1F9897469DC" in policy
    assert "C28D937575603EB4ABB725861C0779DC5C0A9DE4" in policy
    assert "AFD8691FDAEDF03BDF6E460563F15A9B715376CA" in policy


def test_full_history_suppressions_are_exact_reviewed_fingerprints():
    ignored = (ROOT / ".gitleaksignore").read_text(encoding="utf-8").splitlines()
    fingerprints = [line for line in ignored if line and not line.startswith("#")]

    assert fingerprints == [
        "ca9fd1a23b32e745b42fb0fcce2945e8d6e9f0e3:docker/mautic.Dockerfile:generic-api-key:61",
        "95301ef4f2a709c50d2297f022f17af37c8aae90:apps/web/e2e/auth.spec.ts:generic-api-key:35",
        "e5b8791d0d9c652b084943c5b5fb765b2b32e811:apps/web/e2e/auth.spec.ts:generic-api-key:35",
        "c5b36f73929d7078d4633e80498fce5be921fa57:apps/web/e2e/auth.spec.ts:generic-api-key:35",
        # Historical campaign TestClient Idempotency-Key values, not credentials.
        "6f8bf1f52e8abf4baf50e2ff266e6c33e5f92072:tests/test_m06_api_completion.py:generic-api-key:73",
        "6f8bf1f52e8abf4baf50e2ff266e6c33e5f92072:tests/test_m06_api_completion.py:generic-api-key:76",
        "6f8bf1f52e8abf4baf50e2ff266e6c33e5f92072:tests/test_m06_api_completion.py:generic-api-key:78",
        "6f8bf1f52e8abf4baf50e2ff266e6c33e5f92072:tests/test_m06_api_completion.py:generic-api-key:79",
        "6f8bf1f52e8abf4baf50e2ff266e6c33e5f92072:tests/test_m06_api_completion.py:generic-api-key:81",
        # Historical Phase 0 synthetic Stripe-shaped fixture values.
        "a7cfd683a34cf17c0c99d4c3bcc1968da420e6c8:tests/test_billing_activation_config.py:stripe-access-token:139",
        "a7cfd683a34cf17c0c99d4c3bcc1968da420e6c8:tests/test_billing_activation_config.py:stripe-access-token:163",
        "a7cfd683a34cf17c0c99d4c3bcc1968da420e6c8:tests/test_billing_activation_config.py:stripe-access-token:177",
        "a7cfd683a34cf17c0c99d4c3bcc1968da420e6c8:tests/test_billing_activation_config.py:stripe-access-token:219",
    ]
    assert all(len(line.split(":", 1)[0]) == 40 for line in fingerprints)
