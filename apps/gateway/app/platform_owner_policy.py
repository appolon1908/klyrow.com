"""Fail-closed policy for the single Klyrow platform-owner identity.

The platform role remains Klyrow-owned authorization data. This module adds a
second, non-bypassable identity gate: a platform administrator must also be the
exact configured Keycloak issuer/subject pair and present verified, recent MFA
claims. A mailbox is supporting metadata and can never grant authority alone.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from email_validator import EmailNotValidError, validate_email

CANONICAL_ISSUER = "https://auth.codestra.co/realms/codestra"
MFA_POSSESSION_AMR_VALUES = frozenset({"fido", "fido2", "hwk", "otp", "swk", "totp", "webauthn"})
MFA_KNOWLEDGE_AMR_VALUES = frozenset({"pin", "pwd"})
# No ACR label is trusted by default. A protected deployment may opt in only
# after the exact Keycloak realm flow has been reviewed and evidenced.
DEFAULT_REQUIRED_ACR_VALUES: frozenset[str] = frozenset()


class PlatformOwnerError(ValueError):
    """A sanitized, HTTP-compatible platform-owner authorization failure."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class PlatformOwnerConfig:
    issuer: str
    subject: str
    email: str
    step_up_max_age_seconds: int = 300
    required_acr_values: frozenset[str] = DEFAULT_REQUIRED_ACR_VALUES

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, str],
        *,
        canonical_issuer: str = CANONICAL_ISSUER,
    ) -> PlatformOwnerConfig:
        issuer = str(values.get("KLYROW_PLATFORM_OWNER_ISSUER", "")).strip()
        subject = str(values.get("KLYROW_PLATFORM_OWNER_SUBJECT", "")).strip()
        email = str(values.get("KLYROW_PLATFORM_OWNER_EMAIL", "")).strip().lower()
        if not issuer or not subject or not email:
            raise PlatformOwnerError(503, "platform_owner_not_configured")
        if issuer != canonical_issuer:
            raise PlatformOwnerError(503, "platform_owner_issuer_misconfigured")
        if len(subject) > 255 or any(char in subject for char in "\r\n"):
            raise PlatformOwnerError(503, "platform_owner_subject_misconfigured")
        try:
            mailbox = validate_email(email, check_deliverability=False)
        except EmailNotValidError as exc:
            raise PlatformOwnerError(503, "platform_owner_email_misconfigured") from exc
        if mailbox.normalized.lower() != email:
            raise PlatformOwnerError(503, "platform_owner_email_misconfigured")

        raw_age = str(values.get("KLYROW_PLATFORM_OWNER_STEP_UP_MAX_AGE_SECONDS", "300")).strip()
        try:
            max_age = int(raw_age)
        except ValueError as exc:
            raise PlatformOwnerError(503, "platform_owner_step_up_misconfigured") from exc
        if not 60 <= max_age <= 900:
            raise PlatformOwnerError(503, "platform_owner_step_up_misconfigured")

        raw_acr = str(values.get("KLYROW_PLATFORM_OWNER_REQUIRED_ACR_VALUES", ""))
        acr_values = frozenset(item.strip() for item in raw_acr.split(",") if item.strip())
        acr_evidence_approved = (
            str(values.get("KLYROW_PLATFORM_OWNER_ACR_MFA_EVIDENCE_APPROVED", "false"))
            .strip()
            .lower()
        )
        if acr_evidence_approved not in {"true", "false"}:
            raise PlatformOwnerError(503, "platform_owner_acr_misconfigured")
        if acr_evidence_approved == "true":
            if not acr_values:
                raise PlatformOwnerError(503, "platform_owner_acr_misconfigured")
        else:
            acr_values = frozenset()

        return cls(
            issuer=issuer,
            subject=subject,
            email=email,
            step_up_max_age_seconds=max_age,
            required_acr_values=acr_values,
        )

    @classmethod
    def from_env(cls, *, canonical_issuer: str = CANONICAL_ISSUER) -> PlatformOwnerConfig:
        return cls.from_mapping(os.environ, canonical_issuer=canonical_issuer)


def _integer_claim(claims: Mapping[str, object], name: str) -> int:
    value = claims.get(name)
    if isinstance(value, bool):
        raise PlatformOwnerError(403, "platform_owner_claims_invalid")
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise PlatformOwnerError(403, "platform_owner_claims_invalid") from exc


def _mfa_present(claims: Mapping[str, object], required_acr_values: frozenset[str]) -> bool:
    raw_amr = claims.get("amr", ())
    if isinstance(raw_amr, str):
        amr = {raw_amr.lower()}
    elif isinstance(raw_amr, (list, tuple, set, frozenset)):
        amr = {str(item).lower() for item in raw_amr}
    else:
        amr = set()
    if "mfa" in amr:
        return True
    if amr & MFA_KNOWLEDGE_AMR_VALUES and amr & MFA_POSSESSION_AMR_VALUES:
        return True
    return bool(required_acr_values) and str(claims.get("acr") or "") in required_acr_values


def validate_platform_owner_claims(
    config: PlatformOwnerConfig,
    *,
    identity_issuer: str,
    identity_subject: str,
    claims: Mapping[str, object],
    now_epoch: int,
) -> None:
    """Require the DB identity and the stored validated ID token to agree.

    The caller must invoke this only after Klyrow has independently confirmed a
    platform-administrator role. Exact identity is an additional requirement;
    it never creates or elevates a role by itself.
    """

    if identity_issuer != config.issuer or identity_subject != config.subject:
        raise PlatformOwnerError(403, "platform_owner_identity_mismatch")
    if str(claims.get("iss") or "") != config.issuer:
        raise PlatformOwnerError(403, "platform_owner_identity_mismatch")
    if str(claims.get("sub") or "") != config.subject:
        raise PlatformOwnerError(403, "platform_owner_identity_mismatch")

    email = str(claims.get("email") or "").strip().lower()
    if claims.get("email_verified") is not True or email != config.email:
        raise PlatformOwnerError(403, "platform_owner_email_unverified")

    if not _mfa_present(claims, config.required_acr_values):
        raise PlatformOwnerError(403, "platform_owner_mfa_required")

    issued_at = _integer_claim(claims, "iat")
    expires_at = _integer_claim(claims, "exp")
    auth_time = _integer_claim(claims, "auth_time")
    if issued_at > now_epoch + 30 or auth_time > now_epoch + 30:
        raise PlatformOwnerError(403, "platform_owner_claims_invalid")
    if expires_at <= now_epoch:
        raise PlatformOwnerError(401, "platform_owner_token_expired")
    if now_epoch - auth_time > config.step_up_max_age_seconds:
        raise PlatformOwnerError(403, "platform_owner_step_up_required")
