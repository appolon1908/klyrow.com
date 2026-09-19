#!/usr/bin/env python3
"""Fail-closed validation of release-bound M33 acceptance evidence.

Run inside the protected release job after artifact signature verification.
This verifies completeness, freshness and artifact binding, not signatures or
the truth of arbitrary user-authored assertions. It never activates delivery.
"""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re

REQUIRED_GATES = (
    "openapi", "asyncapi", "keycloak", "openbao", "kong", "klyrow_send",
    "klyrow_webhook", "telnexa_send", "telnexa_dlr", "telnexa_usage",
    "middleware_odoo", "klyrow_odoo_denied", "telnexa_odoo_denied",
    "grafana_odoo_denied", "prometheus_odoo_denied", "alertmanager_odoo_denied",
    "superset_write_denied", "odoo_outage_recovery", "observability_outage",
    "backup_restore", "alert_incident_recovery", "telemetry_ready",
)


def validate(document, *, source_sha, image_digest, artifacts, now=None):
    now = now or datetime.now(timezone.utc)
    failures = []
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        return ["invalid_expected_source_sha"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest):
        return ["invalid_expected_image_digest"]
    if document.get("source_sha") != source_sha or document.get("image_digest") != image_digest:
        failures.append("release_identity_mismatch")
    gates = document.get("gates", {})
    if not isinstance(gates, dict):
        return failures + ["invalid_gate_map"]
    for name in REQUIRED_GATES:
        gate = gates.get(name)
        if not isinstance(gate, dict) or gate.get("status") != "PASS":
            failures.append(name+":not_passed")
            continue
        try:
            observed = datetime.fromisoformat(gate["observed_at"].replace("Z", "+00:00"))
            if observed.tzinfo is None or not now-timedelta(hours=24) <= observed <= now:
                raise ValueError("invalid observation time")
            filename = gate["artifact"]
            if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", filename):
                raise ValueError("invalid artifact filename")
            path = artifacts / filename
            if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
                raise ValueError("missing evidence")
            if hashlib.sha256(path.read_bytes()).hexdigest() != gate["sha256"]:
                raise ValueError("evidence digest mismatch")
        except (KeyError, TypeError, ValueError, OSError):
            failures.append(name+":invalid_evidence")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    try:
        document = json.loads(args.evidence.read_text())
        failures = validate(document, source_sha=args.source_sha, image_digest=args.image_digest,
                            artifacts=args.artifacts)
    except (OSError, ValueError, TypeError, AttributeError):
        failures = ["acceptance_evidence_unavailable_or_invalid"]
    print(json.dumps({"status": "FAIL" if failures else "PASS", "failures": failures}))
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
