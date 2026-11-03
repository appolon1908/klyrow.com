import hashlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "archive_b2", Path(__file__).parents[1] / "scripts/archive-backblaze.py"
)
B2 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(B2)


class BackblazeTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.file = self.root / "klyrow-test.tar.gz.gpg"
        self.file.write_bytes(b"\x85ciphertext-test-fixture")
        self.checksum = Path(str(self.file) + ".sha256")
        self.checksum.write_text(f"{B2.digest(self.file)}  {self.file.name}\n")
        self.objects = {}
        self.calls = []
        self.config = (
            "https://s3.us-east-005.backblazeb2.com",
            "Codestra",
            "codestra/klyrow",
            "a" * 40,
            {},
        )

    def fake_aws(self, endpoint, env, action, *args):
        self.calls.append((action, args))
        key = args[args.index("--key") + 1]
        if action == "put-object":
            body = Path(args[args.index("--body") + 1]).read_bytes()
            self.objects[key] = body
            self.assertIn("--server-side-encryption", args)
            return {"VersionId": "version-" + hashlib.sha256(body).hexdigest()}
        self.assertIn("--version-id", args)
        Path(args[-1]).write_bytes(self.objects[key])
        return {"VersionId": args[args.index("--version-id") + 1], "ServerSideEncryption": "AES256"}

    def run_archive(self, call=None):
        with patch.object(B2, "settings", return_value=self.config):
            return B2.archive(self.file, {}, call or self.fake_aws)

    def test_verified_archive_checksum_and_receipt(self):
        receipt = self.run_archive()
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(receipt["readback"], "PASS")
        self.assertTrue(receipt["archive"]["key"].startswith("codestra/klyrow/" + "a" * 40))
        self.assertEqual(json.loads(Path(str(self.file) + ".receipt.json").read_text()), receipt)

    def test_corrupt_readback_never_writes_receipt(self):
        def corrupt(*args):
            result = self.fake_aws(*args)
            if args[2] == "get-object":
                Path(args[-1]).write_bytes(b"corrupt")
            return result

        with self.assertRaisesRegex(B2.BackupError, "b2_readback_mismatch"):
            self.run_archive(corrupt)
        self.assertFalse(Path(str(self.file) + ".receipt.json").exists())

    def test_missing_version_fails_before_download(self):
        with self.assertRaisesRegex(B2.BackupError, "b2_version_missing"):
            self.run_archive(lambda *args: {})

    def test_wrong_version_or_encryption_is_rejected(self):
        for field, value in (("VersionId", "wrong"), ("ServerSideEncryption", "none")):

            def wrong(*args, field=field, value=value):
                result = self.fake_aws(*args)
                if args[2] == "get-object":
                    result[field] = value
                return result

            with self.assertRaisesRegex(B2.BackupError, "b2_readback_mismatch"):
                self.run_archive(wrong)

    def test_checksum_mismatch_has_no_network_side_effect(self):
        self.checksum.write_text("bad\n")
        with self.assertRaisesRegex(B2.BackupError, "checksum_mismatch"):
            self.run_archive()
        self.assertFalse(self.calls)

    def test_plaintext_and_symmetric_archive_are_rejected(self):
        for payload in (b"database dump", b"\x8csymmetric fixture"):
            self.file.write_bytes(payload)
            with self.assertRaisesRegex(B2.BackupError, "public_key_ciphertext_required"):
                self.run_archive()
        self.assertFalse(self.calls)

    def test_local_archive_mutation_is_rejected(self):
        def mutate(*args):
            result = self.fake_aws(*args)
            if args[2] == "get-object":
                self.file.write_bytes(b"\x85changed")
            return result

        with self.assertRaisesRegex(B2.BackupError, "archive_changed"):
            self.run_archive(mutate)

    def test_credentials_never_appear_in_cli_errors(self):
        result = subprocess.CompletedProcess([], 255, "", "InvalidAccessKeyId secret-value request")
        with patch.object(B2.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(B2.BackupError, "^b2_InvalidAccessKeyId$"):
                B2.aws("https://s3.us-east-005.backblazeb2.com", {}, "list-objects-v2")

    def test_credentials_are_file_only_and_ambient_auth_is_discarded(self):
        values = {
            "b2-access-key-id": "test-id",
            "b2-secret-access-key": "test-secret",
            "b2-region": "us-east-005",
            "b2-bucket": "Codestra",
            "b2-endpoint": "https://s3.us-east-005.backblazeb2.com",
        }
        with patch.object(B2, "private_value", side_effect=lambda p: values[p.name]):
            config = B2.settings(
                {
                    "KLYROW_RELEASE_SHA": "a" * 40,
                    "HTTPS_PROXY": "https://proxy.invalid",
                    "AWS_PROFILE": "wrong",
                }
            )
            self.assertNotIn("HTTPS_PROXY", config[-1])
            self.assertNotIn("AWS_PROFILE", config[-1])
            self.assertEqual(config[-1]["AWS_SECRET_ACCESS_KEY"], "test-secret")
            values["b2-endpoint"] = "http://s3.us-east-005.backblazeb2.com"
            with self.assertRaisesRegex(B2.BackupError, "endpoint_invalid"):
                B2.settings({"KLYROW_RELEASE_SHA": "a" * 40})

    def test_credential_symlinks_are_rejected(self):
        credential = self.root / "credential"
        credential.write_text("secret")
        pointer = self.root / "pointer"
        pointer.symlink_to(credential)
        with self.assertRaisesRegex(B2.BackupError, "credential_file_unavailable"):
            B2.private_value(pointer)


if __name__ == "__main__":
    unittest.main()
