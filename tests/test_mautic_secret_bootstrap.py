from pathlib import Path

import pytest
from test_middleware_transport_migration import _migration_module


@pytest.fixture
def migration(tmp_path, monkeypatch):
    module = _migration_module()
    # Exercise the real script against only an isolated synthetic repository.
    (tmp_path / "scripts").mkdir()
    monkeypatch.setattr(module, "__file__", str(tmp_path / "scripts/migrate-runtime-secrets"))
    monkeypatch.setenv("KLYROW_RUNTIME_SECRET_DIR", str(tmp_path / "secrets"))
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    # Non-root CI cannot chown; assert requested ownership without changing it.
    ownership = []
    monkeypatch.setattr(
        module.os, "chown", lambda path, uid, gid: ownership.append((Path(path), uid, gid))
    )
    return module, tmp_path, ownership


@pytest.mark.parametrize("provided", [False, True])
def test_mautic_bootstrap_is_private_silent_and_idempotent(migration, capsys, provided):
    module, root, ownership = migration
    values = (
        {
            "KLYROW_MAUTIC_API_CLIENT_ID": "synthetic-client-id",
            "KLYROW_MAUTIC_API_CLIENT_SECRET": "synthetic-client-secret",
        }
        if provided
        else {}
    )
    env = root / ".env"
    env.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
    module.main()
    first = env.read_bytes()
    module.main()
    assert env.read_bytes() == first
    for name, basename in (
        ("KLYROW_MAUTIC_API_CLIENT_ID", "mautic-api-client-id"),
        ("KLYROW_MAUTIC_API_CLIENT_SECRET", "mautic-api-client-secret"),
    ):
        target = root / "secrets" / basename
        assert target.read_text() == values.get(name, "")
        assert target.stat().st_mode & 0o777 == 0o600
        assert f"{name}_FILE={target}" in env.read_text()
        assert f"{name}=" not in env.read_text()
        assert (target, 0, 0) in ownership
    assert env.stat().st_mode & 0o777 == 0o600
    output = capsys.readouterr()
    assert "synthetic-client" not in output.out + output.err


def test_existing_oauth_file_authority_is_preserved(migration):
    module, root, _ = migration
    target = root / "approved-client-secret"
    target.write_text("existing-synthetic-secret")
    env = root / ".env"
    env.write_text(f"KLYROW_MAUTIC_API_CLIENT_SECRET_FILE={target}\n")
    module.main()
    module.main()
    assert target.read_text() == "existing-synthetic-secret"
    assert f"KLYROW_MAUTIC_API_CLIENT_SECRET_FILE={target}" in env.read_text()
    assert not (root / "secrets/mautic-api-client-secret").exists()


@pytest.mark.parametrize("kind", ["missing", "symlink", "directory"])
def test_invalid_existing_secret_reference_fails_without_replacing_env(migration, kind):
    module, root, _ = migration
    target = root / "configured-secret"
    if kind == "directory":
        target.mkdir()
    elif kind == "symlink":
        original = root / "original"
        original.write_text("must-not-change")
        target.symlink_to(original)
    env = root / ".env"
    original_env = f"KLYROW_MAUTIC_API_CLIENT_SECRET_FILE={target}\n"
    env.write_text(original_env)
    with pytest.raises(SystemExit):
        module.main()
    assert env.read_text() == original_env
    if kind == "symlink":
        assert (root / "original").read_text() == "must-not-change"


def test_incomplete_secret_write_never_installs_empty_authority(migration, monkeypatch):
    module, root, _ = migration
    directory = root / "secrets"
    directory.mkdir()
    target = directory / "mautic-api-client-secret"
    real_fsync = module.os.fsync

    def full_disk(_descriptor):
        raise OSError("synthetic disk full")

    monkeypatch.setattr(module.os, "fsync", full_disk)
    with pytest.raises(OSError):
        module.create_secret_file(target, "synthetic-legacy-credential")
    assert not target.exists()
    assert list(directory.iterdir()) == []
    monkeypatch.setattr(module.os, "fsync", real_fsync)
    module.create_secret_file(target, "synthetic-legacy-credential")
    assert target.read_text() == "synthetic-legacy-credential"
    assert target.stat().st_mode & 0o777 == 0o600


