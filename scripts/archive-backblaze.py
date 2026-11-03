#!/usr/bin/env python3
"""Upload ciphertext to Backblaze B2 and verify exact-version downloads."""

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


class BackupError(Exception):
    pass


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def private_value(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
                raise BackupError("credential_file_permissions")
            value = stream.read(4097).strip()
        if not value or len(value) > 4096 or "\n" in value:
            raise BackupError("credential_file_invalid")
        return value
    except (OSError, UnicodeError) as error:
        raise BackupError("credential_file_unavailable") from error


def settings(environ):
    root = Path(environ.get("KLYROW_BACKUP_B2_CONFIG_DIR", "/etc/codestra/backup"))
    if not root.is_absolute():
        raise BackupError("config_path_invalid")
    values = {
        name: private_value(root / ("b2-" + name))
        for name in ("access-key-id", "secret-access-key", "endpoint", "region", "bucket")
    }
    region = values["region"]
    if not re.fullmatch(r"[a-z]+-[a-z]+-[0-9]{3}", region):
        raise BackupError("region_invalid")
    if values["endpoint"] != f"https://s3.{region}.backblazeb2.com":
        raise BackupError("endpoint_invalid")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{1,61}[A-Za-z0-9]", values["bucket"]):
        raise BackupError("bucket_invalid")
    prefix = environ.get("KLYROW_BACKUP_B2_PREFIX", "codestra/klyrow").strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", prefix):
        raise BackupError("prefix_invalid")
    source = environ.get("KLYROW_RELEASE_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", source):
        raise BackupError("source_sha_required")
    # Explicit credentials only; no ambient profiles, proxies or endpoint overrides.
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "AWS_ACCESS_KEY_ID": values["access-key-id"],
        "AWS_SECRET_ACCESS_KEY": values["secret-access-key"],
        "AWS_DEFAULT_REGION": region,
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "AWS_PAGER": "",
        "AWS_MAX_ATTEMPTS": "3",
        "AWS_RETRY_MODE": "standard",
    }
    return values["endpoint"], values["bucket"], prefix, source, env


def aws(endpoint, env, *args):
    try:
        result = subprocess.run(
            [
                "aws",
                "--endpoint-url",
                endpoint,
                "--cli-connect-timeout",
                "15",
                "--cli-read-timeout",
                "120",
                "s3api",
                *args,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        if result.returncode:
            # Never echo CLI errors: they can contain request details or credentials.
            code = "b2_request_failed"
            for known in ("InvalidAccessKeyId", "AccessDenied", "SignatureDoesNotMatch"):
                if known in result.stderr:
                    code = "b2_" + known
                    break
            raise BackupError(code)
        body = json.loads(result.stdout)
        if not isinstance(body, dict):
            raise BackupError("b2_response_invalid")
        return body
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        raise BackupError("b2_transport_failed") from error


def put_verified(endpoint, env, bucket, key, file, temp, call=aws):
    expected = digest(file)
    response = call(
        endpoint,
        env,
        "put-object",
        "--bucket",
        bucket,
        "--key",
        key,
        "--body",
        str(file),
        "--server-side-encryption",
        "AES256",
        "--metadata",
        json.dumps({"sha256": expected}),
    )
    version = response.get("VersionId")
    if not isinstance(version, str) or not version or version == "null":
        raise BackupError("b2_version_missing")
    downloaded = temp / "readback"
    result = call(
        endpoint,
        env,
        "get-object",
        "--bucket",
        bucket,
        "--key",
        key,
        "--version-id",
        version,
        str(downloaded),
    )
    if (
        result.get("VersionId") != version
        or result.get("ServerSideEncryption") != "AES256"
        or not downloaded.is_file()
        or digest(downloaded) != expected
    ):
        raise BackupError("b2_readback_mismatch")
    downloaded.unlink()
    return {"key": key, "version_id": version, "sha256": expected}


def archive(file, environ, call=aws):
    file = Path(file)
    checksum = Path(str(file) + ".sha256")
    if (
        file.is_symlink()
        or checksum.is_symlink()
        or not file.is_file()
        or not checksum.is_file()
        or not re.fullmatch(r"[A-Za-z0-9_.-]+\.tar\.gz\.gpg", file.name)
    ):
        raise BackupError("encrypted_archive_required")
    if not 1 < file.stat().st_size <= 5_000_000_000:
        raise BackupError("archive_size_unsupported")
    with file.open("rb") as stream:
        header = stream.read(1)[0]
    # Public-key encrypted session-key packet; reject plaintext and legacy symmetric archives.
    if not header & 0x80 or ((header & 0x3F) if header & 0x40 else ((header >> 2) & 0x0F)) != 1:
        raise BackupError("public_key_ciphertext_required")
    expected = digest(file)
    if checksum.read_text() != f"{expected}  {file.name}\n":
        raise BackupError("checksum_mismatch")
    endpoint, bucket, prefix, source, env = settings(environ)
    # Content-addressed prefix prevents a later attempt replacing a different archive.
    key = f"{prefix}/{source}/{expected}/{file.name}"
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="klyrow-b2-") as work:
        temp = Path(work)
        stored = put_verified(endpoint, env, bucket, key, file, temp, call)
        if digest(file) != expected or stored["sha256"] != expected:
            raise BackupError("archive_changed")
        sidecar = put_verified(endpoint, env, bucket, key + ".sha256", checksum, temp, call)
        if sidecar["sha256"] != hashlib.sha256(f"{expected}  {file.name}\n".encode()).hexdigest():
            raise BackupError("checksum_changed")
        receipt = {
            "storage": "backblaze_b2",
            "bucket": bucket,
            "source_sha": source,
            "archive": stored,
            "checksum": sidecar,
            "readback": "PASS",
            "client_encryption": "OpenPGP",
            "server_encryption": "AES256",
        }
        receipt_file = temp / "receipt.json"
        receipt_file.write_text(json.dumps(receipt, sort_keys=True) + "\n")
        put_verified(endpoint, env, bucket, key + ".receipt.json", receipt_file, temp, call)
        local_receipt = Path(str(file) + ".receipt.json")
        if local_receipt.is_symlink():
            raise BackupError("receipt_path_invalid")
        # Local receipt is optional evidence for operators; never publish PASS before B2 readback.
        fd = os.open(local_receipt, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(receipt_file.read_text())
            output.flush()
            os.fsync(output.fileno())
    return receipt


def main():
    try:
        if len(sys.argv) != 2:
            raise BackupError("usage_archive_required")
        result = archive(sys.argv[1], os.environ)
        print("OFFHOST_BACKUP=PASS storage=backblaze_b2 sha256=" + result["archive"]["sha256"])
        return 0
    except (BackupError, OSError, UnicodeError) as error:
        code = str(error) if isinstance(error, BackupError) else "local_io_failed"
        print("OFFHOST_BACKUP=FAIL reason=" + code, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
