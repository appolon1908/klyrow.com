import hashlib
import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args, cwd=None, env=None):
    return subprocess.run(args, cwd=cwd, env=env, check=True, text=True, capture_output=True)


def test_encrypted_backup_restore_round_trip(tmp_path, isolated_durable_result_keyring):
    if shutil.which("gpg") is None:
        raise AssertionError("gpg is required for backup certification")

    fixture = tmp_path / "fixture"
    for directory in (
        "scripts",
        "config",
        "deploy",
        "docker",
        "docs",
        "secrets",
        "bin",
        "backups",
        "offhost",
    ):
        (fixture / directory).mkdir(parents=True, exist_ok=True)
    for name in ("backup", "archive-offhost", "restore", "lib.sh"):
        shutil.copy2(ROOT / "scripts" / name, fixture / "scripts" / name)
    # Exercise the real backup guards as the test account; the production CLI's
    # root-only boundary is independently covered in test_durable_backup_contract.
    for name in ("durable_backup.py", "durable_keys.py"):
        target = fixture / "apps/gateway/app" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "apps/gateway/app" / name, target)
    shutil.copy2(
        ROOT / "scripts/durable-keyring-backup", fixture / "scripts/actual-durable-keyring-backup"
    )
    (fixture / "scripts/durable-keyring-backup").write_text(
        "import runpy\nfrom pathlib import Path\n"
        "module=runpy.run_path(str(Path(__file__).with_name('actual-durable-keyring-backup')))\n"
        "module['main'].__globals__['require_root']=lambda:None\n"
        "raise SystemExit(module['main']())\n"
    )
    (fixture / ".env").write_text(
        f"KLYROW_ENV=test\nKLYROW_DURABLE_RESULT_KEYRING_FILE={isolated_durable_result_keyring}\n"
    )
    (fixture / ".env").chmod(0o600)
    (fixture / "docker-compose.yml").write_text("services: {}\n")
    (fixture / "docker-compose.postal-provisioning.yml").write_text("services: {}\n")
    (fixture / "docker-compose.web.yml").write_text("services: {}\n")
    (fixture / "deploy" / "docker-compose.middleware-mtls.yml").write_text("services: {}\n")
    (fixture / "deploy" / "docker-compose.security-mail.yml").write_text("services: {}\n")
    (fixture / "config" / "fixture").write_text("configuration\n")
    (fixture / "docker" / "fixture").write_text("container\n")
    (fixture / "docs" / "fixture").write_text("documentation\n")
    (fixture / "secrets" / "fixture").write_text("encrypted-only fixture\n")

    docker_log = fixture / "docker.log"
    mock_docker = fixture / "bin" / "docker"
    mock_docker.write_text(
        """#!/usr/bin/env bash
set -Eeuo pipefail
printf '%s\\n' "$*" >>"$MOCK_DOCKER_LOG"
case "$*" in
  *"pg_dump"*) printf 'postgres-dump-fixture' ;;
  *"postal-db"*"mariadb-dump"*) printf 'postal-dump-fixture' ;;
  *"mariadb-dump"*) printf 'mautic-dump-fixture' ;;
  *"mautic tar"*) printf 'mautic-files-fixture' ;;
  *"mautic sh -lc"*"find"*) ;;
  *"export_definitions"*) printf '[]' ;;
  *"import_definitions"*) cat >/dev/null ;;
  *"list_queues"*) printf '[]' ;;
  *"pg_restore"*|*"mariadb -uroot"*) cat >/dev/null ;;
  *) exit 9 ;;
esac
"""
    )
    mock_docker.chmod(0o755)

    gpg_home = tmp_path / "keyring"
    gpg_home.mkdir(mode=0o700)
    run(
        "gpg",
        "--batch",
        "--homedir",
        str(gpg_home),
        "--passphrase",
        "",
        "--quick-generate-key",
        "Klyrow Backup Test <backup-test@invalid.example>",
        "rsa2048",
        "encrypt",
        "1d",
    )
    public_key = tmp_path / "recipient.asc"
    private_key = tmp_path / "private.asc"
    public_key.write_text(
        run("gpg", "--batch", "--homedir", str(gpg_home), "--armor", "--export").stdout
    )
    private_key.write_text(
        run("gpg", "--batch", "--homedir", str(gpg_home), "--armor", "--export-secret-keys").stdout
    )

    env = os.environ.copy()
    env.update(
        PATH=f"{fixture / 'bin'}:{env['PATH']}",
        MOCK_DOCKER_LOG=str(docker_log),
        KLYROW_BACKUP_RECIPIENT_FILE=str(public_key),
        KLYROW_BACKUP_STAGING_ROOT="/dev/shm",
        KLYROW_BACKUP_OFFHOST_DIR=str(fixture / "offhost"),
        KLYROW_BACKUP_ALLOW_TEST_DIRECTORY="true",
        KLYROW_ENV="test",
        KLYROW_RELEASE_SHA="0" * 40,
    )
    result = run(
        str(fixture / "scripts" / "backup"), str(fixture / "backups"), cwd=fixture, env=env
    )
    archive = Path(result.stdout.strip())
    assert archive.suffixes[-3:] == [".tar", ".gz", ".gpg"]
    assert archive.stat().st_mode & 0o777 == 0o600
    assert Path(f"{archive}.sha256").stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in (fixture / "backups").iterdir()) == [
        archive.name,
        f"{archive.name}.sha256",
    ]
    assert sorted(p.name for p in (fixture / "offhost").iterdir()) == [
        archive.name,
        f"{archive.name}.receipt.json",
        f"{archive.name}.sha256",
    ]
    ciphertext = archive.read_bytes()
    assert (fixture / "offhost" / archive.name).read_bytes() == ciphertext
    for marker in (
        b"postgres-dump-fixture",
        b"mautic-dump-fixture",
        b"postal-dump-fixture",
        b"encrypted-only fixture",
    ):
        assert marker not in ciphertext

    decrypted = subprocess.run(
        ["gpg", "--batch", "--homedir", str(gpg_home), "--decrypt", str(archive)],
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(decrypted), mode="r:gz") as bundle:
        archived_key = bundle.extractfile("durable-result-keyring.json").read()
        assert archived_key == isolated_durable_result_keyring.read_bytes()
        assert bundle.getmember("durable-result-keyring.json").mode == 0o600
        key_line = (
            hashlib.sha256(archived_key).hexdigest().encode() + b"  durable-result-keyring.json"
        )
        assert key_line in bundle.extractfile("MANIFEST.sha256").read().splitlines()
    assert archived_key not in ciphertext
    assert "DURABLE_KEYRING_BACKUP=PASS" not in result.stdout

    env.update(
        KLYROW_BACKUP_PRIVATE_KEY_FILE=str(private_key),
        CONFIRM_RESTORE="RESTORE_KLYROW",
    )
    restored = run(str(fixture / "scripts" / "restore"), str(archive), cwd=fixture, env=env)
    assert "Klyrow databases, Mautic files, and RabbitMQ definitions restored" in restored.stdout
    calls = docker_log.read_text()
    assert "pg_restore" in calls
    assert calls.count("mariadb -uroot") == 2
    assert "import_definitions" in calls
    assert "import_definitions -" not in calls
    assert calls.index("mautic sh -lc") < calls.rindex("mautic tar -xz")
    assert "import_definitions -" not in (ROOT / "scripts" / "restore-verify").read_text()
    # A lost or replaced key must stop the same shell restore before Docker.
    from apps.gateway.app.durable_keys import new_keyring_document

    isolated_durable_result_keyring.write_text(new_keyring_document())
    docker_log.write_text("")
    blocked = subprocess.run(
        [str(fixture / "scripts/restore"), str(archive)],
        cwd=fixture,
        env=env,
        text=True,
        capture_output=True,
    )
    assert blocked.returncode != 0
    assert "DURABLE_KEYRING_BACKUP=BLOCKED" in blocked.stderr
    assert docker_log.read_text() == ""


def test_backup_scripts_fail_closed_contract():
    backup = (ROOT / "scripts" / "backup").read_text()
    offhost = (ROOT / "scripts" / "archive-offhost").read_text()
    restore = (ROOT / "scripts" / "restore").read_text()
    assert "tar.gz.gpg" in backup
    assert "--encrypt" in backup
    assert "stat -f -c %T" in backup and "tmpfs" in backup
    assert "paths=(config deploy docker docs scripts" in backup
    assert "docker-compose.postal-provisioning.yml" in backup
    assert "docker-compose.web.yml" in backup
    assert "CONFIRM_RESTORE=RESTORE_KLYROW" in restore
    assert "--decrypt" in restore
    assert 'find "$root" -xdev -mindepth 1 -depth -delete' in restore
    assert "KLYROW_BACKUP_OFFHOST_DIR is required" in offhost
    assert "mountpoint -q" in offhost
    assert "OFFHOST_BACKUP=PASS" in offhost
    assert "-p$(" not in backup + offhost + restore
