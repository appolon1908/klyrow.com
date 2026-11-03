#!/usr/bin/env python3
"""List every (path, method) registered more than once on the composed app.

Exit status 1 when a duplicate exists. The startup guard in
``apps/gateway/app/platform.py`` shares the grouping logic
(``apps/gateway/app/routing_guards.py``); when it fires during import the
message is printed as the duplicate list (first endpoint per key resolves,
later ones are shadowed).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    for name in list(os.environ):
        if name.startswith(("KLYROW_", "CODESTRA_")):
            del os.environ[name]
    os.environ.update(
        KLYROW_ENV="development",
        KLYROW_DATABASE_URL="sqlite://",
        KLYROW_SAFE_MODE="true",
        LIVE_EMAIL_DELIVERY="false",
        EXTERNAL_EMAIL_DELIVERY="false",
        PRODUCTION_PROVIDER_ROUTING="false",
    )
    from apps.gateway.app.routing_guards import duplicate_api_routes, endpoint_label

    try:
        from apps.gateway.app.platform import app
    except RuntimeError as exc:
        if not str(exc).startswith("duplicate_route:"):
            raise
        print(str(exc).removeprefix("duplicate_route: ").replace("; ", "\n"))
        return 1
    duplicates = duplicate_api_routes(app.router.routes)
    if not duplicates:
        print("no duplicate (path, method) routes")
        return 0
    for (path, method), routes in sorted(duplicates.items()):
        print(f"{method} {path} -> " + ", ".join(endpoint_label(route) for route in routes))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
