#!/usr/bin/env python3
"""Test the owned adapter slice with its immutable upstream package dependencies."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    owned = repo / "integrations/codestra-middleware"
    upstream = Path(sys.argv[1]).resolve()
    source = json.loads((owned / "SOURCE.json").read_text())
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=upstream, text=True).strip()
    if sha != source["source_sha"]:
        raise SystemExit("Middleware checkout must match SOURCE.json exactly")
    if subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=upstream
    ):
        raise SystemExit("Middleware checkout has modified tracked files")
    with tempfile.TemporaryDirectory(prefix="klyrow-contract-") as directory:
        overlay = Path(directory)
        shutil.copytree(
            upstream / "app", overlay / "app", ignore=shutil.ignore_patterns("__pycache__")
        )
        shutil.copytree(upstream / "contracts", overlay / "contracts")
        shutil.copytree(owned / "app", overlay / "app", dirs_exist_ok=True)
        shutil.copytree(owned / "deploy", overlay / "deploy")
        env = dict(
            os.environ,
            PYTHONPATH=os.pathsep.join(
                map(
                    str,
                    [
                        repo,
                        repo / "tests",
                        owned / "tests",
                        overlay,
                    ],
                )
            ),
        )
        return subprocess.call(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                str(owned / "tests"),
                "--import-mode=prepend",
                "--disable-warnings",
            ],
            cwd=overlay,
            env=env,
        )


if __name__ == "__main__":
    raise SystemExit(main())
