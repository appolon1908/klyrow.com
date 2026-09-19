import base64
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.gateway.app.account_api import router
from apps.gateway.app.main import Tenant, auth, db
from apps.gateway.app.tenancy import Organization, TenantMember


@pytest.fixture
def client():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    for model in (Tenant, Organization, TenantMember):
        model.__table__.create(engine)
    with Session(engine) as s:
        for tenant in ("a", "b"):
            s.add(Tenant(id=tenant, name=tenant))
            s.add(Organization(id="org-"+tenant, tenant_id=tenant, name=tenant, slug=tenant))
            for i in range(3):
                s.add(TenantMember(id=f"{tenant}-{i}", tenant_id=tenant, user_id=f"{tenant}-{i}", role="READ_ONLY", active=i != 2))
        s.commit()
    app = FastAPI()
    app.include_router(router)
    def session():
        with Session(engine) as s:
            yield s
    app.dependency_overrides[db] = session
    app.dependency_overrides[auth] = lambda: {"tenant": "a", "sub": "a-0"}
    with TestClient(app) as c:
        yield c
    engine.dispose()


def test_account_reads_are_tenant_scoped_and_cursor_paginated(client):
    assert client.get("/v1/organization").json()["id"] == "org-a"
    first = client.get("/v1/members?limit=1").json()
    assert first["items"] == [{"id": "a-0", "user_id": "a-0", "role": "READ_ONLY"}]
    last = client.get("/v1/members", params={"cursor": first["next_cursor"]}).json()
    assert [m["id"] for m in last["items"]] == ["a-1"]
    assert last["next_cursor"] is None
    alien = base64.urlsafe_b64encode(json.dumps({"tenant_id": "b", "after": "b-0"}).encode()).decode()
    assert client.get("/v1/members", params={"cursor": alien}).status_code == 422
    assert client.get("/v1/members?cursor=!!!!").status_code == 422
