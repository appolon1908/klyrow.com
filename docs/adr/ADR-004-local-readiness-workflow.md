# ADR-004: Repository-local deployment-readiness workflow

Status: accepted for source publication, 2026-09-13; runtime certification pending.

## Problem

Klyrow is public. Its caller references a reusable workflow in the private
`appolon1908-hue/Infustruction-repo` repository. GitHub Actions rejects that
visibility combination before running any job, even when an authenticated
maintainer can read the source file. Changing the pinned SHA or passing a
runtime credential cannot repair workflow resolution.

Reference: https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations

## Prepared decision

Use `./.github/workflows/reusable-codestra-deploy-readiness.yml`. GitHub resolves
that file from the caller's own commit. Vendor the existing workflow, preserving
source checks, secret scanning, vulnerability scanning, signing, SBOM/provenance
verification, protected environments, fixed confirmations, read-only canary
limits, backup/restore/rollback requirements and denial of external effects.
The main gateway workflow continues owning full application/image CI.

Upstream provenance:

- Repository: `appolon1908-hue/Infustruction-repo` (private)
- Path: `.github/workflows/reusable-codestra-deploy-readiness.yml`
- Commit: `f39ff21af74487c0c51cb46412658bc16f92f429`
- Git blob: `c17eac9dde8307fb9f4446681e40db749903c6b3`

The local adaptation replaces the private signing-identity alternative with an
escaped, exact repository/ref expression accepting the caller and local reusable
workflow. It accepts only main/staging/production release refs. No wildcard
repository or arbitrary branch identity is trusted. Self-hosted runner labels
are declared for linting; this does not provision runners or environments.

## Publication boundary

The repository owner authorized publication of this reviewed workflow copy
on 2026-09-13. The publication scope is this file and its local adaptation; the private infrastructure repository remains private. No credentials
are included. No deployment or manual release is authorized by this proposal.

## Validation and rollback

Local validation: 15 focused tests pass; actionlint validates both workflows;
all embedded shell scripts parse; the inherited repository-structure validator
passes against this checkout; Gitleaks directory scan passes.

Live Actions resolution, signing and protected-environment execution remain
unverified until the corresponding authorized runs.
In particular, the source-bundle scanner and external runner configuration may
reveal further readiness failures once the workflow can start. Do not mark
production readiness complete from these local checks.

Rollback: revert the local caller and vendored files. This restores the former
caller, including its known visibility failure, without changing runtime data,
secrets, repository visibility or branch-protection settings.
