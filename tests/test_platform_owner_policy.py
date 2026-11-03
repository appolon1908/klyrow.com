from __future__ import annotations

import pytest

from apps.gateway.app.platform_owner_policy import (
    CANONICAL_ISSUER,
    PlatformOwnerConfig,
    PlatformOwnerError,
    validate_platform_owner_claims,
)

NOW = 2_000_000_000
SUBJECT = "f6b08ad0-7a9a-4c39-922f-93ce91d76a8b"
EMAIL = "owner@example.com"


def config(**updates: str) -> PlatformOwnerConfig:
    values = {
        "KLYROW_PLATFORM_OWNER_ISSUER": CANONICAL_ISSUER,
        "KLYROW_PLATFORM_OWNER_SUBJECT": SUBJECT,
        "KLYROW_PLATFORM_OWNER_EMAIL": EMAIL,
        "KLYROW_PLATFORM_OWNER_STEP_UP_MAX_AGE_SECONDS": "300",
        "KLYROW_PLATFORM_OWNER_REQUIRED_ACR_VALUES": "2,3,urn:codestra:loa:2",
        "KLYROW_PLATFORM_OWNER_ACR_MFA_EVIDENCE_APPROVED": "true",
    }
    values.update(updates)
    return PlatformOwnerConfig.from_mapping(values)


def claims(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "iss": CANONICAL_ISSUER,
        "sub": SUBJECT,
        "email": EMAIL,
        "email_verified": True,
        "amr": ["pwd", "otp"],
        "acr": "2",
        "iat": NOW - 10,
        "auth_time": NOW - 20,
        "exp": NOW + 240,
    }
    values.update(updates)
    return values


def validate(current_claims: dict[str, object], **identity: str) -> None:
    validate_platform_owner_claims(
        config(),
        identity_issuer=identity.get("identity_issuer", CANONICAL_ISSUER),
        identity_subject=identity.get("identity_subject", SUBJECT),
        claims=current_claims,
        now_epoch=NOW,
    )


def failure_detail(callable_) -> str:
    with pytest.raises(PlatformOwnerError) as caught:
        callable_()
    return caught.value.detail


def test_exact_subject_verified_mail_mfa_and_fresh_step_up_pass() -> None:
    validate(claims())


def test_email_match_alone_never_grants_owner_authority() -> None:
    assert (
        failure_detail(
            lambda: validate(
                claims(sub="attacker-subject"),
                identity_subject="attacker-subject",
            )
        )
        == "platform_owner_identity_mismatch"
    )


def test_database_identity_and_token_identity_must_both_match() -> None:
    assert (
        failure_detail(lambda: validate(claims(), identity_subject="different-database-subject"))
        == "platform_owner_identity_mismatch"
    )


def test_verified_mailbox_is_required_as_secondary_evidence() -> None:
    assert (
        failure_detail(lambda: validate(claims(email_verified=False)))
        == "platform_owner_email_unverified"
    )
    assert (
        failure_detail(lambda: validate(claims(email="other@example.com")))
        == "platform_owner_email_unverified"
    )


def test_mfa_is_required() -> None:
    assert (
        failure_detail(lambda: validate(claims(amr=["pwd"], acr="1")))
        == "platform_owner_mfa_required"
    )
    assert (
        failure_detail(lambda: validate(claims(amr=["otp"], acr="1")))
        == "platform_owner_mfa_required"
    )


def test_acr_label_is_ignored_without_approved_realm_evidence() -> None:
    values = {
        "KLYROW_PLATFORM_OWNER_ISSUER": CANONICAL_ISSUER,
        "KLYROW_PLATFORM_OWNER_SUBJECT": SUBJECT,
        "KLYROW_PLATFORM_OWNER_EMAIL": EMAIL,
        "KLYROW_PLATFORM_OWNER_STEP_UP_MAX_AGE_SECONDS": "300",
        "KLYROW_PLATFORM_OWNER_REQUIRED_ACR_VALUES": "2",
    }
    unapproved = PlatformOwnerConfig.from_mapping(values)
    assert unapproved.required_acr_values == frozenset()
    with pytest.raises(PlatformOwnerError, match="platform_owner_mfa_required"):
        validate_platform_owner_claims(
            unapproved,
            identity_issuer=CANONICAL_ISSUER,
            identity_subject=SUBJECT,
            claims=claims(amr=["pwd"], acr="2"),
            now_epoch=NOW,
        )

    approved = PlatformOwnerConfig.from_mapping(
        {
            **values,
            "KLYROW_PLATFORM_OWNER_ACR_MFA_EVIDENCE_APPROVED": "true",
        }
    )
    validate_platform_owner_claims(
        approved,
        identity_issuer=CANONICAL_ISSUER,
        identity_subject=SUBJECT,
        claims=claims(amr=["pwd"], acr="2"),
        now_epoch=NOW,
    )


def test_invalid_or_empty_approved_acr_configuration_fails_closed() -> None:
    with pytest.raises(PlatformOwnerError, match="platform_owner_acr_misconfigured"):
        config(KLYROW_PLATFORM_OWNER_ACR_MFA_EVIDENCE_APPROVED="maybe")
    with pytest.raises(PlatformOwnerError, match="platform_owner_acr_misconfigured"):
        config(
            KLYROW_PLATFORM_OWNER_ACR_MFA_EVIDENCE_APPROVED="true",
            KLYROW_PLATFORM_OWNER_REQUIRED_ACR_VALUES="",
        )


def test_stale_authentication_requires_step_up() -> None:
    assert (
        failure_detail(lambda: validate(claims(auth_time=NOW - 301)))
        == "platform_owner_step_up_required"
    )


def test_expired_token_is_rejected() -> None:
    with pytest.raises(PlatformOwnerError) as caught:
        validate(claims(exp=NOW))
    assert caught.value.status_code == 401
    assert caught.value.detail == "platform_owner_token_expired"


def test_incomplete_or_noncanonical_configuration_fails_closed() -> None:
    with pytest.raises(PlatformOwnerError, match="platform_owner_not_configured"):
        PlatformOwnerConfig.from_mapping({})
    with pytest.raises(PlatformOwnerError, match="platform_owner_issuer_misconfigured"):
        config(KLYROW_PLATFORM_OWNER_ISSUER="https://example.invalid/realms/codestra")


@pytest.mark.parametrize(
    "mailbox",
    [
        "owner@gmail",
        "@example.com",
        "owner@",
        "owner@@example.com",
        "owner name@example.com",
        "Owner <owner@example.com>",
        "owner@example..com",
        "owner@example.com\\r\\nBcc:other@example.com",
    ],
)
def test_owner_configuration_requires_a_complete_mailbox(mailbox):
    with pytest.raises(PlatformOwnerError, match="platform_owner_email_misconfigured"):
        config(KLYROW_PLATFORM_OWNER_EMAIL=mailbox)


@pytest.mark.parametrize("field", ["identity_issuer", "token_issuer"])
def test_owner_issuer_comparison_is_exact(field):
    identity = {"identity_issuer": CANONICAL_ISSUER}
    token = claims()
    if field == "identity_issuer":
        identity["identity_issuer"] += "/"
    else:
        token["iss"] = CANONICAL_ISSUER + "/"
    assert failure_detail(lambda: validate(token, **identity)) == "platform_owner_identity_mismatch"
