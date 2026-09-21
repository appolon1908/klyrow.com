"""Worker-facing, read-only billing reconciliation command."""
from __future__ import annotations

from sqlalchemy.orm import Session

from .billing_reconciliation import reconcile_billing


def run_billing_reconciliation(session: Session, *, tenant_id: str | None = None) -> dict:
    """Return a stable report suitable for an operator or scheduled worker.

    Reconciliation is intentionally read-only. A caller may persist the report
    or audit command outcome, but this function never changes billing history.
    """
    issues = reconcile_billing(session, tenant_id=tenant_id)
    return {
        "tenant_id": tenant_id,
        "status": "PASS" if not issues else "DRIFT",
        "issue_count": len(issues),
        "issues": [issue.as_dict() for issue in issues],
    }
