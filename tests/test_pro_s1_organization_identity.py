from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_pro_s1_contract_and_no_identity_bypass():
    api=(ROOT/"apps/gateway/app/tenancy_onboarding.py").read_text()
    routes=(ROOT/"apps/web/src/portal/routes.ts").read_text()
    team=(ROOT/"apps/web/src/portal/pages/SettingsTeamPage.vue").read_text()
    enterprise=(ROOT/"apps/web/src/portal/pages/SettingsEnterpriseIdentityPage.vue").read_text()
    for endpoint in (
      "/app/api/team/invitations",
      "/app/api/team/{user_id}",
      "/app/api/identity/capabilities",
    ):
      assert endpoint in api
    assert "last_owner_protected" in api
    assert '"direct_keycloak_writes": False' in api
    assert "governed Keycloak/Middleware provisioning contract" in api
    assert "Pending invitations" in team
    assert "changeRole" in team and "removeMember" in team and "revokeInvite" in team
    assert "SettingsEnterpriseIdentityPage" in (ROOT/"apps/web/src/portal/pages/index.ts").read_text()
    assert routes.count("GET /app/api/identity/capabilities") >= 2
    assert "Direct Keycloak writes" in enterprise

def test_pro_s1_authority_freeze_exists():
    doc=(ROOT/"docs/architecture/PRO_S1_ORGANIZATION_IDENTITY.md").read_text(encoding="utf-8")
    assert "Keycloak owns human authentication" in doc
    assert "Klyrow PostgreSQL owns" in doc
    assert "Caddy → Kong → Middleware → authorized adapter" in doc
