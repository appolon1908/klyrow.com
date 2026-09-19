#!/usr/bin/env python3
"""Keep published event schemas immutable and permit new streams/operations."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("api_compatibility", ROOT/"scripts/check-api-compatibility.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


def changes(old, new):
    if "asyncapi" not in old:
        return [] if old == new else ["published schema changed; add a new version"]
    failures = []
    for section in ("channels", "operations"):
        for name, value in old.get(section, {}).items():
            try:
                unchanged = api.contract(value, old) == api.contract(new.get(section, {}).get(name), new)
            except (KeyError, ValueError):
                unchanged = False
            if not unchanged:
                failures.append(section+":"+name+": changed or removed")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.base_ref):
        parser.error("base-ref must be a full commit SHA")
    subprocess.run(["git", "cat-file", "-e", args.base_ref], cwd=ROOT, check=True, capture_output=True)
    paths = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", args.base_ref, "--",
        "schemas/asyncapi", "schemas/json-schema", "contracts/openapi"], cwd=ROOT, text=True).splitlines()
    failures = []
    for name in paths:
        if not name.endswith((".json", ".yaml")):
            continue
        path = ROOT/name
        if not path.is_file():
            failures.append(name+": removed")
            continue
        before = json.loads(subprocess.check_output(["git", "show", f"{args.base_ref}:{name}"], cwd=ROOT, text=True))
        failures.extend(name+": "+item for item in changes(before, json.loads(path.read_text())))
    for item in failures:
        print(item)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
