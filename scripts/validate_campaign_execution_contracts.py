"""Offline reference validator for MCR-D; never contacts a provider."""
from __future__ import annotations
import hashlib, json
from datetime import datetime
from pathlib import Path
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

ROOT=Path(__file__).resolve().parents[1]
DIR=ROOT/"contracts/campaign-execution"
IDENTITY=("tenant_id","lead_id","campaign_id","campaign_version","channel","touch_index")

def _canonical(v): return json.dumps(v,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()
def exposure_key(touch):
    return "mcr1:"+hashlib.sha256(_canonical({k:touch[k] for k in IDENTITY})).hexdigest()
def request_hash(plan):
    value={k:v for k,v in plan.items() if k!="request_hash"}
    return hashlib.sha256(_canonical(value)).hexdigest()
def replay_binding(original,current):
    if original==current:return "duplicate"
    raise ValueError("idempotency_conflict")
def _dt(v):
    value=datetime.fromisoformat(v.replace("Z","+00:00"))
    if value.tzinfo is None: raise ValueError("timezone_required")
    return value
def validate_plan(plan,tenant):
    schema=json.loads((DIR/"execution.v1.schema.json").read_text())
    try:
        Draft202012Validator(schema,format_checker=Draft202012Validator.FORMAT_CHECKER).validate(plan)
    except ValidationError as exc:
        raise ValueError("invalid_plan") from exc
    touch=plan["touch"]
    if touch["tenant_id"]!=tenant: raise ValueError("tenant_mismatch")
    if exposure_key(touch)!=plan["exposure_idempotency_key"]: raise ValueError("binding_mismatch")
    if request_hash(plan)!=plan["request_hash"]: raise ValueError("request_hash_mismatch")
    if _dt(plan["schedule"]["expires_at"]) <= _dt(plan["schedule"]["not_before"]): raise ValueError("invalid_schedule")
    return plan
def validate_readback(plan,result,tenant):
    validate_plan(plan,tenant)
    if set(result)!={"schema_version","command_id","request_hash","exposure_idempotency_key","touch","status","provider_effects"}:
        raise ValueError("readback_fields")
    if result["schema_version"]!="1.0" or result["provider_effects"]!="disabled": raise ValueError("unsafe_readback")
    if result["touch"]!=plan["touch"] or result["touch"]["tenant_id"]!=tenant: raise ValueError("binding_mismatch")
    if result["request_hash"]!=plan["request_hash"] or result["exposure_idempotency_key"]!=plan["exposure_idempotency_key"]: raise ValueError("binding_mismatch")
    import uuid
    uuid.UUID(result["command_id"])
    expected_command_id=str(uuid.uuid5(uuid.NAMESPACE_URL, "klyrow-mcr:"+plan["request_hash"]))
    if result["command_id"] != expected_command_id: raise ValueError("binding_mismatch")
    return result
def map_event(raw,bounce_class=None):
    if raw=="klyrow.email.bounced":
        if bounce_class not in {None,"soft","hard","unknown"}: raise ValueError("invalid_bounce_class")
        event="soft_bounce" if bounce_class=="soft" else "hard_bounce"
        return {"normalized_event":event,"block_address":event=="hard_bounce","suppression_scope":None,"suppression_reason":None}
    doc=json.loads((DIR/"event-mapping.v1.json").read_text())["mapping"]
    if raw not in doc: raise ValueError("unmapped_event")
    row={"block_address":False,"suppression_scope":None,"suppression_reason":None,**doc[raw]}
    return row
def recovery(outcome,attempt,retry_after_seconds=None):
    if type(attempt) is not int or not 1<=attempt<=5: raise ValueError("invalid_attempt")
    if outcome=="transient_before_admission":
        delay=[30,60,120,240][min(attempt,4)-1]
        if retry_after_seconds is not None:
            if type(retry_after_seconds) is not int or retry_after_seconds<0: raise ValueError("invalid_retry_after")
            if retry_after_seconds>900:return {"action":"operator_review","delay_seconds":None}
            delay=max(delay,retry_after_seconds)
        return {"action":"retry" if attempt<5 else "dead_letter","delay_seconds":delay if attempt<5 else None}
    if outcome in {"unknown_outcome","readback_mismatch"}: return {"action":"reconcile","delay_seconds":None}
    if outcome=="permanent_failure": return {"action":"dead_letter","delay_seconds":None}
    if outcome in {"delivered","suppressed"}: return {"action":"no_send","delay_seconds":None}
    raise ValueError("unknown_recovery_outcome")
def authorize(principal,tenant,*,audience,azp,subject):
    if not audience or not azp or not subject: raise ValueError("auth_unconfigured")
    ok=(principal.get("verified") is True and principal.get("issuer")=="https://auth.codestra.co/realms/codestra"
        and principal.get("audience")==audience and principal.get("azp")==azp and principal.get("service") is True
        and principal.get("subject")==subject and principal.get("tenant_id")==tenant and "klyrow.read" in principal.get("scopes",[]))
    if not ok: raise ValueError("unauthorized")
def validate_bundle():
    required=("execution.v1.schema.json","execution.openapi.json","event-mapping.v1.json","examples/plan.json","examples/readback.json")
    for name in required:
        if not (DIR/name).is_file(): raise ValueError("missing_contract_artifact")
    schema=json.loads((DIR/"execution.v1.schema.json").read_text()); Draft202012Validator.check_schema(schema)
    api=json.loads((DIR/"execution.openapi.json").read_text())
    if api.get("x-runtime-status")!="contract_only": raise ValueError("runtime_activation")
    readback=api.get("paths",{}).get("/v1/campaign-executions/{command_id}",{}).get("get",{})
    params=readback.get("parameters",[])
    if not any(p.get("name")=="command_id" and p.get("in")=="path" and p.get("required") is True for p in params):
        raise ValueError("command_id_path_parameter")
    plan=json.loads((DIR/"examples/plan.json").read_text()); validate_plan(plan,plan["touch"]["tenant_id"])
    read=json.loads((DIR/"examples/readback.json").read_text()); validate_readback(plan,read,plan["touch"]["tenant_id"])
    return True
if __name__=="__main__":
    validate_bundle(); print("MCR_D_CONTRACTS=PASS EXTERNAL_EFFECTS=NONE RELEASE=NO_GO")
