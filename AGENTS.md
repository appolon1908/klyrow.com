<!-- CODESTRA-GOVERNANCE-V3:BEGIN -->
# Codestra Governed Development Contract v3

Standalone family: Klyrow
Component: klyrow.com

Mandatory hierarchy:
Product -> Section -> Subsection -> Atomic Task

Only authorized promotion:
subsection -> section -> development -> testing -> staging -> production

Agent rules:
- one active lease per subsection;
- work only in the assigned subsection branch/worktree;
- implementation + tests + evidence + commit + push are required;
- review-only output is not completion;
- no force push and no direct protected-environment writes;
- every promotion requires codestra-control-plane plus repository CI;
- dirty, stale, divergent, dependency-incomplete, or uncertified work fails closed.

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
