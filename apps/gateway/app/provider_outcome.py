"""Fail-closed handling for ambiguous provider submission outcomes."""

import httpx


INDETERMINATE = "INDETERMINATE"


class ProviderSubmissionIndeterminate(RuntimeError):
    """Transport crossed its submission boundary without a durable outcome.

    Messages must be constant safe codes, never raw provider replies or content.
    """


def provider_outcome_is_ambiguous(exc: Exception) -> bool:
    """Return true when the selected provider may already have accepted work."""

    if isinstance(exc, ProviderSubmissionIndeterminate):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError))


def reconcile_before_retry(
    *,
    state: str,
    provider_message_id: str | None,
    provider_absence_confirmed: bool,
) -> bool:
    """Permit an indeterminate retry only after authoritative absence proof."""

    if state != INDETERMINATE:
        return True
    if provider_message_id:
        return False
    return provider_absence_confirmed is True
