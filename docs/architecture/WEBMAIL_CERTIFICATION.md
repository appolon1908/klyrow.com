# Klyrow Client Email Suite / Webmail Certification Authority

Mission: PAS-195 / M1E

Protected-main baseline: ca54d5567d719d7aa09098dca1d5e85c3bd76903

This document freezes the source-owned browser Webmail contract without regenerating shared billing/OpenAPI artifacts. The authoritative implementation remains apps/gateway/app/webmail.py, apps/gateway/app/webmail_models.py, and apps/web/src/Webmail.vue.

## Browser application

- UI route: /app/mail and descendants.
- Authentication: existing Klyrow OIDC browser session; no second mailbox password.
- UI states: loading, error, empty, ready.
- Folders: INBOX, STARRED, SENT, DRAFTS, ARCHIVE, SPAM, TRASH.
- Message HTML is never rendered with v-html; the bundled client renders the plain-text body and treats attachments as explicit downloads.

## Browser BFF contract

| Method | Path | Purpose | Mutation guard |
| --- | --- | --- | --- |
| GET | /app/api/mailboxes | tenant/access-scoped mailbox list and counts | browser session |
| POST | /app/api/mailboxes/sync | materialize verified senders as mailboxes | manager + CSRF |
| POST | /app/api/mailboxes/inbound/activate | reconcile Postal + exact inbound routes | manager + CSRF |
| GET | /app/api/mailboxes/{mailbox_id}/messages | folder/search message list | mailbox grant |
| GET | /app/api/mailboxes/{mailbox_id}/messages/{message_id} | message detail | mailbox grant |
| GET | /app/api/mailboxes/{mailbox_id}/messages/{message_id}/attachments/{attachment_id} | attachment download | mailbox grant |
| POST | /app/api/mailboxes/{mailbox_id}/drafts | create draft | sender/owner grant + CSRF |
| PUT | /app/api/mailboxes/{mailbox_id}/drafts/{message_id} | update draft | sender/owner grant + CSRF |
| POST | /app/api/mailboxes/{mailbox_id}/send | governed transactional send | sender/owner grant + CSRF + Idempotency-Key |
| PATCH | /app/api/mailboxes/{mailbox_id}/messages/{message_id} | read/star/folder state | mailbox grant; owner required for folder mutation |
| DELETE | /app/api/mailboxes/{mailbox_id}/messages/{message_id} | trash/permanent delete | owner/manager + CSRF |
| GET | /app/api/mailboxes/{mailbox_id}/access | list mailbox grants | tenant manager |
| POST | /app/api/mailboxes/{mailbox_id}/access | grant OWNER/SENDER/READER | tenant manager + CSRF |
| DELETE | /app/api/mailboxes/{mailbox_id}/access/{user_id} | revoke mailbox grant | tenant manager + CSRF |

## Authority boundaries

- Keycloak/OIDC: identity and browser session.
- Klyrow PostgreSQL: mailbox, message, draft, access, storage quota, exact inbound route.
- Klyrow mail engine/outbox: accepted outbound work, sender/policy/idempotency enforcement.
- Postal: transport and authenticated provider delivery/inbound evidence.
- Provider callbacks: delivery/inbound evidence only; never tenant/mailbox authorization.

## Inbound acceptance sequence

1. Klyrow requires a verified Klyrow domain and a provider domain in SENDING_ENABLED.
2. Only enabled exact senders whose local part is in the inbound allowlist are considered.
3. Klyrow asks the Postal provisioner to reconcile the exact address list.
4. The returned address set must exactly match the requested set or activation fails closed.
5. Klyrow reconciles the provider-domain credential and then enables only exact webmail routes.
6. Mailbox materialization marks receiving ready only when provider inbound state and the exact Klyrow route are both enabled.
7. Authenticated provider inbound evidence is copied into the matching tenant mailbox.
8. Duplicate provider inbound IDs are idempotent; attachment digests and storage quota are checked before persistence.

## Certification rule

Tasks 1-5 are source-certified only when the exact route inventory test, backend lifecycle tests, frontend unit/E2E tests, tenant/access denial tests, and controlled Postal-inbound contract test all pass on the same branch head. No external unrestricted delivery is required or authorized by this certification.
