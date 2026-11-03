from fastapi import APIRouter, FastAPI, Header

from apps.gateway.app.openapi_authority import (
    runtime_idempotency_routes,
    runtime_route_fingerprint,
    runtime_routes,
)


def test_effective_route_audit_follows_nested_included_routers() -> None:
    leaf = APIRouter(prefix="/leaf")

    @leaf.post("/operation", include_in_schema=False)
    def operation(
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, str]:
        return {"idempotency_key": idempotency_key}

    middle = APIRouter(prefix="/middle")
    middle.include_router(leaf)

    app = FastAPI()
    app.include_router(middle, prefix="/root")

    expected = ("post", "/root/middle/leaf/operation")
    assert runtime_idempotency_routes(app) == {expected}

    rows = runtime_routes(app)
    assert rows == [
        (
            "post",
            "/root/middle/leaf/operation",
            False,
            "operation",
        )
    ]
    assert runtime_route_fingerprint(rows) == runtime_route_fingerprint(runtime_routes(app))
