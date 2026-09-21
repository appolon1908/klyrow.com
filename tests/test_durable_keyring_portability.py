import pytest
from apps.gateway.app import durable_keys


@pytest.mark.parametrize("flag", ["O_NOFOLLOW", "O_NONBLOCK"])
@pytest.mark.parametrize("missing", [True, False])
def test_missing_secure_open_flag_rejects_before_open(tmp_path, monkeypatch, flag, missing):
    path = tmp_path / "keyring.json"
    path.write_text(durable_keys.new_keyring_document(), encoding="utf-8")
    path.chmod(0o600)

    def forbidden_open(*args, **kwargs):
        raise AssertionError("unsafe file open attempted")

    with monkeypatch.context() as patch:
        if missing:
            patch.delattr(durable_keys.os, flag, raising=False)
        else:
            patch.setattr(durable_keys.os, flag, 0, raising=False)
        patch.setattr(durable_keys.os, "open", forbidden_open)
        with pytest.raises(durable_keys.KeyringError):
            durable_keys.load_keyring(path)
