"""Event insert races and old-writer compatibility in disposable PostgreSQL."""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from apps.gateway.app.main import Base, Tenant
from apps.gateway.app.saas import CustomerEvent, EventIn, Profile, ingest_event

pytestmark = pytest.mark.skipif(
    not os.getenv("KLYROW_CONTRACT_POSTGRES_URL"), reason="Requires disposable PostgreSQL"
)


@pytest.fixture
def event_database():
    url = os.environ["KLYROW_CONTRACT_POSTGRES_URL"]
    admin = create_engine(url)
    schema = "event_test_" + uuid.uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id="tenant", name="Synthetic", quota=100))
        session.flush()
        session.add(Profile(id="profile", tenant_id="tenant"))
        session.commit()
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.parametrize("conflict", [False, True])
def test_overlapping_event_retries_recover_winner(event_database, conflict):
    barrier = Barrier(2)

    class RacingSession(Session):
        waited = False

        def scalar(self, statement, *args, **kwargs):
            value = super().scalar(statement, *args, **kwargs)
            if not self.waited and value is None and "customer_events" in str(statement):
                self.waited = True
                barrier.wait(timeout=10)
            return value

    def insert(index):
        with RacingSession(event_database) as session:
            try:
                item = EventIn(
                    profile_id="profile",
                    name="changed" if conflict and index else "created",
                    idempotency_key="same-key",
                )
                result, replayed = ingest_event(
                    item, {"tenant": "tenant", "permissions": ["contact.manage"]}, session
                )
                session.commit()
                return result.id, replayed
            except HTTPException as error:
                assert error.status_code == 409
                # The savepoint kept the surrounding transaction usable.
                other, _ = ingest_event(
                    EventIn(profile_id="profile", name="unrelated"),
                    {"tenant": "tenant", "permissions": ["contact.manage"]},
                    session,
                )
                session.commit()
                return "conflict", other.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(insert, [0, 1]))
    if conflict:
        assert sum(item[0] == "conflict" for item in outcomes) == 1
    else:
        assert outcomes[0][0] == outcomes[1][0]
        assert sorted(item[1] for item in outcomes) == [False, True]
    with Session(event_database) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(CustomerEvent)
                .where(CustomerEvent.idempotency_key == "same-key")
            )
            == 1
        )


def test_migration_twice_preserves_old_writer_default(event_database):
    migration = Path("migrations/2026090901_customer_event_ingestion.sql").read_text()
    with event_database.begin() as connection:
        # Model the parent schema before applying the additive migration.
        connection.execute(text("DROP TABLE customer_events"))
        connection.execute(
            text(
                "CREATE TABLE customer_events (id VARCHAR PRIMARY KEY, tenant_id VARCHAR, profile_id VARCHAR, name VARCHAR, properties_json TEXT, occurred_at TIMESTAMPTZ NOT NULL)"  # noqa: E501  # SQL fixture statement
            )
        )
        connection.exec_driver_sql(migration)
        connection.exec_driver_sql(migration)
        received = connection.execute(
            text(
                "INSERT INTO customer_events (id, tenant_id, profile_id, name, properties_json, occurred_at) VALUES ('old-writer','tenant','profile','old','{}',now()) RETURNING received_at"  # noqa: E501  # SQL fixture statement
            )
        ).scalar_one()
        assert received is not None
