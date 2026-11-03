"""Isolated file/GPG rehearsals only. No Mautic, Docker or provider is invoked."""

from __future__ import annotations

import copy
import hashlib
import importlib.machinery
import importlib.util
import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/migrate-mautic-volumes"
loader = importlib.machinery.SourceFileLoader("mautic_volume_migration", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)


@pytest.fixture
def sample(tmp_path):
    source = tmp_path / "legacy"
    source.mkdir(mode=0o700)
    for path in m.ROOTS:
        directory = source / path
        directory.mkdir(parents=True, mode=0o750)
        (directory / "sample.txt").write_bytes(b"synthetic-data\n")
        (directory / "sample.txt").chmod(0o640)
    # Executable code and cache must not be copied out of a legacy whole-app volume.
    (source / "vendor").mkdir()
    (source / "vendor" / "old.php").write_text("do not migrate application code")
    (source / "var/cache").mkdir()
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    target = tmp_path / "targets"
    target.mkdir(mode=0o700)
    return SimpleNamespace(
        source=source,
        destination=target / "split",
        backup_file=backups / "snapshot.gpg",
        source_sha="a" * 40,
        source_image="ghcr.io/example/mautic@sha256:" + "b" * 64,
        target_image="ghcr.io/example/mautic@sha256:" + "c" * 64,
        apply=False,
        confirm=None,
        recipient_file=None,
        recipient_fingerprint=None,
    )


def document(sample):
    return {
        "schema": m.SCHEMA,
        "source_sha": sample.source_sha,
        "source_image": sample.source_image,
        "target_image": sample.target_image,
        "entries": m.inventory(sample.source),
    }


@pytest.fixture
def offline(monkeypatch):
    # Unit tests use synthetic regular directories. The opt-in root rehearsal below
    # exercises the real Linux read-only-mount check without this substitution.
    monkeypatch.setattr(m, "readonly_source", lambda path: m.checked_path(path))


@pytest.fixture(scope="module")
def recipient(tmp_path_factory):
    if not Path("/usr/bin/gpg").exists():
        pytest.fail("GPG is required for Mautic encrypted-backup regression tests")
    root = tmp_path_factory.mktemp("mautic-synthetic-gpg")
    home = root / "private"
    home.mkdir(mode=0o700)
    args = m.gpg_args(home)
    subprocess.run(
        args
        + [
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
            "--quick-generate-key",
            "Synthetic Mautic Rehearsal <mautic@example.invalid>",
            "rsa2048",
            "encr",
            "1d",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    listing = subprocess.check_output(
        args + ["--with-colons", "--list-keys"], stderr=subprocess.DEVNULL
    ).decode()
    fingerprint = next(
        line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")
    )
    public = root / "recipient.asc"
    public.write_bytes(subprocess.check_output(args + ["--armor", "--export", fingerprint]))
    yield SimpleNamespace(home=home, public=public, fingerprint=fingerprint)
    subprocess.run(
        ["gpgconf", "--homedir", str(home), "--kill", "all"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def apply_args(sample, recipient):
    sample.apply, sample.confirm = True, m.CONFIRM
    sample.recipient_file, sample.recipient_fingerprint = recipient.public, recipient.fingerprint
    return sample


def restore_args(sample, recipient, destination):
    return SimpleNamespace(
        backup_file=sample.backup_file,
        backup_sha256=m.digest_file(sample.backup_file),
        gpg_home=recipient.home,
        destination=destination,
        apply=False,
        confirm=None,
    )


def test_plain_readwrite_source_is_refused(sample):
    with pytest.raises(m.Blocked, match="readonly_source_required"):
        m.migrate(sample)
    assert not sample.destination.exists() and not sample.backup_file.exists()


def test_plan_is_readonly_and_excludes_old_code(sample, offline):
    before = set(sample.destination.parent.iterdir()), set(sample.backup_file.parent.iterdir())
    result = m.migrate(sample)
    assert result["state"] == "DRY_RUN" and not result["cutover_performed"]
    assert result["entries"] == 8
    assert before == (
        set(sample.destination.parent.iterdir()),
        set(sample.backup_file.parent.iterdir()),
    )
    assert b"vendor" not in m.canonical(document(sample))


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "fifo", "setuid", "xattr"])
def test_unsafe_persistent_data_is_rejected(sample, unsafe):
    target = sample.source / "config/sample.txt"
    if unsafe == "symlink":
        target.unlink()
        target.symlink_to("/etc/passwd")
    elif unsafe == "hardlink":
        os.link(target, target.with_name("second"))
    elif unsafe == "fifo":
        target.unlink()
        os.mkfifo(target)
    elif unsafe == "setuid":
        target.chmod(0o4640)
    else:
        os.setxattr(target, "user.test", b"must-not-be-lost")
    with pytest.raises(m.Blocked):
        m.inventory(sample.source)


def test_missing_persistent_directory_is_rejected(sample):
    shutil.rmtree(sample.source / "config")
    with pytest.raises(m.Blocked, match="persistent_directory_missing"):
        m.inventory(sample.source)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_sha", "main"),
        ("source_image", "mautic:latest"),
        ("target_image", "mautic:latest"),
        ("schema", "wrong"),
    ],
)
def test_declared_release_tuple_requires_immutable_syntax(sample, field, value):
    manifest = document(sample)
    manifest[field] = value
    with pytest.raises(m.Blocked):
        m.validate_manifest(manifest)


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/config/escape",
        "config/../escape",
        "vendor/old.php",
        "config//double",
        "config/./dot",
    ],
)
def test_archive_paths_cannot_escape_or_add_code(sample, name):
    manifest = document(sample)
    manifest["entries"][1]["path"] = name
    with pytest.raises(m.Blocked):
        m.validate_manifest(manifest)


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "symlink"),
        ("mode", 0o4777),
        ("uid", -1),
        ("gid", True),
        ("size", -1),
        ("mtime_ns", -1),
        ("sha256", "not-a-digest"),
    ],
)
def test_archive_metadata_is_bounded_and_typed(sample, field, value):
    manifest = document(sample)
    manifest["entries"][1][field] = value
    with pytest.raises(m.Blocked):
        m.validate_manifest(manifest)


