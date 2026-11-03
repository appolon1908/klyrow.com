import json
import sys
import urllib.error
import urllib.request
import uuid

tenant = sys.argv[1]
env = dict(
    line.rstrip().split("=", 1)
    for line in open("/etc/codestra/secrets/kyqra-telnexa/middleware.env")
    if line.strip() and not line.startswith("#")
)
url = "http://10.40.0.2:18000/v1/email/send"
payload = {
    "customer_id": tenant,
    "from": "noreply@klyrow.com",
    "to": "middleware-controlled@example.net",
    "reply_to": "support@klyrow.com",
    "subject": "Middleware controlled safe-mode test",
    "html": "<p>No external delivery</p>",
    "text": "No external delivery",
    "campaign_id": "controlled",
    "tags": ["controlled"],
    "tracking": {"opens": True, "clicks": True},
    "callback_metadata": {"source": "middleware"},
}


def call(token, key):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    headers = {
        "Authorization": "Bearer " + token,
        "X-Klyrow-Tenant-Id": tenant,
        "Idempotency-Key": key,
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, raw, headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


key = "middleware-smoke-" + str(uuid.uuid4())
s1, b1 = call(env["KLYROW_MIDDLEWARE_API_KEY"], key)
s2, b2 = call(env["KLYROW_MIDDLEWARE_API_KEY"], key)
s3, _ = call("invalid", key + "-bad")
tests = [
    ("middleware_send", s1 == 202 and b1.get("safe_mode") is True),
    ("middleware_idempotency", s2 == 202 and b2.get("id") == b1.get("id")),
    ("middleware_invalid_key", s3 == 401),
]
for name, ok in tests:
    print(name + "=" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if all(ok for _, ok in tests) else 1)
