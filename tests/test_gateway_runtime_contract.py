from pathlib import Path

from apps.gateway.app import auth_bff, main

ROOT = Path(__file__).parents[1]


def _env_value(name: str) -> str:
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1]
    raise AssertionError(f"missing {name}")


def test_base_gateway_image_bundles_reviewed_vue_application():
    dockerfile = (ROOT / "apps/gateway/Dockerfile").read_text(encoding="utf-8")
    assert "FROM node:22-bookworm-slim@sha256:" in dockerfile
    assert " AS web-build" in dockerfile.splitlines()[0]
    assert "RUN pnpm build" in dockerfile
    assert "COPY --from=web-build /build/dist ./app/auth_web" in dockerfile
    assert '"app.platform:app"' in dockerfile


def test_bundled_vue_assets_resolve_from_the_distribution_root():
    mount = next(route for route in main.app.routes if getattr(route, "path", "") == "/auth-assets")
    assert Path(mount.app.directory) == main.AUTH_WEB_DIST


def test_example_uses_public_pkce_client_without_unmounted_secret(monkeypatch):
    assert _env_value("KLYROW_OIDC_CLIENT_SECRET_FILE") == ""
    monkeypatch.setenv("KLYROW_OIDC_CLIENT_SECRET_FILE", "")
    assert auth_bff._client_secret() is None
