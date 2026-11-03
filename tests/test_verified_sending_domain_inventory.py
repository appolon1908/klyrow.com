import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
INVENTORY = ROOT / "evidence/runtime/verified-sending-domains-20260827.json"
BOUNCE_SPF_MANIFEST = ROOT / "docs/evidence/production-20260830/GODADDY_BOUNCE_SPF_MANIFEST.csv"
EXPECTED = [
    "beyvra.com",
    "breero.com",
    "breero.shop",
    "codestra.agency",
    "codestra.cloud",
    "codestra.co",
    "codestra.digital",
    "codestra.media",
    "klyrow.com",
    "kyqra.com",
    "moneybee.loan",
    "moneybeeloan.com",
    "nativoenglish.com",
    "telnexa.co",
]
DOMAIN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


def test_verified_sending_domain_inventory_is_complete_and_non_authorizing():
    data = json.loads(INVENTORY.read_text(encoding="utf-8"))
    assert data["status"] == "VERIFIED_SENDING_ENABLED"
    assert data["domain_count"] == 14
    assert data["domains"] == EXPECTED
    assert data["domains"] == sorted(set(data["domains"]))
    assert all(DOMAIN.fullmatch(domain) for domain in data["domains"])
    assert data["authorization_effect"] == "NONE"
    assert all(value is False for value in data["safety"].values())


def test_bounce_spf_manifest_records_post_change_state():
    with BOUNCE_SPF_MANIFEST.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))

    assert len(rows) == 13
    assert {row["DOMAIN"] for row in rows} == set(EXPECTED) - {"klyrow.com"}
    assert all(row["RECORD_TYPE"] == "TXT" for row in rows)
    assert all(row["HOST"] == "bounce" for row in rows)
    assert all(row["POST_CHANGE_STATE"] == "PRESENT" for row in rows)
    assert all(row["VALUE"] == row["EXPECTED"] for row in rows)
    assert all(row["ACTION_TAKEN"] == "ADD" for row in rows)
    assert all(row["ACTIVATION_REQUIREMENT"] == "SATISFIED" for row in rows)