def test_duplicate_paths_and_json_keys_are_rejected(sample):
    manifest = document(sample)
    manifest["entries"].append(copy.deepcopy(manifest["entries"][1]))
    with pytest.raises(m.Blocked):
        m.validate_manifest(manifest)
    with pytest.raises(m.Blocked, match="duplicate_json_key"):
        m.load_json(b'{"schema":1,"schema":2}')


def test_limits_fail_before_any_copy(sample, monkeypatch, offline):
    monkeypatch.setattr(m, "MAX_BYTES", 1)
    with pytest.raises(m.Blocked, match="byte_limit"):
        m.migrate(sample)
    assert not sample.destination.exists()


def test_low_space_fails_before_any_copy(sample, monkeypatch, offline):
    monkeypatch.setattr(m.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    with pytest.raises(m.Blocked, match="insufficient_space"):
        m.migrate(sample)
    assert not sample.destination.exists() and not sample.backup_file.exists()


def test_apply_requires_offline_confirmation(sample, offline):
    sample.apply = True
    with pytest.raises(m.Blocked, match="offline_confirmation_required"):
        m.migrate(sample)
    assert list(sample.destination.parent.iterdir()) == []


def test_unsafe_parent_and_symlink_paths_rejected(sample, offline, tmp_path):
    sample.destination.parent.chmod(0o777)
    with pytest.raises(m.Blocked, match="private_path_required"):
        m.migrate(sample)
    sample.destination.parent.chmod(0o700)
    link = tmp_path / "alias"
    link.symlink_to(sample.destination.parent, target_is_directory=True)
    sample.destination = link / "split"
    with pytest.raises(m.Blocked, match="symlink_path"):
        m.migrate(sample)


def test_encrypted_roundtrip_idempotence_and_restore(sample, offline, recipient):
    result = m.migrate(apply_args(sample, recipient))
    assert result["state"] == "VERIFIED"
    original = document(sample)
    m.check_tree(sample.destination, original)
    assert not (sample.destination / "vendor").exists()
    assert sample.backup_file.stat().st_mode & 0o777 == 0o600
    assert (sample.destination / "migration.json").stat().st_mode & 0o777 == 0o600
    assert b"synthetic-data" not in sample.backup_file.read_bytes()
    previous = sample.backup_file.read_bytes(), (sample.destination / "migration.json").read_bytes()
    assert m.migrate(sample)["state"] == "ALREADY_VERIFIED"
    assert previous == (
        sample.backup_file.read_bytes(),
        (sample.destination / "migration.json").read_bytes(),
    )
    restored = sample.destination.parent / "restored"
    args = restore_args(sample, recipient, restored)
    assert m.restore(args)["state"] == "RESTORE_DRY_RUN"
    assert not restored.exists()
    args.apply, args.confirm = True, m.CONFIRM
    assert m.restore(args)["state"] == "RESTORED"
    m.check_tree(restored, original)
    assert m.inventory(sample.source) == original["entries"]
    assert not list(sample.destination.parent.glob(".mautic-data-*"))
    assert not list(sample.backup_file.parent.glob(".mautic-data-*"))


def test_destination_conflict_cannot_overwrite_data(sample, offline, recipient):
    m.migrate(apply_args(sample, recipient))
    changed = sample.destination / "config/sample.txt"
    changed.write_bytes(b"new-runtime-data")
    with pytest.raises(m.Blocked, match="destination_data_mismatch"):
        m.migrate(sample)
    assert changed.read_bytes() == b"new-runtime-data"


def test_wrong_recipient_and_encryption_failure_leave_no_destination(sample, offline, recipient):
    apply_args(sample, recipient).recipient_fingerprint = "D" * 40
    with pytest.raises(m.Blocked, match="recipient_mismatch"):
        m.migrate(sample)
    assert not sample.destination.exists() and not sample.backup_file.exists()
    assert not list(sample.destination.parent.iterdir())


def test_source_change_during_copy_is_refused(sample, offline, recipient, monkeypatch):
    original = m.restore_metadata

    def change_source(stage, entries):
        original(stage, entries)
        (sample.source / "config/sample.txt").write_bytes(b"concurrent-writer")

    monkeypatch.setattr(m, "restore_metadata", change_source)
    with pytest.raises(m.Blocked, match="source_changed"):
        m.migrate(apply_args(sample, recipient))
    assert not sample.destination.exists() and not sample.backup_file.exists()


def test_backup_cannot_be_overwritten(sample, offline, recipient):
    sample.backup_file.write_bytes(b"previous-checkpoint")
    with pytest.raises(m.Blocked, match="backup_exists"):
        m.migrate(apply_args(sample, recipient))
    assert sample.backup_file.read_bytes() == b"previous-checkpoint"


def test_atomic_publish_never_replaces_even_empty_destination(tmp_path):
    source, target = tmp_path / "stage", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "data").write_text("source")
    with pytest.raises(m.Blocked, match="destination_publish_conflict"):
        m.publish_directory(source, target)
    assert not list(target.iterdir()) and (source / "data").exists()


