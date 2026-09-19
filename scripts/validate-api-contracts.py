#!/usr/bin/env python3
"""Validate bundled exports and optionally compare a trusted Git base commit."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import subprocess

from openapi_spec_validator import validate

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("compatibility", ROOT / "scripts/check-api-compatibility.py")
compatibility = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compatibility)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref")
    args = parser.parse_args()
    if args.base_ref and not re.fullmatch(r"[0-9a-f]{40}", args.base_ref):
        parser.error("base-ref must be a full Git commit SHA")
    failures = []
    for shared in sorted((ROOT / "contracts/openapi").glob("*.yaml")):
        validate(json.loads(shared.read_text()))
    for path in sorted((ROOT / "schemas/openapi").glob("*.yaml")):
        document = json.loads(path.read_text())
        validate(document)
        operations = [op for item in document["paths"].values() for method, op in item.items() if method in compatibility.METHODS]
        identifiers = [operation["operationId"] for operation in operations]
        if len(identifiers) != len(set(identifiers)):
            failures.append(f"{path.name}: duplicate operationId")
        if any("security" not in operation for operation in operations):
            failures.append(f"{path.name}: operation missing explicit security declaration")
        if args.base_ref:
            relative = path.relative_to(ROOT).as_posix()
            exists = subprocess.run(["git", "cat-file", "-e", args.base_ref], cwd=ROOT, capture_output=True)
            if exists.returncode:
                raise SystemExit("base commit is unavailable; fetch it before checking compatibility")
            previous = subprocess.run(["git", "show", f"{args.base_ref}:{relative}"], cwd=ROOT, capture_output=True, text=True)
            if previous.returncode == 0:
                failures.extend(f"{path.name}: {item}" for item in compatibility.changes(json.loads(previous.stdout), document))
            else:
                print(f"{relative}: initial contract baseline (absent at base commit)")
        print(f"{path.name}: {len(operations)} validated operations")
    for failure in failures:
        print(failure)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
