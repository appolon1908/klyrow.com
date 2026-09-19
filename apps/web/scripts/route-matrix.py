#!/usr/bin/env python3
"""Regenerate docs/frontend/CLIENT_SAAS_ROUTE_MATRIX.md from apps/web/src/portal/routes.ts."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
src = (ROOT / "apps/web/src/portal/routes.ts").read_text(encoding="utf-8")
entries = re.findall(r"(tenant|admin)\(\{(.*?)\}\),\n", src, re.S)
rows = []
for kind, body in entries:
    def field(name, default=""):
        m = re.search(name + r": '((?:[^'\\]|\\.)*)'", body)
        return m.group(1) if m else default
    dep = field("dependency")
    m = re.search(r"dependency: missing\('((?:[^'\\]|\\.)*)'\)", body)
    if m:
        dep = "No browser API exists yet. Required contract: " + m.group(1) + "."
    roles = "OWNER/ADMIN" if "roles: MANAGEMENT" in body else ""
    apis_body = re.search(r"apis: \[(.*?)\]", body, re.S)
    apis = re.findall(r"'([^']+)'", apis_body.group(1)) if apis_body else []
    rows.append(dict(kind=kind, name=field("name"), pattern=field("pattern"), group=field("group"), title=field("title"),
                     capability=field("capability"), roles=roles, availability=field("availability"), dependency=dep, apis=apis))

status = {"implemented": "IMPLEMENTED", "partial": "PARTIAL", "unavailable": "MISSING"}
blocked = {"content-media", "content-brand", "settings-sso", "settings-scim"}
lines = ["# Client SaaS portal — route matrix (Phase 10A)", "",
         "Generated from `apps/web/src/portal/routes.ts` (the single source of truth for",
         "route metadata). Regenerate with the script in `docs/frontend/CLIENT_SAAS_TEST_EVIDENCE.md`",
         "or edit the route table and re-run the route contract tests.", "",
         "Access column: the session capability (`*` grants all) or membership role the page",
         "requires before it renders. Platform-admin routes additionally require server-proven",
         "authority (`GET /app/api/admin/dashboard` → 200). Server authorization remains",
         "authoritative for every call.", "",
         "| Route | Group | Title | Audience | Access | Status | Browser APIs | Dependency / missing contract |",
         "| --- | --- | --- | --- | --- | --- | --- | --- |"]
for r in rows:
    access = r["capability"] or r["roles"] or "session"
    st = "BLOCKED" if r["name"] in blocked else status[r["availability"]]
    aud = "platform-admin" if r["kind"] == "admin" else "tenant"
    apis = ", ".join(f"`{a}`" for a in r["apis"]) or "—"
    lines.append(f"| `{r['pattern']}` | {r['group']} | {r['title']} | {aud} | `{access}` | {st} | {apis} | {r['dependency']} |")
counts = {}
for r in rows:
    st = "BLOCKED" if r["name"] in blocked else status[r["availability"]]
    counts[st] = counts.get(st, 0) + 1
lines += ["", "## Totals", ""] + [f"- {k}: {v}" for k, v in sorted(counts.items())] + [f"- Routes: {len(rows)}", ""]
lines += ["## Navigation groups", "",
          "Overview, Email, Content, Audience, Campaigns, Journeys, Analytics, Deliverability,",
          "Developer, Billing, Settings, Support; the Admin group renders only in the admin",
          "shell after server-proven authority and never appears for ordinary tenants.", "",
          "## Legacy roots preserved", "",
          "`/app` (command center), `/app/mail` (webmail), `/app/provisioning`, `/onboarding`,",
          "`/admin` (legacy admin overview), `/admin/provisioning` and every public",
          "authentication route keep their existing root views and behaviour.", ""]
(ROOT / "docs/frontend/CLIENT_SAAS_ROUTE_MATRIX.md").write_text("\n".join(lines), encoding="utf-8")
print(len(rows), counts)
