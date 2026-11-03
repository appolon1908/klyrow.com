#!/usr/bin/env python3
"""Validate rendered Compose against protected and rollback image evidence."""

import json
import re
import sys

compose = json.load(sys.stdin)
with open(sys.argv[1], encoding="utf-8") as stream:
    published = json.load(stream)
with open(sys.argv[2], encoding="utf-8") as stream:
    rollback = json.load(stream)
source_sha = sys.argv[3]
digest = re.compile(r"^sha256:[0-9a-f]{64}$")
reference = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
service_keys = {
    "gateway": "gateway",
    "web": "web",
    "gateway-migrate": "migrate",
    "postal-provisioner": "postal_provisioner",
}
for service, key in service_keys.items():
    value = published.get(key, "")
    if not digest.fullmatch(value):
        raise SystemExit("invalid protected image digest evidence")
    image = compose["services"][service]["image"]
    if not image.endswith("@" + value):
        raise SystemExit(f"{service}: Compose digest differs from protected evidence")
if rollback.get("source_sha") == source_sha:
    raise SystemExit("rollback authority must reference a prior source SHA")
if not re.fullmatch(r"[0-9a-f]{40}", str(rollback.get("source_sha", ""))):
    raise SystemExit("rollback source SHA is invalid")
images = rollback.get("images")
if not isinstance(images, dict) or set(images) != set(service_keys.values()):
    raise SystemExit("rollback image set is incomplete")
if not all(reference.fullmatch(str(value)) for value in images.values()):
    raise SystemExit("rollback images must use exact digest references")
