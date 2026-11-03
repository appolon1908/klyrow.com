"""Disposable PostgreSQL regression for tenant-bound SES webmail projection."""

import os
import uuid

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker


@pytest.mark.skipif(
    not os.getenv("KLYROW_CONTRACT_POSTGRES_URL"), reason="Requires disposable PostgreSQL"
)
def test_ses_feedback_updates_only_matched_webmail_under_runtime_rls():
    from apps.gateway.app import main
    from apps.gateway.app.provider import ProviderEvent
    from apps.gateway.app.ses_feedback import SESFeedback, reconcile_feedback
    from apps.gateway.app.webmail_models import WebmailMailbox, WebmailMessage

    url = os.environ["KLYROW_CONTRACT_POSTGRES_URL"]
    suffix = uuid.uuid4().hex
    schema = "ses_feedback_" + suffix
    role = "ses_feedback_runtime_" + suffix
    admin = create_engine(url)
    owner = runtime = None
    schema_created = role_created = False
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True
            connection.execute(text(f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER NOBYPASSRLS'))
            role_created = True
            connection.execute(text(f'GRANT "{role}" TO CURRENT_USER'))
        owner = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
        # Only the actual models reached by reconciliation and webmail lookup.
        main.Base.metadata.create_all(
            owner,
            tables=[
                model.__table__
                for model in (
                    main.Tenant,
                    main.Message,
                    main.EmailOutbox,
                    main.Replay,
                    main.Event,
                    main.Audit,
                    main.Suppression,
                    WebmailMailbox,
                    WebmailMessage,
                    ProviderEvent,
                )
            ],
        )
        with Session(owner) as session:
            for tenant in ("tenant-a", "tenant-b"):
                session.add(main.Tenant(id=tenant, name="Synthetic", quota=0))
                session.add(
                    WebmailMailbox(
                        id="mailbox-" + tenant,
                        tenant_id=tenant,
                        domain_id="synthetic-domain",
                        address="noreply@codestra.agency",
                        display_name="Synthetic",
                    )
                )
            session.commit()
            session.add(
                main.Message(
                    id="local-message",
                    tenant_id="tenant-a",
                    sender="noreply@codestra.agency",
                    recipient="recipient@example.net",
                    subject="Synthetic",
                    status="provider_accepted",
                )
            )
            session.add(
                main.EmailOutbox(
                    id="outbox",
                    tenant_id="tenant-a",
                    message_id="local-message",
                    provider_message_id="synthetic-ses-provider-0001",
                    payload="{}",
                    state="delivered",
                )
            )
            for tenant, message_id in (
                ("tenant-a", "local-message"),
                ("tenant-b", "other-message"),
            ):
                session.add(
                    WebmailMessage(
                        id="webmail-" + tenant,
                        tenant_id=tenant,
                        mailbox_id="mailbox-" + tenant,
                        thread_id="thread-" + tenant,
                        outbound_message_id=message_id,
                        direction="OUTBOUND",
                        folder="SENT",
                        from_address="noreply@codestra.agency",
                        delivery_status="SUBMITTED",
                    )
                )
            session.commit()
        with owner.begin() as connection:
            connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"'))
            connection.execute(
                text(
                    f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{schema}" TO "{role}"'
                )
            )
            connection.execute(text("ALTER TABLE webmail_messages ENABLE ROW LEVEL SECURITY"))
            connection.execute(
                text(
                    "CREATE POLICY webmail_messages_tenant_isolation ON webmail_messages "
                    "USING (tenant_id = current_setting('app.tenant_id', true)) "
                    "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
                )
            )
        runtime = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})

        @event.listens_for(runtime, "connect")
        def use_runtime_role(dbapi_connection, _connection_record):
            with dbapi_connection.cursor() as cursor:
                cursor.execute(f'SET ROLE "{role}"')
            # Persist the connection role across transaction rollback/reset.
            dbapi_connection.commit()

        with runtime.connect() as connection:
            assert connection.execute(text("SELECT current_user")).scalar_one() == role
            assert (
                connection.execute(
                    text(
                        "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
                    )
                ).scalar_one()
                is False
            )
            assert (
                connection.execute(text("SELECT count(*) FROM webmail_messages")).scalar_one() == 0
            )
        feedback = SESFeedback(
            sns_message_id="synthetic-sns-event-0001",
            provider_message_id="synthetic-ses-provider-0001",
            event_type="Delivery",
            status="delivered",
            sender="noreply@codestra.agency",
            recipient="recipient@example.net",
            raw_status="",
            permanent_bounce=False,
        )
        factory = sessionmaker(runtime, expire_on_commit=False)
        assert reconcile_feedback(feedback, session_factory=factory) == "ack"
        with Session(owner) as session:
            assert session.get(main.Message, "local-message").status == "delivered"
            assert session.get(WebmailMessage, "webmail-tenant-a").delivery_status == "DELIVERED"
            assert session.get(WebmailMessage, "webmail-tenant-b").delivery_status == "SUBMITTED"
            assert session.query(main.Event).count() == 1
            assert session.query(main.Replay).count() == 1
            callback = session.query(ProviderEvent).one()
            assert (callback.tenant_id, callback.kind, callback.state) == (
                "tenant-a",
                "email.delivered",
                "PENDING",
            )
        assert reconcile_feedback(feedback, session_factory=factory) == "duplicate"
        with runtime.connect() as connection:
            assert (
                connection.execute(text("SELECT count(*) FROM webmail_messages")).scalar_one() == 0
            )
    finally:
        if runtime is not None:
            runtime.dispose()
        if owner is not None:
            owner.dispose()
        with admin.begin() as connection:
            if schema_created:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            if role_created:
                connection.execute(text(f'DROP ROLE "{role}"'))
        admin.dispose()
