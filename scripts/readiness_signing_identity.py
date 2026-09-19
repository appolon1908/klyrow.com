#!/usr/bin/env python3
"""Exact repository/ref identities for the local readiness signer."""
import os
import re


def signing_identity_pattern(repository: str, ref: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("invalid repository")
    if ref not in {"refs/heads/main", "refs/heads/staging", "refs/heads/production"}:
        raise ValueError("release ref is not protected")
    identities = [
        f"https://github.com/{repository}/.github/workflows/{name}@{ref}"
        for name in ("codestra-deploy-readiness.yml", "reusable-codestra-deploy-readiness.yml")
    ]
    return "^(" + "|".join(re.escape(identity) for identity in identities) + ")$"


if __name__ == "__main__":
    print(signing_identity_pattern(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_REF"]))