def checkpoint_bytes(sample, *, mutation=None):
    manifest = document(sample)
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
        raw = m.canonical(manifest)
        header = tarfile.TarInfo("manifest.json")
        header.size = len(raw)
        archive.addfile(header, io.BytesIO(raw))
        inverse = {value: key for key, value in m.ROOTS.items()}
        for entry in manifest["entries"]:
            parts = Path(entry["path"]).parts
            path = sample.source / inverse[parts[0]] / Path(*parts[1:])
            info = archive.gettarinfo(str(path), arcname=entry["path"])
            if mutation:
                mutation(info)
            if info.isreg():
                with path.open("rb") as content:
                    archive.addfile(info, content)
            else:
                archive.addfile(info)
    stream.seek(0)
    return stream


@pytest.mark.parametrize("attack", ["path", "symlink", "device", "uid", "mode"])
def test_restore_rejects_modified_archive_members(sample, tmp_path, attack):
    def mutate(info):
        if info.name == "config/sample.txt":
            if attack == "path":
                info.name = "../escaped"
            elif attack == "symlink":
                info.type, info.linkname, info.size = tarfile.SYMTYPE, "/etc/passwd", 0
            elif attack == "device":
                info.type, info.size = tarfile.CHRTYPE, 0
            elif attack == "uid":
                info.uid += 1
            else:
                info.mode |= 0o4000

    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    with pytest.raises(m.Blocked):
        m.unpack_checkpoint(checkpoint_bytes(sample, mutation=mutate), stage, space_parent=tmp_path)
    assert not (tmp_path / "escaped").exists()


def test_checksum_mismatch_is_rejected_before_decryption(sample, offline, recipient):
    m.migrate(apply_args(sample, recipient))
    args = restore_args(sample, recipient, sample.destination.parent / "restore")
    args.backup_sha256 = "f" * 64
    with pytest.raises(m.Blocked, match="checkpoint_mismatch"):
        m.restore(args)
    assert not args.destination.exists()


