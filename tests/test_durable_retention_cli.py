"""No live database or production backup is needed to exercise CLI guards."""

from __future__ import annotations

import hashlib
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
loader = SourceFileLoader("klyrow_retention_cli", str(ROOT / "scripts/retain-durable-results"))
spec = spec_from_loader(loader.name, loader)
cli = module_from_spec(spec)
loader.exec_module(cli)


def backup(tmp_path):
    path = tmp_path / "synthetic-checkpoint"
    path.write_bytes(b"fixture bytes do not certify encryption or restore")
    path.chmod(0o600)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "relative",
        "symlink",
        "parent-symlink",
        "directory",
        "empty",
        "public",
        "hardlink",
        "oversized",
        "wrong-digest",
    ],
)
def test_backup_guard_fails_closed(tmp_path, monkeypatch, kind):
    path, digest = backup(tmp_path)
    if kind == "missing":
        path.unlink()
    if kind == "relative":
        path = Path("relative")
    if kind == "symlink":
        link = tmp_path / "link"
        link.symlink_to(path)
        path = link
    if kind == "parent-symlink":
        link = tmp_path / "link-dir"
        link.symlink_to(tmp_path)
        path = link / path.name
    if kind == "directory":
        path.unlink()
        path.mkdir()
    if kind == "empty":
        path.write_bytes(b"")
    if kind == "public":
        path.chmod(0o644)
    if kind == "hardlink":
        (tmp_path / "hard").hardlink_to(path)
    if kind == "oversized":
        monkeypatch.setattr(cli, "MAX_BACKUP_BYTES", 2)
    if kind == "wrong-digest":
        digest = "a" * 64
    with pytest.raises((ValueError, OSError)):
        cli.verify_backup(path, digest)


def test_backup_guard_accepts_private_exact_bytes(tmp_path):
    cli.verify_backup(*backup(tmp_path))


def test_cli_is_dry_run_by_default(monkeypatch, capsys):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    calls = []
    monkeypatch.setattr(cli, "execute", lambda args: calls.append(args) or {"updated": 0})
    assert cli.main(["purge", "--tenant", "tenant-a", "--before", "2020-01-01T00:00:00+00:00"]) == 0
    assert len(calls) == 1 and calls[0].apply is False
    assert capsys.readouterr().out == '{"updated": 0}\n'


@pytest.mark.parametrize("kind", ["nonroot", "no-confirmation", "no-plan", "no-backup"])
def test_cli_apply_is_blocked_before_any_database_import_or_execution(monkeypatch, capsys, kind):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000 if kind == "nonroot" else 0)

    def no_execution(args):
        raise AssertionError("database must not be reached")

    monkeypatch.setattr(cli, "execute", no_execution)
    args = ["purge", "--tenant", "tenant-a", "--before", "2020-01-01T00:00:00Z", "--apply"]
    if kind != "no-confirmation":
        args += ["--confirm", cli.CONFIRM]
    if kind != "no-plan":
        args += ["--expected-plan-sha256", "a" * 64]
    assert cli.main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "DURABLE_RESULT_RETENTION=BLOCKED\n"


@pytest.mark.parametrize("checkpoint_owner", [0, 1001])
def test_apply_verifies_checkpoint_then_executes_once(tmp_path, monkeypatch, checkpoint_owner):
    path, digest = backup(tmp_path)
    calls = []
    real_fstat = cli.os.fstat

    def synthetic_ownership(fd):
        # Exercise the real bytes/mode/metadata checks without requiring hosted
        # tests to chown a fixture or weakening the production root boundary.
        actual = real_fstat(fd)
        fields = (
            "st_dev",
            "st_ino",
            "st_size",
            "st_mode",
            "st_gid",
            "st_nlink",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        return SimpleNamespace(
            st_uid=checkpoint_owner, **{key: getattr(actual, key) for key in fields}
        )

    monkeypatch.setattr(cli.os, "fstat", synthetic_ownership)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "execute", lambda args: calls.append(args) or {"updated": 1})
    result = cli.main(
        [
            "purge",
            "--tenant",
            "tenant-a",
            "--before",
            "2020-01-01T00:00:00Z",
            "--apply",
            "--confirm",
            cli.CONFIRM,
            "--expected-plan-sha256",
            "a" * 64,
            "--backup-file",
            str(path),
            "--backup-sha256",
            digest,
        ]
    )
    if checkpoint_owner == 0:
        assert result == 0 and len(calls) == 1 and calls[0].apply is True
    else:
        assert result == 1 and calls == []


def test_cli_never_prints_connection_or_payload_failures(monkeypatch, capsys):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)

    def fail(args):
        raise RuntimeError("credential-bearing-connection synthetic-private-payload")

    monkeypatch.setattr(cli, "execute", fail)
    assert (
        cli.main(
            [
                "hold",
                "--tenant",
                "tenant-a",
                "--outbox-id",
                "operation-a",
                "--hold-id",
                "hold-a",
                "--change-sha256",
                "a" * 64,
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.err == "DURABLE_RESULT_RETENTION=BLOCKED\n" and captured.out == ""


@pytest.mark.parametrize("apply", [False, True])
def test_cli_refuses_database_engines_without_postgres_row_locks(monkeypatch, apply):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import apps.gateway.app.main as main

    monkeypatch.setattr(main, "DB", sessionmaker(create_engine("sqlite://")))
    with pytest.raises(ValueError, match="postgresql_required"):
        cli.execute(SimpleNamespace(operation="purge", tenant="tenant-a", apply=apply))


def test_cli_transaction_rolls_back_purge_failure_and_emits_no_result(monkeypatch):
    import apps.gateway.app.main as main
    import apps.gateway.app.durable_retention as retention

    events = []

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def execute(self, stmt):
            events.append("setup")

        def rollback(self):
            events.append("rollback")

        def commit(self):
            events.append("commit")

    def fails(*args, **kwargs):
        raise ValueError("conflict")

    monkeypatch.setattr(main, "DB", FakeSession)
    monkeypatch.setattr(retention, "purge_result_batch", fails)
    with pytest.raises(ValueError):
        cli.execute(
            SimpleNamespace(
                operation="purge",
                tenant="tenant-a",
                before="2020-01-01T00:00:00Z",
                after_id="",
                limit=1,
                apply=True,
                expected_plan_sha256="a" * 64,
            )
        )
    assert events[-1] == "rollback" and "commit" not in events