def test_old_empty_file_cannot_discard_remaining_legacy_credential(migration):
    module, root, _ = migration
    directory = root / "secrets"
    directory.mkdir()
    target = directory / "mautic-api-client-secret"
    target.touch()
    env = root / ".env"
    original = "KLYROW_MAUTIC_API_CLIENT_SECRET=synthetic-legacy-credential\n"
    env.write_text(original)
    with pytest.raises(SystemExit):
        module.main()
    assert env.read_text() == original
    assert target.read_text() == ""


def test_atomic_install_does_not_overwrite_a_concurrent_winner(migration):
    module, root, _ = migration
    target = root / "winner"
    target.write_text("approved-existing-credential")
    with pytest.raises(SystemExit):
        module.create_secret_file(target, "losing-credential")
    assert target.read_text() == "approved-existing-credential"
    assert not list(root.glob(".credential-migrating-*"))


def test_durable_keyring_bootstrap_is_private_and_never_rotates_on_repeat(migration):
    from apps.gateway.app.durable_keys import KEYRING_ENV, load_keyring

    module, root, ownership = migration
    env = root / ".env"
    env.write_text("")
    module.main()
    path = root / "secrets/durable-result-keyring.json"
    before = path.read_bytes()
    keyring = load_keyring(path)
    module.main()
    assert path.read_bytes() == before
    assert keyring == load_keyring(path)
    assert f"{KEYRING_ENV}={path}" in env.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    assert (path, 0, 0) in ownership


def test_invalid_durable_keyring_cannot_be_silently_replaced(migration):
    module, root, _ = migration
    path = root / "approved-keyring"
    path.write_text("corrupt-authority")
    env = root / ".env"
    original = f"KLYROW_DURABLE_RESULT_KEYRING_FILE={path}\n"
    env.write_text(original)
    with pytest.raises(SystemExit):
        module.main()
    assert env.read_text() == original
    assert path.read_text() == "corrupt-authority"


def test_generate_env_bootstrap_then_configured_migration_preserves_key(migration, monkeypatch):
    import sys

    from apps.gateway.app.durable_keys import KEYRING_ENV, load_keyring

    module, root, _ = migration
    path = root / "secrets/durable-result-keyring.json"
    path.parent.mkdir()
    repository = Path(__file__).resolve().parents[1]
    text = (repository / "scripts/generate-env").read_text()
    snippet = text.split("<<'PY_KEYRING'\n", 1)[1].split("\nPY_KEYRING", 1)[0]
    monkeypatch.chdir(repository)
    monkeypatch.setattr(sys, "argv", ["-", str(path)])
    assert not path.exists()
    exec(compile(snippet, "generate-env-keyring", "exec"), {})
    first = path.read_bytes()
    assert load_keyring(path).active_key_id
    exec(compile(snippet, "generate-env-keyring", "exec"), {})
    assert path.read_bytes() == first
    (root / ".env").write_text(f"{KEYRING_ENV}={path}\n")
    module.main()
    module.main()
    assert path.read_bytes() == first
    assert path.stat().st_mode & 0o777 == 0o600


def test_missing_configured_default_keyring_is_not_silently_replaced(migration):
    module, root, _ = migration
    path = root / "secrets/durable-result-keyring.json"
    original = f"KLYROW_DURABLE_RESULT_KEYRING_FILE={path}\n"
    (root / ".env").write_text(original)
    with pytest.raises(SystemExit):
        module.main()
    assert not path.exists()
    assert (root / ".env").read_text() == original


def test_missing_custom_keyring_does_not_invent_authority(migration):
    module, root, _ = migration
    path = root / "approved-but-missing-keyring"
    original = f"KLYROW_DURABLE_RESULT_KEYRING_FILE={path}\n"
    (root / ".env").write_text(original)
    with pytest.raises(SystemExit):
        module.main()
    assert not path.exists()
    assert (root / ".env").read_text() == original


def test_generate_env_rewrites_the_keyring_to_the_selected_secret_directory():
    text = (Path(__file__).resolve().parents[1] / "scripts/generate-env").read_text()
    assert (
        "s|KLYROW_DURABLE_RESULT_KEYRING_FILE=.*|KLYROW_DURABLE_RESULT_KEYRING_FILE=$runtime_secret_dir/durable-result-keyring.json|"
        in text
    )