def test_root_requirement_and_sanitized_errors(sample, monkeypatch, capsys):
    monkeypatch.setattr(m.os, "geteuid", lambda: 1234)
    result = m.main(
        [
            "migrate",
            "--source",
            str(sample.source),
            "--destination",
            str(sample.destination),
            "--backup-file",
            str(sample.backup_file),
            "--source-sha",
            sample.source_sha,
            "--source-image",
            sample.source_image,
            "--target-image",
            sample.target_image,
        ]
    )
    assert result == 1
    output = capsys.readouterr()
    assert output.out == "" and output.err == "MAUTIC_DATA_MIGRATION=BLOCKED\n"


@pytest.mark.skipif(
    os.getenv("KLYROW_MAUTIC_MOUNT_REHEARSAL") != "1",
    reason="explicit isolated root mount rehearsal only",
)
def test_real_readonly_mount_and_root_cli(sample, recipient, tmp_path):
    assert os.geteuid() == 0
    mountpoint = tmp_path / "readonly"
    mountpoint.mkdir()
    subprocess.run(["mount", "--bind", str(sample.source), str(mountpoint)], check=True)
    try:
        subprocess.run(["mount", "-o", "remount,bind,ro", str(mountpoint)], check=True)
        args = [
            "migrate",
            "--source",
            str(mountpoint),
            "--destination",
            str(sample.destination),
            "--backup-file",
            str(sample.backup_file),
            "--source-sha",
            sample.source_sha,
            "--source-image",
            sample.source_image,
            "--target-image",
            sample.target_image,
            "--recipient-file",
            str(recipient.public),
            "--recipient-fingerprint",
            recipient.fingerprint,
        ]
        assert m.main(args) == 0 and not sample.destination.exists()
        assert m.main(args + ["--apply", "--confirm", m.CONFIRM]) == 0
        assert m.main(args + ["--apply", "--confirm", m.CONFIRM]) == 0
        restore = restore_args(sample, recipient, sample.destination.parent / "restored")
        restore.apply, restore.confirm = True, m.CONFIRM
        assert m.restore(restore)["state"] == "RESTORED"
        m.check_tree(restore.destination, document(sample))
    finally:
        subprocess.run(["umount", str(mountpoint)], check=True)


@pytest.mark.parametrize("size", [0, 1, 1024**2 + 13])
def test_growing_stream_cannot_expand_copy_or_hash_budget(size):
    class Endless:
        consumed = 0

        def read(self, count):
            assert 0 < count <= 1024**2
            self.consumed += count
            return b"x" * count

    for output in (None, io.BytesIO()):
        stream = Endless()
        with pytest.raises(m.Blocked, match="source_changed"):
            m.bounded_content(stream, size, output)
        assert stream.consumed == size + 1
        if output is not None:
            assert len(output.getvalue()) == size


@pytest.mark.parametrize("payload,size", [(b"", 1), (b"short", 10)])
def test_truncated_stream_never_produces_a_successful_copy(payload, size):
    output = io.BytesIO()
    with pytest.raises(m.Blocked, match="source_changed"):
        m.bounded_content(io.BytesIO(payload), size, output)
    assert output.getvalue() == payload


def test_short_reads_still_copy_exact_content():
    class ShortReader(io.BytesIO):
        def read(self, count):
            return super().read(min(count, 2))

    output = io.BytesIO()
    assert (
        m.bounded_content(ShortReader(b"correct"), 7, output)
        == hashlib.sha256(b"correct").hexdigest()
    )
    assert output.getvalue() == b"correct"


def test_short_writes_are_rejected():
    class ShortWriter:
        def write(self, value):
            return len(value) - 1

    with pytest.raises(m.Blocked, match="copy_failed"):
        m.bounded_content(io.BytesIO(b"correct"), 7, ShortWriter())


@pytest.mark.parametrize("change", ["append", "truncate", "replace-content"])
def test_copy_rejects_inflight_change_before_checkpoint(
    sample, offline, recipient, monkeypatch, change
):
    original = m.bounded_content
    source = sample.source / "config/sample.txt"
    expected_size = source.stat().st_size
    observed = []

    def mutate(stream, size, output=None):
        if output is not None and not observed:
            if change == "append":
                with source.open("ab") as writer:
                    writer.write(b"appended-outside-readonly-view")
            elif change == "truncate":
                source.write_bytes(b"x")
            else:
                source.write_bytes(b"X" * expected_size)
            try:
                return original(stream, size, output)
            finally:
                observed.append(output.tell())
        return original(stream, size, output)

    monkeypatch.setattr(m, "bounded_content", mutate)
    with pytest.raises(m.Blocked, match="source_changed"):
        m.migrate(apply_args(sample, recipient))
    assert len(observed) == 1 and observed[0] <= expected_size
    assert not sample.destination.exists() and not sample.backup_file.exists()
    assert not list(sample.destination.parent.iterdir())


