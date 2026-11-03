<!-- CODESTRA-GOVERNANCE-V3:BEGIN -->
# Native GitHub source development contract

Standalone family: Klyrow
Component: klyrow.com

Mandatory hierarchy:
Product -> Section -> Subsection -> Atomic Task

Branch hierarchy for planned environment promotions:
subsection -> section -> development -> testing -> staging -> production

Native-reviewed source repair PRs may target protected branches under effective
GitHub rules.

Agent rules:
- one active writer per assigned subsection or source-repair task, recorded in the issue and GitHub PR;
- work only in the assigned branch and isolated worktree;
- implementation + tests + evidence + commit + push are required;
- review-only output is not completion;
- no force push and no direct protected-environment writes;
- every source promotion requires current-SHA GitHub Actions checks and effective repository protections, including independent review, signed commits and resolved review threads;
- custom controller statuses and database leases are not source merge prerequisites;
- dirty, stale, divergent, dependency-incomplete, unreviewed work or failing required checks fail closed.

GitHub is the source merge authority. Source integrity inspection checks Git
contents only; it does not certify runtime behavior or authorize deployment.

Production safety:
- PRODUCTION_GO=NO
- LIVE_CAPABILITIES_ENABLED=NO
- EXTERNAL_EFFECTS=false

Live calls, SMS, email, WhatsApp, payments, publishing, credential issuance,
production database writes, and production infrastructure mutation remain disabled
until separately certified and explicitly approved.
<!-- CODESTRA-GOVERNANCE-V3:END -->


# Existing repository-specific instructions

<!-- CODESTRA_AGENT_PROTOCOL_V1 -->
## Codestra continuation contract

Canonical protocol:
https://github.com/ingtrader21-spec/codestra/blob/main/docs/AGENT-CONTINUATION-PROTOCOL.md

Quick start:
https://github.com/ingtrader21-spec/codestra/blob/main/docs/AGENT-QUICKSTART.md

Before changing code:
1. Read `.codestra-mission/*` when present.
2. Read the active Linear issue and linked Notion architecture.
3. Inspect exact Git branch/HEAD/dirty/worktree/upstream/PR/CI state.
4. Preserve all existing local work.
5. If acting as Builder, verify exclusive issue ownership and use a dedicated worktree.
6. Do not invent or self-assign the next task.
7. Update GitHub + Linear + Notion + the mission checkpoint before handoff.
8. Do not cross the live-production approval boundary.

The canonical protocol's no-loss, one-writer, protected-merge, checkpoint, and production-boundary rules are mandatory.
