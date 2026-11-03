import runpy
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts/ops/verify-klyrow-sending-domains"
EVIDENCE = ROOT / "evidence/runtime/verified-sending-domains-20260827.json"


def test_domain_runtime_verifier_uses_the_authoritative_evidence_file():
    module = runpy.run_path(str(SCRIPT), run_name="klyrow_domain_verifier")
    domains = module["load_manifest"](EVIDENCE)
    assert len(domains) == 14
    assert domains == sorted(domains)
    assert "klyrow.com" in domains
    assert "telnexa.co" in domains


def test_domain_runtime_verifier_is_read_only_and_detects_extra_domains():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "SELECT lower(domain)" in source
    assert "FROM domain_claims" in source
    assert "unexpected_sending_enabled" in source
    for forbidden in (
        "INSERT INTO",
        "UPDATE domain_claims",
        "DELETE FROM domain_claims",
        "ALTER TABLE domain_claims",
        "DROP TABLE domain_claims",
    ):
        assert forbidden not in source