def test_inventory_hashing_is_also_bounded_on_inflight_append(sample, monkeypatch):
    original = m.bounded_content
    source = sample.source / "config/sample.txt"
    expected_size = source.stat().st_size
    observed = []

    def grow(stream, size, output=None):
        with source.open("ab") as writer:
            writer.write(b"unexpected-growth")
        try:
            return original(stream, size, output)
        finally:
            observed.append(stream.tell())

    monkeypatch.setattr(m, "bounded_content", grow)
    with pytest.raises(m.Blocked, match="source_changed"):
        m.inventory(sample.source)
    assert observed == [expected_size + 1]


@pytest.mark.parametrize("relative", ["config", "var", "docroot/media", "config/sample.txt"])
def test_descriptor_mount_identity_rejects_same_device_mounts(sample, monkeypatch, relative):
    original = m.mount_id
    nested_inode = (sample.source / relative).stat().st_ino
    assert (sample.source / relative).stat().st_dev == sample.source.stat().st_dev

    def nested(descriptor):
        return original(descriptor) + (1 if os.fstat(descriptor).st_ino == nested_inode else 0)

    monkeypatch.setattr(m, "mount_id", nested)
    with pytest.raises(m.Blocked, match="nested_mount_rejected"):
        m.inventory(sample.source)


def test_copy_rechecks_mount_identity_after_inventory(sample, monkeypatch, tmp_path):
    entry = next(
        item for item in document(sample)["entries"] if item["path"] == "config/sample.txt"
    )
    original = m.mount_id
    ancestor_inode = (sample.source / "config").stat().st_ino
    monkeypatch.setattr(
        m, "mount_id", lambda fd: original(fd) + (os.fstat(fd).st_ino == ancestor_inode)
    )
    target = tmp_path / "copy"
    with (
        m.tree_root(sample.source) as descriptor,
        pytest.raises(m.Blocked, match="nested_mount_rejected"),
    ):
        m.copy_entry(descriptor, "config/sample.txt", entry, target)
    assert not target.exists()


@pytest.mark.parametrize(
    "raw", [b"pos:\t0\n", b"mnt_id:\t123\nmnt_id:\t123\n", b"mnt_id:\t-1\n", b"x" * 8193]
)
def test_missing_or_malformed_kernel_mount_identity_fails_closed(monkeypatch, raw):
    monkeypatch.setattr(m, "open", lambda *args, **kwargs: io.BytesIO(raw), raising=False)
    with pytest.raises(m.Blocked, match="mount_identity_unavailable"):
        m.mount_id(100)


@pytest.mark.skipif(
    os.getenv("KLYROW_MAUTIC_MOUNT_REHEARSAL") != "1",
    reason="explicit isolated root mount rehearsal only",
)
@pytest.mark.parametrize("relative", ["config", "var", "docroot/media", "config/sample.txt"])
def test_real_same_filesystem_nested_bind_mount_is_rejected(sample, tmp_path, relative):
    assert os.geteuid() == 0
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    foreign = tmp_path / "foreign"
    original = sample.source / relative
    if original.is_dir():
        shutil.copytree(original, foreign)
    else:
        shutil.copy2(original, foreign)
    subprocess.run(["mount", "--bind", str(sample.source), str(readonly)], check=True)
    nested = readonly / relative
    nested_mounted = False
    try:
        subprocess.run(["mount", "-o", "remount,bind,ro", str(readonly)], check=True)
        subprocess.run(["mount", "--bind", str(foreign), str(nested)], check=True)
        nested_mounted = True
        # A different mount ID with the SAME filesystem device was the original bypass.
        assert readonly.stat().st_dev == nested.stat().st_dev
        assert not nested.is_mount()
        sample.source = readonly
        with pytest.raises(m.Blocked, match="nested_mount_rejected"):
            m.migrate(sample)
        assert not sample.destination.exists() and not sample.backup_file.exists()
    finally:
        if nested_mounted:
            subprocess.run(["umount", str(nested)], check=True)
        subprocess.run(["umount", str(readonly)], check=True)
