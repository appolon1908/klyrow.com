import copy
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "api_compatibility", ROOT / "scripts/check-api-compatibility.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def schema():
    return {
        "paths": {
            "/v1/messages": {
                "post": {
                    "operationId": "send_message",
                    "security": [{"bearer": []}],
                    "requestBody": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Message"}}
                        }
                    },
                    "responses": {"202": {"description": "Accepted"}},
                }
            }
        },
        "components": {
            "schemas": {"Message": {"type": "object", "properties": {"to": {"type": "string"}}}}
        },
    }


def test_ref_schema_change_is_detected_without_changing_operation():
    old = schema()
    new = copy.deepcopy(old)
    new["components"]["schemas"]["Message"]["required"] = ["to"]
    assert any("requestBody" in value for value in module.changes(old, new))


def test_added_optional_parameter_and_operation_are_compatible():
    old = schema()
    new = copy.deepcopy(old)
    new["paths"]["/v1/messages"]["get"] = {"responses": {"200": {"description": "OK"}}}
    new["paths"]["/v1/messages"]["post"]["parameters"] = [
        {"name": "tag", "in": "query", "required": False}
    ]
    assert module.changes(old, new) == []


def test_removing_acceptance_or_authentication_is_rejected():
    old = schema()
    new = copy.deepcopy(old)
    new["paths"]["/v1/messages"]["post"]["responses"] = {"200": {"description": "OK"}}
    new["paths"]["/v1/messages"]["post"]["security"] = []
    failures = module.changes(old, new)
    assert any("authentication" in value for value in failures)
    assert any("response 202" in value for value in failures)


def test_recursive_refs_terminate_and_detect_nested_change():
    old = schema()
    old["components"]["schemas"]["Message"]["properties"]["child"] = {
        "$ref": "#/components/schemas/Message"
    }
    assert module.changes(old, copy.deepcopy(old)) == []
    new = copy.deepcopy(old)
    new["components"]["schemas"]["Message"]["properties"]["to"]["type"] = "integer"
    assert module.changes(old, new)


def test_removing_operation_and_adding_required_parameter_are_rejected():
    old = schema()
    removed = copy.deepcopy(old)
    removed["paths"] = {}
    assert module.changes(old, removed) == ["POST /v1/messages: operation removed"]
    changed = copy.deepcopy(old)
    changed["paths"]["/v1/messages"]["post"]["parameters"] = [
        {"name": "project", "in": "query", "required": True}
    ]
    assert any("required" in item for item in module.changes(old, changed))


def test_changing_referenced_security_scheme_is_rejected():
    old = schema()
    old["components"]["securitySchemes"] = {"bearer": {"type": "http", "scheme": "bearer"}}
    new = copy.deepcopy(old)
    new["components"]["securitySchemes"]["bearer"] = {
        "type": "apiKey",
        "in": "query",
        "name": "key",
    }
    assert any("security scheme" in item for item in module.changes(old, new))
