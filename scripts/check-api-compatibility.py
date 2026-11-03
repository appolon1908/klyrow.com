#!/usr/bin/env python3
"""Conservative compatibility gate for generated OpenAPI snapshots.

Adding operations and optional parameters is allowed. Changing existing schema
contracts requires a new version, even when a human considers it compatible.
This deliberate conservatism avoids silently approving unknown schema changes.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}


def contract(value, document):
    """Compare reachable refs as well as the operation, with cycle protection."""
    refs = {}

    def collect(item):
        if isinstance(item, dict):
            ref = item.get("$ref")
            if ref and ref not in refs:
                if not ref.startswith("#/"):
                    raise ValueError("external refs must be bundled before compatibility checking")
                target = document
                for part in ref[2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
                refs[ref] = target
                collect(target)
            for child in item.values():
                collect(child)
        elif isinstance(item, list):
            for child in item:
                collect(child)

    collect(value)
    return {"value": value, "refs": refs}


def changes(old: dict, new: dict) -> list[str]:
    failures = []
    for path, item in old.get("paths", {}).items():
        for method, operation in item.items():
            if method not in METHODS:
                continue
            label = f"{method.upper()} {path}"
            current_item = new.get("paths", {}).get(path, {})
            current = current_item.get(method)
            if current is None:
                failures.append(f"{label}: operation removed")
                continue
            for key in ("operationId", "requestBody"):
                if contract(operation.get(key), old) != contract(current.get(key), new):
                    failures.append(f"{label}: {key} contract changed")
            before_security = operation.get("security", old.get("security", []))
            after_security = current.get("security", new.get("security", []))
            if before_security != after_security:
                failures.append(f"{label}: authentication contract changed")
            for requirement in before_security:
                for name in requirement:
                    before = old.get("components", {}).get("securitySchemes", {}).get(name)
                    after = new.get("components", {}).get("securitySchemes", {}).get(name)
                    if before != after:
                        failures.append(f"{label}: security scheme {name} changed or removed")
            before_parameters = item.get("parameters", []) + operation.get("parameters", [])
            after_parameters = current_item.get("parameters", []) + current.get("parameters", [])

            def parameter_key(parameter):
                return parameter.get("$ref") or (
                    parameter["in"],
                    parameter["name"].lower() if parameter["in"] == "header" else parameter["name"],
                )

            prior = {parameter_key(p): p for p in before_parameters}
            following = {parameter_key(p): p for p in after_parameters}
            for key, parameter in prior.items():
                if contract(parameter, old) != contract(following.get(key), new):
                    failures.append(f"{label}: parameter {key} changed or removed")
            for key, parameter in following.items():
                if key not in prior and (parameter.get("required") or "$ref" in parameter):
                    failures.append(f"{label}: required or referenced parameter {key} added")
            for status, response in operation.get("responses", {}).items():
                if contract(response, old) != contract(
                    current.get("responses", {}).get(status), new
                ):
                    failures.append(f"{label}: response {status} changed or removed")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("previous", type=Path)
    parser.add_argument("current", type=Path)
    args = parser.parse_args()
    failures = changes(json.loads(args.previous.read_text()), json.loads(args.current.read_text()))
    for failure in failures:
        print(failure)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
