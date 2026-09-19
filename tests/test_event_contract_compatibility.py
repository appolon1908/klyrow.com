import copy
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("event_compatibility", ROOT/"scripts/check-event-compatibility.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def test_changed_referenced_event_payload_or_removed_stream_is_rejected():
    old = json.loads((ROOT/"schemas/asyncapi/codestra-events.yaml").read_text())
    new = copy.deepcopy(old)
    new["components"]["messages"]["klyrow"]["payload"]["properties"]["tenant_id"]["type"] = "integer"
    assert gate.changes(old, new)
    new = copy.deepcopy(old)
    del new["channels"]["klyrow"]
    assert gate.changes(old, new)
    new = copy.deepcopy(old)
    new["channels"]["new_version"] = {"address": "codestra.new.v2", "messages": {}}
    assert gate.changes(old, new) == []


def test_json_schema_cannot_be_changed_in_place():
    assert gate.changes({"type": "string"}, {"type": "integer"})
