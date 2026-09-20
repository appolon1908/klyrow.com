import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs/database/source-structure-inventory.json"


def test_source_structure_inventory_matches_measured_metadata():
    inventory = json.loads(INVENTORY.read_text())
    assert inventory["authority"] == "apps/gateway/app/main.py:Base.metadata"
    assert inventory["table_count"] == 136
    assert inventory["tenant_scoped_count"] == 122
    assert inventory["non_tenant_scoped_count"] == 14
    assert len(inventory["tables"]) == inventory["table_count"]
    assert all(table["columns"] for table in inventory["tables"])


def test_structure_inventory_has_explicit_relationships():
    inventory = json.loads(INVENTORY.read_text())
    assert any(table["indexes"] for table in inventory["tables"])
    assert any(table["foreign_keys"] for table in inventory["tables"])
    assert any(table["constraints"] for table in inventory["tables"])
