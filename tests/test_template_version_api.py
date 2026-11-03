"""History reads must preserve tenant boundaries and immutable content."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.gateway.app.main import auth, db
from apps.gateway.app.platform import app
from apps.gateway.app.messaging import Template, TemplateVersion


@pytest.fixture
def history_client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Template.__table__.create(engine)
    TemplateVersion.__table__.create(engine)
    with Session(engine) as session:
        for tenant in ("a", "b"):
            session.add(
                Template(id=f"tmpl-{tenant}", tenant_id=tenant, slug="invoice", name="Invoice")
            )
            for version in range(1, 4):
                session.add(
                    TemplateVersion(
                        id=f"v-{tenant}-{version}",
                        tenant_id=tenant,
                        template_id=f"tmpl-{tenant}",
                        version=version,
                        subject=f"Revision {version}",
                        html_body=f"<p>{tenant} {version}</p>",
                        text_body=f"{tenant} {version}",
                        variables_json='["invoice"]',
                        created_by=tenant,
                        created_at=datetime(2026, 9, 12, tzinfo=UTC),
                    )
                )
        session.commit()

    def session_dependency():
        with Session(engine) as session:
            yield session

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[db] = session_dependency
    app.dependency_overrides[auth] = lambda: {"tenant": "a", "sub": "reader", "role": "tenant_user"}
    # Do not start delivery workers or application database bootstrap.
    client = TestClient(app)
    try:
        yield client, engine
    finally:
        client.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        engine.dispose()


def test_version_history_is_cursor_paginated_without_duplicates(history_client):
    client, _ = history_client
    first = client.get("/v1/templates/tmpl-a/versions", params={"limit": 2})
    assert first.status_code == 200
    data = first.json()
    assert [item["version"] for item in data["items"]] == [3, 2]
    assert data["items"][0]["created_at"].endswith("Z")
    second = client.get(
        "/v1/templates/tmpl-a/versions", params={"limit": 2, "cursor": data["next_cursor"]}
    )
    assert [item["version"] for item in second.json()["items"]] == [1]
    assert second.json()["next_cursor"] is None


def test_history_content_does_not_change_when_current_version_advances(history_client):
    client, engine = history_client
    url = "/v1/templates/tmpl-a/versions/v-a-1"
    before = client.get(url).json()
    with Session(engine) as session:
        session.get(Template, "tmpl-a").current_version = 3
        session.commit()
    assert client.get(url).json() == before
    assert before["html_body"] == "<p>a 1</p>"
    assert before["variables"] == ["invoice"]


@pytest.mark.parametrize(
    "path",
    [
        "/v1/templates/tmpl-b/versions",
        "/v1/templates/tmpl-b/versions/v-b-1",
        "/v1/templates/tmpl-a/versions/v-b-1",
        "/v1/templates/missing/versions",
    ],
)
def test_cross_tenant_and_wrong_parent_versions_are_not_found(history_client, path):
    client, _ = history_client
    assert client.get(path).status_code == 404


@pytest.mark.parametrize(
    "params",
    [{"cursor": "!!!!"}, {"cursor": "MA=="}, {"cursor": "LTE="}, {"limit": 0}, {"limit": 101}],
)
def test_invalid_page_requests_are_rejected(history_client, params):
    client, _ = history_client
    assert client.get("/v1/templates/tmpl-a/versions", params=params).status_code == 422


def test_history_requires_real_authentication():
    client = TestClient(app)
    try:
        assert client.get("/v1/templates/unknown/versions").status_code == 401
    finally:
        client.close()
