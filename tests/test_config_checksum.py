"""Exercise the checksum shell entrypoint with an isolated Compose executable."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("explicit", [False, True])
def test_checksum_uses_exact_composition_for_validation_and_hashing(tmp_path, explicit):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("config-checksum", "lib.sh"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    (tmp_path / ".env").touch()
    validator = scripts / "validate-production-images"
    validator.write_text('#!/bin/sh\nexec "$@" validate-images\n')
    validator.chmod(0o755)
    docker = tmp_path / "docker"
    docker.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
with Path(os.environ["COMMAND_LOG"]).open("a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[-3:] == ["config", "--format", "json"]:
    print(json.dumps({"composition": sys.argv[1:-3], "secret": "DO_NOT_PRINT"}))
""")
    docker.chmod(0o755)
    command = ["bash", str(scripts / "config-checksum")]
    compose = [
        "docker",
        "compose",
        "-f",
        "docker-compose.yml",
        "-f",
        "deploy/docker-compose.middleware-mtls.yml",
        "-f",
        "deploy/docker-compose.security-mail.yml",
        "-f",
        "docker-compose.postal-provisioning.yml",
        "-f",
        "docker-compose.web.yml",
    ]
    if explicit:
        compose = [
            "docker",
            "compose",
            "--env-file",
            "/approved/env with spaces",
            "-f",
            "docker-compose.yml",
            "-f",
            "compose.email-activation.yaml",
        ]
        command += compose
    log = tmp_path / "commands.jsonl"
    result = subprocess.run(
        command,
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "COMMAND_LOG": str(log),
        },
        capture_output=True,
        text=True,
        check=True,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    prefix = compose[1:]
    assert calls == [
        prefix + ["config", "--quiet"],
        prefix + ["validate-images"],
        prefix + ["config", "--format", "json"],
    ]
    rendered = json.dumps({"composition": prefix, "secret": "DO_NOT_PRINT"}) + "\n"
    assert result.stdout == hashlib.sha256(rendered.encode()).hexdigest() + "\n"
    assert "DO_NOT_PRINT" not in result.stdout + result.stderr
