# Billing Phase 0 traceability evidence

Base SHA: `fba9101416e0102f7d9b9f35c2256b85b8c18cf0` (`origin/main`)
Branch: `phase/00-billing-baseline`
Worktree: `C:\Users\Usuario\Desktop\klyrow-phase-00`
Scope: configuration, fail-closed startup validation, and documentation only.
No `PaymentAttempt`, provider adapter, ledger, journal, webhook runtime, or
billing UI is introduced by this branch.

## Requirement → implementation → test mapping

| Requirement | Implementation | Test(s) |
| --- | --- | --- |
| Billing feature flags default safely | `apps/gateway/app/billing_activation.py` `FLAG_NAMES`, `_read_flags` | `test_every_flag_defaults_safely_when_environment_is_empty` |
| Boolean parsing accepts documented forms only | `_boolean` (`1/true/yes/on`, `0/false/no/off`) | `test_boolean_parsing_accepts_documented_true_forms`, `test_boolean_parsing_accepts_documented_false_forms` |
| Invalid boolean values fail closed | `_boolean` raises `BillingActivationError` | `test_invalid_boolean_values_fail_closed` |
| Provider activation requires billing core | `validate_billing_activation` core-dependent check | `test_provider_activation_requires_billing_core` |
| Entitlements/dunning/live-charging require core | same core-dependent check | `test_entitlements_requires_billing_core`, `test_dunning_requires_billing_core`, `test_live_charging_requires_billing_core` |
| Missing / empty / unreadable secret references fail | `_validate_secret_reference` | `test_missing_secret_reference_fails`, `test_empty_secret_reference_value_fails`, `test_empty_secret_file_fails`, `test_unavailable_secret_file_fails` |
| Errors redact sensitive values and paths | `_validate_secret_reference` messages reference only the env var name | `test_errors_do_not_expose_secret_path_or_content` |
| Disabled configuration starts successfully | `validate_billing_activation({})` returns disabled state | `test_disabled_configuration_starts_successfully`, `test_fully_disabled_configuration_permits_gateway_startup` |
| Invalid enabled configuration blocks startup | `apps/gateway/app/main.py` `validate_billing_activation_on_startup` | `test_invalid_enabled_configuration_blocks_gateway_startup` |
| Configuration imports have no side effects | module has no top-level side-effecting code | `test_configuration_import_has_no_side_effects` |
| No provider network request occurs during validation | `billing_activation.py` has no HTTP/network client | inspection; no `httpx`/`socket` import in the module |
| Provider webhooks require an enabled provider and verification secret | `validate_billing_activation` webhook branch | `test_provider_webhooks_require_an_enabled_provider`, `test_provider_webhooks_require_verification_secret_reference`, `test_provider_webhooks_activate_with_all_secret_references` |
| Dunning requires entitlements | `validate_billing_activation` dunning branch | `test_dunning_requires_entitlements_even_with_core_enabled`, `test_dunning_activates_with_entitlements_enabled` |
| Live charging requires a valid provider | `validate_billing_activation` live-charging branch | `test_live_charging_requires_a_valid_provider`, `test_live_charging_activates_with_a_valid_provider` |
| Environment-example flags remain synchronized | `.env.example` documents every `KLYROW_BILLING_*` flag/reference, disabled by default | manual diff against `FLAG_NAMES`/`SECRET_REFERENCE_NAMES` |
| Architecture and operations documents exist | `docs/architecture/billing-invariants.md`, `docs/architecture/billing-target-architecture.md`, `docs/operations/billing-rollout-and-rollback.md`, `docs/adr/ADR-006-*`, `docs/adr/ADR-007-*` | file existence |
| CI contains a Phase 0 contract gate | `.github/workflows/ci.yml` `billing-phase-0` job | CI run |
| Generated inventories are current | `docs/security/secret-references.json` regenerated via `scripts/export-api-contracts.py` | `python scripts/export-api-contracts.py --check` |
| No PaymentAttempt/ledger/webhook runtime/provider adapter introduced | grep of this branch's diff | manual review; only `billing_activation.py`, `.env.example`, docs, CI, and the `main.py` startup hook changed |

## Commands run and evidence

- `python -m compileall -q apps/gateway` — passed (exit 0).
- `python -m pytest -q tests/test_billing_activation_config.py` — 39 passed.
- `python -m pytest -q tests/test_billing.py tests/test_billing_route_backlog.py tests/test_delivery_safety.py tests/test_billing_activation_config.py` — 80 passed.
- `python scripts/export-api-contracts.py --check` — passed after regenerating
  `docs/security/secret-references.json` with `python scripts/export-api-contracts.py`.
- `python -m pytest -q tests --ignore=tests/test_api.py` — result:
  **266 failed, 1029 passed, 22 skipped, 50 errors in 792.81s**. This Windows
  worktree is based on `origin/main` at `fba9101`, which predates the
  cross-platform portability fix (`.gitattributes`, `os.O_NOFOLLOW`/`O_NONBLOCK`
  fallback, `/tmp` path handling) present only on local `main` (`5262a1e`, not
  yet merged upstream). Every distinct failure/error cause observed is a known
  POSIX-only assumption, not a Phase 0 regression:
  - `AttributeError: module 'os' has no attribute 'O_NOFOLLOW'` in
    `apps/gateway/app/durable_keys.py` (Windows lacks this flag; fixed on local
    `main` but absent from this branch's `origin/main` base).
  - `AttributeError: <module 'os' (frozen)> has no attribute 'geteuid'`
    (`tests/test_mautic_secret_bootstrap.py`, POSIX-only permission checks).
  - `Failed: GPG is required for Mautic encrypted-backup regression tests`
    (`tests/test_mautic_volume_migration.py`, missing local GPG binary).
  - `PermissionError: [Errno 13] ... '\\tmp\\...'` (`tests/test_api.py`,
    hard-coded `/tmp`, the exact defect the unmerged portability commit
    fixed with `tempfile.gettempdir()`).
  - `sqlite3.OperationalError: no such table: ...` in several ingestion/mail
    tests, consistent with the same CRLF/checkout portability class of issue
    affecting fixture/migration ordering on this platform.
  None of these failures touch `billing_activation.py`, `.env.example`,
  `main.py`'s new startup hook, or `tests/test_billing_activation_config.py`.
  This is recorded as environmental, not a green full-suite run; remote
  Linux-based CI remains authoritative for full-suite acceptance.

## Known baseline context (informational only, not a substitute for this evidence)

A previously reported Linux-container run from the same base observed 1,322
passed, 23 skipped, 2 failures (backup/volume-migration tooling, one webhook
retry case), and 9 setup errors; a broader infrastructure-adjusted run passed
1,273 with 18 skipped. These figures are cited for context only and are not
reproduced as green in this Windows worktree.
