from pathlib import Path
import json
import yaml

ROOT = Path(__file__).resolve().parents[1]

def test_webmail_postal_observability_contract():
    rules = yaml.safe_load((ROOT/"monitoring/klyrow-webmail-postal-recording-rules.yml").read_text())
    alerts = yaml.safe_load((ROOT/"monitoring/klyrow-webmail-postal-alerts.yml").read_text())
    dashboard = json.loads((ROOT/"monitoring/grafana/klyrow-webmail-postal.json").read_text())
    rule_names = {r["record"] for g in rules["groups"] for r in g["rules"]}
    required = {
        "klyrow:webmail_inbound_failure:ratio15m",
        "klyrow:webmail_send_failure:ratio15m",
        "klyrow:webmail_queue_age_seconds:max",
        "klyrow:webmail_provider_latency:p95",
        "klyrow:webmail_bounce:ratio30m",
        "klyrow:webmail_reconciliation_failures:sum",
    }
    assert required <= rule_names
    alert_names = {r["alert"] for g in alerts["groups"] for r in g["rules"]}
    assert len(alert_names) >= 6
    queries = " ".join(t["expr"] for p in dashboard["panels"] for t in p.get("targets", []))
    assert required <= {name for name in required if name in queries}

def test_observability_has_no_high_cardinality_or_pii_labels():
    text = "\n".join([
        (ROOT/"monitoring/klyrow-webmail-postal-recording-rules.yml").read_text(),
        (ROOT/"monitoring/klyrow-webmail-postal-alerts.yml").read_text(),
        (ROOT/"monitoring/grafana/klyrow-webmail-postal.json").read_text(),
    ]).lower()
    for forbidden in ("tenant_id=", "email=", "recipient=", "sender=", "message_id=", "subject=", "body="):
        assert forbidden not in text

def test_slo_and_runbook_keep_delivery_fail_closed():
    text = ((ROOT/"docs/WEBMAIL_POSTAL_SLOS.md").read_text() + (ROOT/"docs/runbooks/WEBMAIL_POSTAL_OBSERVABILITY.md").read_text())
    assert "LIVE_EMAIL_DELIVERY" in text
    assert "EXTERNAL_EMAIL_DELIVERY" in text
    assert "PRODUCTION_PROVIDER_ROUTING" in text
    assert "reconcile before retry" in text.lower()
