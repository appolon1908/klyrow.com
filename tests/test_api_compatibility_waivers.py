"""The compatibility gate may waive a reviewed removal, and nothing else."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WAIVERS = ROOT / "contracts/openapi/compatibility-waivers.json"

spec = importlib.util.spec_from_file_location(
    "compatibility", ROOT / "scripts/check-api-compatibility.py"
)
compatibility = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compatibility)


def _doc(*operations: tuple[str, str]) -> dict:
    paths: dict = {}
    for method, path in operations:
        paths.setdefault(path, {})[method] = {
            "operationId": f"{method}_{path}",
            "security": [],
            "responses": {"200": {"description": "ok"}},
        }
    return {"paths": paths, "components": {}}


def test_unwaived_removal_still_fails():
    old = _doc(("post", "/v1/a"), ("get", "/v1/b"))
    new = _doc(("get", "/v1/b"))
    assert compatibility.changes(old, new) == ["POST /v1/a: operation removed"]


def test_waived_removal_passes_but_other_changes_do_not():
    old = _doc(("post", "/v1/a"), ("get", "/v1/b"))
    new = _doc(("get", "/v1/b"))
    assert compatibility.changes(old, new, frozenset({"POST /v1/a"})) == []
    new["paths"]["/v1/b"]["get"]["operationId"] = "renamed"
    assert compatibility.changes(old, new, frozenset({"POST /v1/a"})) == [
        "GET /v1/b: operationId contract changed"
    ]


def test_waiver_file_entries_are_complete_and_scoped(tmp_path: Path):
    labels = compatibility.load_waivers(WAIVERS, "klyrow-public-api.yaml")
    assert "POST /v1/team/invitations/accept" in labels
    assert compatibility.load_waivers(WAIVERS, "klyrow-internal-api.yaml") == frozenset()
    incomplete = tmp_path / "waivers.json"
    incomplete.write_text(
        json.dumps({"removed_operations": [{"spec": "x.yaml", "method": "GET", "path": "/v1/x"}]})
    )
    with pytest.raises(ValueError, match="reason"):
        compatibility.load_waivers(incomplete, "x.yaml")


def test_waived_operations_are_absent_from_current_spec():
    """A waiver outlives its purpose once the base no longer has the operation."""
    document = json.loads((ROOT / "schemas/openapi/klyrow-public-api.yaml").read_text())
    for label in compatibility.load_waivers(WAIVERS, "klyrow-public-api.yaml"):
        method, path = label.split(" ", 1)
        assert method.lower() not in document["paths"].get(path, {}), label
