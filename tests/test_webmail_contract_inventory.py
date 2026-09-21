from pathlib import Path

from apps.gateway.app.webmail import router as webmail_router


EXPECTED_ROUTES = {
    ("GET", "/app/api/mailboxes"),
    ("POST", "/app/api/mailboxes/sync"),
    ("POST", "/app/api/mailboxes/inbound/activate"),
    ("GET", "/app/api/mailboxes/{mailbox_id}/messages"),
    ("GET", "/app/api/mailboxes/{mailbox_id}/messages/{message_id}"),
    ("GET", "/app/api/mailboxes/{mailbox_id}/messages/{message_id}/attachments/{attachment_id}"),
    ("POST", "/app/api/mailboxes/{mailbox_id}/drafts"),
    ("PUT", "/app/api/mailboxes/{mailbox_id}/drafts/{message_id}"),
    ("POST", "/app/api/mailboxes/{mailbox_id}/send"),
    ("PATCH", "/app/api/mailboxes/{mailbox_id}/messages/{message_id}"),
    ("DELETE", "/app/api/mailboxes/{mailbox_id}/messages/{message_id}"),
    ("GET", "/app/api/mailboxes/{mailbox_id}/access"),
    ("POST", "/app/api/mailboxes/{mailbox_id}/access"),
    ("DELETE", "/app/api/mailboxes/{mailbox_id}/access/{user_id}"),
}


def test_webmail_browser_bff_inventory_is_exact():
    actual = {
        (method, route.path)
        for route in webmail_router.routes
        for method in getattr(route, "methods", set())
    }
    assert actual == EXPECTED_ROUTES


def test_webmail_ui_contract_references_every_mutation_and_never_renders_untrusted_html():
    root = Path(__file__).resolve().parents[1]
    source = (root / "apps/web/src/Webmail.vue").read_text(encoding="utf-8")
    route_manifest = (root / "apps/web/src/routeManifest.ts").read_text(encoding="utf-8")

    assert "path: '/app/mail', descendants: true, view: 'Webmail'" in route_manifest
    for fragment in (
        "'/app/api/mailboxes'",
        "'/app/api/mailboxes/sync'",
        "'/app/api/mailboxes/inbound/activate'",
        "/messages?folder=",
        "/drafts",
        "/send",
        "method:'PATCH'",
        "method:'DELETE'",
        "'Idempotency-Key'",
    ):
        assert fragment in source
    assert "v-html" not in source
    assert "selected.text" in source
    assert "file.download_url" in source
