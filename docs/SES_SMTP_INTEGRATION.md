# Codestra Klyrow SES SMTP (disabled by default)

AWS region: us-east-1. The SES source domains initially authorized for
the Klyrow transport are klyrow.com and codestra.agency.

The existing Klyrow outbox keeps Postal as its default transport. SES is
explicitly opt-in through KLYROW_EMAIL_TRANSPORT=ses. The dedicated mail
worker declares the approved transport and configuration set directly so it
does not depend on a domain-level SES default. Its mandatory runtime settings:

KLYROW_EMAIL_TRANSPORT=ses
KLYROW_SES_REGION=us-east-1
KLYROW_SES_CONFIGURATION_SET=codestra-klyrow-transactional
KLYROW_SES_CANARY_ONLY=true
KLYROW_SES_LIVE_APPROVED=false
KLYROW_SES_SMTP_USERNAME_FILE=/run/secrets/klyrow_ses_smtp_username
KLYROW_SES_SMTP_PASSWORD_FILE=/run/secrets/klyrow_ses_smtp_password

Secrets must be regular absolute-path files owned by the deployed Klyrow
service identity (UID/GID 10001 in the protected image), mode 0400 or 0600,
not symlinks, and never embedded in Git, logs, or Docker environment literals.
A root-owned mode-0400 source is intentionally unreadable to the non-root
worker. On the host, provide service-owned files through
KLYROW_SES_SMTP_USERNAME_SOURCE_FILE and
KLYROW_SES_SMTP_PASSWORD_SOURCE_FILE. All five Klyrow delivery switches
continue to apply.

Canary-only mode permits only one simulated transactional recipient,
success@simulator.amazonses.com. This uses SES SMTP on port 587 with
mandatory certificate-verified STARTTLS, SMTP authentication and SES
configuration-set tagging. Production sending is NOT authorized by this mode.

To leave canary-only mode requires separately setting
KLYROW_SES_CANARY_ONLY=false and KLYROW_SES_LIVE_APPROVED=true in a reviewed
release after AWS permissions, signed event callbacks, reputation,
bounce/complaint suppression and end-to-end reconciliation pass. SMTP 250
acknowledgement means accepted by SES, not confirmed delivered to recipient.
Keep live delivery off until the full release gate has passed.

No public SMTP port is needed for sending. Do not change existing MX,
website A records, main SPF, or expose Odoo/PostgreSQL.
