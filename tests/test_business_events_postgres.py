"""Prove snapshot serialization and additive trace migration in PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from threading import Barrier
import uuid

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from apps.gateway.app.platform import app  # compose legacy module dependencies
from apps.gateway.app.business_events import BusinessEventOutbox, daily_snapshot
from apps.gateway.app.billing import UsageEvent
from apps.gateway.app.main import Tenant

pytestmark = pytest.mark.skipif(not os.getenv("KLYROW_CONTRACT_POSTGRES_URL"), reason="Requires disposable PostgreSQL")


@pytest.fixture
def database():
    url = os.environ["KLYROW_CONTRACT_POSTGRES_URL"]
    admin = create_engine(url)
    schema = "business_"+uuid.uuid4().hex
    with admin.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    for model in (Tenant, UsageEvent, BusinessEventOutbox):
        model.__table__.create(engine)
    with Session(engine) as s:
        s.add(Tenant(id="a", name="Synthetic"))
        s.commit()
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as c:
            c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_concurrent_snapshot_publishers_create_one_immutable_fact(database):
    barrier = Barrier(2)
    day = datetime.now(timezone.utc).date()-timedelta(days=1)
    def publish(_):
        with Session(database) as s:
            barrier.wait(timeout=10)
            result = daily_snapshot(s, "a", day)
            s.flush()
            result_id = result.id
            s.commit()
            return result_id
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(publish, range(2)))
    assert ids[0] == ids[1]
    with Session(database) as s:
        assert s.scalar(select(func.count()).select_from(BusinessEventOutbox)) == 1


def test_trace_migration_is_repeatable_and_preserves_old_writers(database):
    sql = (Path(__file__).parents[1]/"migrations/2026091301_outbox_trace_context.sql").read_text()
    with database.begin() as c:
        c.execute(text("CREATE TABLE email_outbox (id text PRIMARY KEY)"))
        c.exec_driver_sql(sql)
        c.exec_driver_sql(sql)
        assert c.execute(text("INSERT INTO email_outbox(id) VALUES('old-writer') RETURNING trace_context_json")).scalar_one() == "{}"
