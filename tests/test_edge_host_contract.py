from pathlib import Path

ROOT = Path(__file__).parents[1]


def _server_blocks(source: str) -> list[str]:
    blocks = []
    cursor = 0
    while True:
        start = source.find("server {", cursor)
        if start < 0:
            return blocks
        depth = 0
        for index in range(start + len("server "), len(source)):
            if source[index] == "{":
                depth += 1
            elif source[index] == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(source[start : index + 1])
                    cursor = index + 1
                    break
        else:
            raise AssertionError("unbalanced nginx server block")


def _https_block(source: str, hostname: str) -> str:
    matches = [
        block
        for block in _server_blocks(source)
        if "listen 443 ssl;" in block and f"server_name {hostname};" in block
    ]
    assert len(matches) == 1, (hostname, len(matches))
    return matches[0]


def test_frontend_container_serves_only_the_canonical_application_host():
    source = (ROOT / "apps/web/nginx.conf").read_text(encoding="utf-8")
    assert "listen 8080 default_server;" in source
    assert "server_name app.klyrow.com;" in source
    assert "return 421;" in source
    assert 'X-Robots-Tag "noindex, nofollow"' in source
    assert "server_name app.klyrow.com api.klyrow.com" not in source
    assert "server_name track.codestra.co" not in source


def test_frontend_re_resolves_gateway_and_preserves_request_correlation():
    source = (ROOT / "apps/web/nginx.conf").read_text(encoding="utf-8")
    assert "resolver 127.0.0.11 valid=10s ipv6=off;" in source
    assert "zone klyrow_gateway_dynamic 64k;" in source
    assert "server gateway:8000 resolve;" in source
    assert source.count("proxy_pass http://klyrow_gateway_dynamic;") == 4
    assert "proxy_pass http://gateway:8000;" not in source
    assert "map $http_x_request_id $klyrow_request_id" in source
    assert source.count("proxy_set_header X-Request-ID $klyrow_request_id;") == 4


def test_frontend_location_headers_do_not_drop_the_security_policy():
    source = (ROOT / "apps/web/nginx.conf").read_text(encoding="utf-8")
    # Nginx location-level add_header directives replace the server-level set.
    # The immutable-asset and SPA locations therefore repeat every policy.
    assert source.count("add_header Content-Security-Policy") == 3
    assert source.count('add_header X-Content-Type-Options "nosniff" always;') == 3
    assert source.count('add_header X-Frame-Options "DENY" always;') == 3
    assert source.count('add_header Referrer-Policy "no-referrer" always;') == 3
    assert (
        source.count(
            'add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;'
        )
        == 3
    )
    assert source.count('add_header X-Robots-Tag "noindex, nofollow" always;') == 4
    assert source.count("add_header X-Request-ID $klyrow_request_id always;") == 2


def test_provider_edge_keeps_app_api_and_tracking_on_distinct_upstreams():
    source = (ROOT / "docker/proxy/klyrow.conf").read_text(encoding="utf-8")
    assert "map $http_upgrade $connection_upgrade" in source
    app = _https_block(source, "app.klyrow.com")
    api = _https_block(source, "api.klyrow.com")
    tracking = _https_block(source, "track.klyrow.com")
    bounce = _https_block(source, "bounce.klyrow.com")

    assert "proxy_pass http://klyrow_web;" in app
    assert "proxy_pass http://klyrow_gateway;" not in app
    assert "location = /mautic/api { return 404; }" in app
    assert "location ^~ /mautic/api/ { return 404; }" in app
    assert "location = /mautic/oauth { return 404; }" in app
    assert "location ^~ /mautic/oauth/ { return 404; }" in app
    assert app.index("location ^~ /mautic/api/") < app.index("location /mautic/")
    assert app.index("location ^~ /mautic/oauth/") < app.index("location /mautic/")
    assert "location = /ops { return 308 https://app.klyrow.com/ops/; }" in app
    assert "proxy_pass http://127.0.0.1:18003;" in app
    assert "proxy_pass http://127.0.0.1:18003/;" not in app
    assert "proxy_pass http://klyrow_gateway;" in api
    assert "proxy_pass http://klyrow_web;" not in api
    assert "location ^~ /t/" in tracking and "location / { return 404; }" in tracking
    assert "proxy_pass http://klyrow_web;" not in tracking
    assert "location / { return 404; }" in bounce
    assert "server_name track.codestra.co" not in source


def test_edge_activation_probe_covers_bff_and_wrong_host_failures():
    source = (ROOT / "scripts/verify-browser-edge").read_text(encoding="utf-8")
    for marker in ("/auth/session", "/auth/login", "track.codestra.co", "redirect_uri="):
        assert marker in source

    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "scripts/verify-browser-edge" in workflow
    assert "LIVE_EMAIL_DELIVERY=false" in workflow
    assert "PRODUCTION_PROVIDER_ROUTING=false" in workflow
