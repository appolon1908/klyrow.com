from pathlib import Path

from fastapi.testclient import TestClient

from apps.gateway.app.main import app

ROOT = Path(__file__).parents[1]
client = TestClient(app)


def test_canonical_message_route_is_in_openapi():
    schema = client.get("/openapi.json").json()
    assert "post" in schema["paths"]["/v1/messages"]
    assert "/v1/email/send" not in schema["paths"]


def test_auth_ui_routes_drop_legacy_portal_and_do_not_implement_bff():
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/portal" not in paths
    source = (ROOT / "apps/web/src/App.vue").read_text(encoding="utf-8")
    assert "/auth/google?return_to=" in source
    assert "localStorage" not in source and "sessionStorage" not in source
    assert "client_secret" not in source and "token_endpoint" not in source


def test_keycloak_theme_has_complete_localized_surfaces():
    theme = ROOT / "themes/klyrow"
    english = (theme / "login/messages/messages_en.properties").read_text(encoding="utf-8")
    spanish = (theme / "login/messages/messages_es.properties").read_text(encoding="utf-8")
    for key in (
        "loginTitle",
        "registerTitle",
        "emailVerifyTitle",
        "emailForgotTitle",
        "updatePasswordTitle",
        "pageExpiredTitle",
        "errorTitle",
        "termsTitle",
        "configureTotpTitle",
        "confirmLinkIdpTitle",
        "logoutConfirmTitle",
    ):
        assert f"{key}=" in english and f"{key}=" in spanish
    assert "client_secret" not in "\n".join(
        path.read_text(encoding="utf-8") for path in theme.rglob("*") if path.is_file()
    )
