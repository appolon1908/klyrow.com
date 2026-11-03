"""Route-table uniqueness guard.

Starlette resolves requests by first match, so a second ``APIRoute`` for the
same ``(path, method)`` is silently shadowed. That makes the hardened/unhardened
handler choice depend on router registration order alone. The composition root
asserts at import time that no such duplicate exists; the CLI script
``scripts/check-duplicate-routes.py`` reuses the same grouping.
"""

from collections import defaultdict

from fastapi.routing import APIRoute


def endpoint_label(route: APIRoute) -> str:
    endpoint = route.endpoint
    return f"{getattr(endpoint, '__module__', '?')}:{getattr(endpoint, '__qualname__', '?')}"


def group_api_routes(routes) -> dict[tuple[str, str], list[APIRoute]]:
    """Group every ``APIRoute`` by ``(path, method)`` in registration order."""
    grouped: dict[tuple[str, str], list[APIRoute]] = defaultdict(list)
    for route in routes:
        if not isinstance(route, APIRoute):
            continue
        for method in sorted(route.methods or ()):
            grouped[(route.path, method)].append(route)
    return dict(grouped)


def duplicate_api_routes(routes) -> dict[tuple[str, str], list[APIRoute]]:
    """Return only the ``(path, method)`` keys that have more than one route."""
    return {key: found for key, found in group_api_routes(routes).items() if len(found) > 1}


def assert_unique_api_routes(routes) -> None:
    """Raise ``RuntimeError("duplicate_route: ...")`` when any route is shadowed."""
    duplicates = duplicate_api_routes(routes)
    if not duplicates:
        return
    detail = "; ".join(
        f"{method} {path} -> {', '.join(endpoint_label(route) for route in found)}"
        for (path, method), found in sorted(duplicates.items())
    )
    raise RuntimeError(f"duplicate_route: {detail}")


__all__ = [
    "assert_unique_api_routes",
    "duplicate_api_routes",
    "endpoint_label",
    "group_api_routes",
]
