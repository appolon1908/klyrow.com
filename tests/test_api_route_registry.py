import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "docs/api/route-registry.json"


def test_route_registry_is_complete_and_classified():
    rows = json.loads(REGISTRY.read_text())
    assert len(rows) == 466
    assert {row["classification"] for row in rows} <= {"KEEP", "COMPATIBILITY"}
    assert all(row["operation_id"] and row["method"] and row["path"] and row["handler"] for row in rows)
    assert all(row["audience"] for row in rows)
    assert any(row["classification"] == "COMPATIBILITY" for row in rows)
    assert any(row["classification"] == "KEEP" for row in rows)


def test_browser_mutations_have_csrf_metadata():
    rows = json.loads(REGISTRY.read_text())
    mutations = [
        row for row in rows
        if row["audience"] == "BROWSER_BFF" and row["method"] in {"POST", "PUT", "PATCH", "DELETE"}
    ]
    assert mutations
    assert all("required" in row["csrf"] for row in mutations)
