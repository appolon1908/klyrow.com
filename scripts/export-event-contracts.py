#!/usr/bin/env python3
"""Generate Klyrow event schemas and AsyncAPI from runtime models."""
import argparse
import copy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.gateway.app.business_events import (  # noqa: E402
    EventEnvelope,
    KLYROW_EVENT_DATA_MODELS,
)
from apps.gateway.app.api_standard import components  # noqa: E402


EXAMPLES = {
    "klyrow.tenant.created": {"tenant_id":"tnt_example","name":"Example","organization_id":"org_example","enabled":True},
    "klyrow.tenant.updated": {"tenant_id":"tnt_example","name":"Example","organization_id":"org_example","enabled":False},
    "klyrow.subscription.changed": {"subscription_id":"sub_example","status":"ACTIVE","plan_id":"plan_email","price_id":"price_v1","version":2,"effective_at":"2026-09-13T00:00:00Z"},
    "klyrow.usage.daily": {"date":"2026-09-12","unit":"accepted_message","quantity":123,"snapshot_at":"2026-09-13T00:00:00Z"},
    "klyrow.kpi.daily": {"date":"2026-09-12","accepted":123,"delivered":120,"bounced":2,"complained":1,"snapshot_at":"2026-09-13T00:00:00Z"},
    "klyrow.campaign.summary": {"campaign_id":"cmp_example","campaign_version":1,"status":"COMPLETED","audience_count":100,"delivered_count":96,"suppressed_count":3,"failed_count":1},
    "klyrow.domain.status": {"domain_id":"dom_example","domain":"example.test","status":"VERIFIED","verified_at":"2026-09-13T00:00:00Z"},
    "klyrow.provider.health": {"provider":"postal","status":"HEALTHY","checked_at":"2026-09-13T00:00:00Z","reason_code":None},
    "klyrow.account.held": {"status":"HELD","reason":"PAYMENT_REVIEW","changed_at":"2026-09-13T00:00:00Z"},
    "klyrow.account.released": {"status":"RELEASED","reason":"REVIEW_COMPLETE","changed_at":"2026-09-13T00:00:00Z"},
}


def event_example(event_type):
    return {
        "id": "evt_" + event_type.replace(".", "_"),
        "type": event_type,
        "version": 1,
        "source": "klyrow",
        "tenant_id": "tnt_example",
        "correlation_id": "cor_example",
        "causation_id": "cause_example",
        "occurred_at": "2026-09-13T00:00:00Z",
        "data": EXAMPLES[event_type],
    }


def klyrow_envelope_schema():
    envelope = EventEnvelope.model_json_schema()
    envelope["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    envelope["$id"] = "https://schemas.codestra.co/events/klyrow-business-event.v1.schema.json"
    envelope["required"] = [
        "id",
        "type",
        "version",
        "source",
        "tenant_id",
        "correlation_id",
        "occurred_at",
        "data",
    ]
    envelope["properties"]["occurred_at"]["pattern"] = r"(?:Z|\+00:00)$"
    definitions = envelope.setdefault("$defs", {})
    conditions = []
    for event_type, model in KLYROW_EVENT_DATA_MODELS.items():
        schema = model.model_json_schema()
        if event_type == "klyrow.usage.daily":
            schema["required"] = ["date", "unit", "quantity", "snapshot_at"]
        nested = schema.pop("$defs", {})
        definitions.update(nested)
        definitions[model.__name__] = schema
        conditions.append({
            "if": {"properties": {"type": {"const": event_type}}, "required": ["type"]},
            "then": {"properties": {"data": {"$ref": f"#/$defs/{model.__name__}"}}},
        })
    envelope["allOf"] = conditions
    return envelope


def asyncapi_payload(schema):
    """Make embedded JSON Schema references resolve from the AsyncAPI root."""
    value = copy.deepcopy(schema)
    def visit(item):
        if isinstance(item, dict):
            if isinstance(item.get("$ref"), str) and item["$ref"].startswith("#/$defs/"):
                item["$ref"] = "#/components/messages/klyrow/payload" + item["$ref"][1:]
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
    visit(value)
    return value


def documents():
    envelope = klyrow_envelope_schema()
    payload = asyncapi_payload(envelope)
    payload.pop("$id", None)
    payload["properties"]["source"] = {"const": "klyrow"}
    klyrow_contract = {
        "asyncapi": "3.0.0",
        "info": {"title": "Klyrow to Middleware business events", "version": "1.0.0"},
        "defaultContentType": "application/json",
        "servers": {
            "middleware": {
                "host": "middleware.internal",
                "protocol": "https",
                "pathname": "/api/v1/events/klyrow",
                "description": "Environment-specific internal Middleware endpoint",
            }
        },
        "channels": {
            "klyrow": {
                "address": "codestra.klyrow.events",
                "messages": {"event": {"$ref": "#/components/messages/klyrow"}},
            }
        },
        "operations": {
            "publishKlyrowBusinessEvent": {
                "action": "send", "channel": {"$ref": "#/channels/klyrow"},
            }
        },
        "components": {
            "messages": {
                "klyrow": {"name": "klyrowBusinessEventV1", "payload": payload}
            }
        },
    }
    output = {
        "contracts/openapi/codestra-components.yaml": {
            "openapi": "3.1.0",
            "info": {"title": "Codestra API components", "version": "1.0.0"},
            "paths": {},
            "components": components(),
        },
        "schemas/json-schema/klyrow-business-event.v1.json": envelope,
        "schemas/asyncapi/klyrow-business-events-v1.yaml": klyrow_contract,
    }
    for event_type, model in KLYROW_EVENT_DATA_MODELS.items():
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = "https://schemas.codestra.co/events/" + event_type.replace(".", "-") + ".v1.data.schema.json"
        output["schemas/json-schema/" + event_type.replace(".", "-") + ".v1.data.json"] = schema
        output["schemas/examples/" + event_type.replace(".", "-") + ".v1.json"] = event_example(event_type)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    stale = []
    for name, document in documents().items():
        target = ROOT / name
        value = json.dumps(document, sort_keys=True, indent=2) + "\n"
        if args.check:
            if not target.exists() or target.read_text() != value:
                stale.append(name)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(value)
    if stale:
        raise SystemExit("Stale event contracts: " + ", ".join(stale))


if __name__ == "__main__":
    main()
