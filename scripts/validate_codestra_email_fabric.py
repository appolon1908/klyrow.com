#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def validate() -> None:
    path = ROOT / "docs" / "integrations" / "codestra-fabric" / "manifest.v2.json"
    with path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    assert manifest["integration_boundary"] == "MIDDLEWARE_ONLY"
    assert manifest["n8n_direct_access"] is False
    assert manifest["postal_direct_access"] is False
    assert manifest["mautic_direct_access"] is False
    assert manifest["security_email_synchronous_n8n"] is False
    assert manifest["service_identity"] == "klyrow-adapter"
    assert not any(manifest["capabilities"].values())


if __name__ == "__main__":
    validate()
    print("KLYROW_CODESTRA_EMAIL_FABRIC=PASS")
