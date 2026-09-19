from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("acceptance", Path(__file__).parents[1]/"scripts/validate-production-acceptance.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def test_missing_gate_or_stale_mismatched_artifact_blocks_release(tmp_path):
    now = datetime.now(timezone.utc)
    content = b"synthetic test proof, not production evidence"
    (tmp_path/"proof.txt").write_bytes(content)
    document = {"source_sha": "a"*40, "image_digest": "sha256:"+"b"*64,
                "gates": {name: {"status": "PASS", "observed_at": now.isoformat(),
                                  "artifact": "proof.txt", "sha256": hashlib.sha256(content).hexdigest()}
                          for name in gate.REQUIRED_GATES}}
    def check():
        return gate.validate(document, source_sha="a"*40, image_digest="sha256:"+"b"*64,
                             artifacts=tmp_path, now=now)
    assert check() == []
    document["gates"]["backup_restore"]["status"] = "NOT APPLICABLE"
    assert "backup_restore:not_passed" in check()
    document["gates"]["backup_restore"]["status"] = "PASS"
    document["gates"]["backup_restore"]["observed_at"] = (now-timedelta(days=2)).isoformat()
    assert "backup_restore:invalid_evidence" in check()
    document["source_sha"] = "c"*40
    assert "release_identity_mismatch" in check()
    assert len(gate.validate({}, source_sha="a"*40, image_digest="sha256:"+"b"*64,
                             artifacts=tmp_path, now=now)) == len(gate.REQUIRED_GATES)+1
