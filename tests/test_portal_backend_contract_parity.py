"""Same-origin portal route manifest must match real authenticated backend APIs.

Fail if an implemented/partial frontend page advertises an API that does not
exist in FastAPI's OpenAPI schema. Unavailable pages must be explicit; they may
not silently masquerade as fully implemented navigation flows.
"""
from collections import Counter
from pathlib import Path
import re

from apps.gateway.app.platform import app  # Composed gateway, not the inner FastAPI application

ROUTES = Path(__file__).resolve().parents[1] / "apps/web/src/portal/routes.ts"
ROUTE_PATTERN = re.compile(r"(?:tenant|admin)\(\{\s*name:\s*'([^']+)'.*?\}\)",re.S)
API_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})


def declared_routes():
    source = ROUTES.read_text(encoding="utf-8")
    for match in ROUTE_PATTERN.finditer(source):
        body = match.group()
        name = match.group(1)
        state_match = re.search(r"availability:\s*'([^']+)'",body)
        paths_match = re.search(r"\bapis:\s*\[([^\]]*)\]",body,re.S)
        assert state_match and paths_match, f"missing explicit route state/API list for {name}"
        apis = re.findall(r"'(GET|POST|PATCH|PUT|DELETE) ([^']+)'",paths_match.group(1))
        yield name,state_match.group(1),apis


def path_matches(declared: str, actual: str) -> bool:
    regex = "^" + re.sub(r"\{[^}]+\}",r"[^/]+",declared) + "$"
    return re.fullmatch(regex,actual) is not None


def test_implemented_routes_have_current_backend_contracts():
    routes = list(declared_routes())
    assert len(routes)>=70
    assert len({r[0] for r in routes})==len(routes)
    schema = app.openapi()
    registered={(method.upper(),path) for path,entry in schema["paths"].items() for method in entry if method.upper() in API_METHODS}
    checked = set()
    for name,availability,calls in routes:
        assert availability in {"implemented","partial","unavailable"},(name,availability)
        if availability == "unavailable":
            assert not calls, f"unavailable route unexpectedly advertises active API: {name}"
            continue
        assert calls, f"active frontend route missing API dependency: {name}"
        for method,route in calls:
            assert route.startswith("/app/api/") or route.startswith("/auth/"),(name,method,route)
            assert any(method==m and path_matches(route,path) for m,path in registered), (
                f"frontend {name} references nonexistent API {method} {route}"
            )
            checked.add((method,route))
    assert len(checked)>=55


def test_unavailable_pages_are_not_reported_as_complete():
    counts=Counter(availability for _,availability,_ in declared_routes())
    assert counts["unavailable"]>=1
    assert counts["implemented"]>=20
    assert counts["partial"]>=5
