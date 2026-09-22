import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.gateway.app.main import Base
from apps.gateway.app.billing import Invoice, Payment
from apps.gateway.app.payment_attempts import AUTHORIZED, CAPTURED, REQUIRES_ACTION
from apps.gateway.app.stablecoin_provider import (
    EvmStablecoinReader,
    StablecoinChainEvent,
    StablecoinError,
    StablecoinPaymentRequest,
    TransferEvidence,
    create_or_resume_stablecoin_request,
    process_stablecoin_event,
    submit_stablecoin_transaction,
)


TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
CONTRACT = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
RECIPIENT = "0x1111111111111111111111111111111111111111"
TX = "0x" + "a" * 64
BLOCK_HASH = "0x" + "b" * 64


def _settings():
    return SimpleNamespace(
        enabled=True,
        live_charging_enabled=False,
        stablecoin=SimpleNamespace(
            enabled=True,
            environment="sandbox",
            production_approved=False,
            chain_id=1,
            usdc_contract=CONTRACT,
            decimals=6,
            confirmation_threshold=12,
            network_allowlist=("1",),
            currency_allowlist=("USD",),
            api_base_url="https://rpc.example.test",
            receive_address=RECIPIENT,
            wallet_reference="openbao://billing/usdc-receiver",
        ),
    )


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine, Session(engine)


def _invoice(session):
    item = Invoice(
        id="invoice",
        number="INV-USDC",
        tenant_id="tenant",
        subscription_id="sub",
        currency="USD",
        subtotal=Decimal("10.00"),
        tax=Decimal("0.00"),
        discount=Decimal("0.00"),
        credits=Decimal("0.00"),
        total=Decimal("10.00"),
        status="OPEN",
        due_at=datetime.now(timezone.utc),
    )
    session.add(item)
    session.commit()
    return item


def test_stablecoin_payment_request_is_server_derived(monkeypatch):
    engine, session = _db()
    _invoice(session)
    monkeypatch.setattr("apps.gateway.app.stablecoin_provider.load_billing_settings", lambda: _settings())

    result = create_or_resume_stablecoin_request(
        session,
        tenant_id="tenant",
        actor_id="user",
        invoice_id="invoice",
        idempotency_key="stablecoin-request-1",
    )

    assert result.currency == "USD"
    assert result.amount == "10.00"
    assert result.amount_base_units == "10000000"
    assert result.chain_id == 1
    assert result.token_contract == CONTRACT
    assert result.recipient_address == RECIPIENT
    attempt = session.get(__import__("apps.gateway.app.payment_attempts", fromlist=["PaymentAttempt"]).PaymentAttempt, result.attempt_id)
    assert attempt.status == REQUIRES_ACTION
    assert attempt.provider == "stablecoin"
    session.close(); engine.dispose()


def test_stablecoin_transaction_submission_is_idempotent_and_tenant_scoped(monkeypatch):
    engine, session = _db()
    _invoice(session)
    monkeypatch.setattr("apps.gateway.app.stablecoin_provider.load_billing_settings", lambda: _settings())
    result = create_or_resume_stablecoin_request(
        session, tenant_id="tenant", actor_id="user", invoice_id="invoice", idempotency_key="stablecoin-request-1",
    )
    one, duplicate = submit_stablecoin_transaction(
        session, tenant_id="tenant", actor_id="user", payment_attempt_id=result.attempt_id, tx_hash=TX,
    )
    assert duplicate is False
    two, duplicate = submit_stablecoin_transaction(
        session, tenant_id="tenant", actor_id="user", payment_attempt_id=result.attempt_id, tx_hash=TX,
    )
    assert duplicate is True
    assert one.id == two.id
    session.close(); engine.dispose()


def test_evm_reader_accepts_only_exact_usdc_transfer_and_finality():
    to_topic = "0x" + ("0" * 24) + RECIPIENT.removeprefix("0x")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload["method"])
        if payload["method"] == "eth_chainId":
            result = "0x1"
        elif payload["method"] == "eth_getTransactionReceipt":
            result = {
                "status": "0x1",
                "blockHash": BLOCK_HASH,
                "blockNumber": "0x64",
                "logs": [{
                    "address": CONTRACT,
                    "topics": [TRANSFER_TOPIC, "0x" + "0" * 64, to_topic],
                    "data": hex(10_000_000),
                    "logIndex": "0x2",
                }],
            }
        elif payload["method"] == "eth_getBlockByNumber":
            result = {"hash": BLOCK_HASH}
        elif payload["method"] == "eth_blockNumber":
            result = hex(111)
        else:
            raise AssertionError(payload["method"])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    reader = EvmStablecoinReader(
        rpc_url="https://rpc.example.test",
        api_token="opaque-read-token",
        chain_id=1,
        transport=httpx.MockTransport(handler),
    )
    evidence = reader.inspect_transfer(
        tx_hash=TX,
        token_contract=CONTRACT,
        recipient_address=RECIPIENT,
        amount_base_units=10_000_000,
        confirmation_threshold=12,
    )
    assert evidence.state == "FINALIZED"
    assert evidence.confirmations == 12
    assert evidence.log_index == 2
    assert calls == ["eth_chainId", "eth_getTransactionReceipt", "eth_getBlockByNumber", "eth_blockNumber"]


def test_evm_reader_rejects_wrong_network_and_token():
    def network_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": "0x89"})

    reader = EvmStablecoinReader(
        rpc_url="https://rpc.example.test",
        api_token="opaque",
        chain_id=1,
        transport=httpx.MockTransport(network_handler),
    )
    with pytest.raises(StablecoinError, match="stablecoin_network_mismatch"):
        reader.inspect_transfer(
            tx_hash=TX,
            token_contract=CONTRACT,
            recipient_address=RECIPIENT,
            amount_base_units=10_000_000,
            confirmation_threshold=12,
        )


def test_stablecoin_settles_only_after_finalized_evidence(monkeypatch):
    engine, session = _db()
    _invoice(session)
    monkeypatch.setattr("apps.gateway.app.stablecoin_provider.load_billing_settings", lambda: _settings())
    result = create_or_resume_stablecoin_request(
        session, tenant_id="tenant", actor_id="user", invoice_id="invoice", idempotency_key="stablecoin-request-1",
    )
    event, _ = submit_stablecoin_transaction(
        session, tenant_id="tenant", actor_id="user", payment_attempt_id=result.attempt_id, tx_hash=TX,
    )
    event.state = "PROCESSING"
    session.commit()

    class PendingReader:
        def inspect_transfer(self, **kwargs):
            return TransferEvidence(
                "PENDING", 1, TX, log_index=2, block_number=100, block_hash=BLOCK_HASH,
                confirmations=3, evidence={"confirmations": 3},
            )

    process_stablecoin_event(session, event, reader=PendingReader(), settings=_settings())
    session.commit()
    attempt = session.get(__import__("apps.gateway.app.payment_attempts", fromlist=["PaymentAttempt"]).PaymentAttempt, result.attempt_id)
    assert attempt.status == AUTHORIZED
    assert session.query(Payment).count() == 0

    event.state = "PROCESSING"

    class FinalReader:
        def inspect_transfer(self, **kwargs):
            return TransferEvidence(
                "FINALIZED", 1, TX, log_index=2, block_number=100, block_hash=BLOCK_HASH,
                confirmations=12, evidence={"confirmations": 12},
            )

    process_stablecoin_event(session, event, reader=FinalReader(), settings=_settings())
    session.commit()
    attempt = session.get(__import__("apps.gateway.app.payment_attempts", fromlist=["PaymentAttempt"]).PaymentAttempt, result.attempt_id)
    assert attempt.status == CAPTURED
    payment = session.query(Payment).one()
    assert payment.provider == "stablecoin"
    assert payment.provider_reference == f"1:{TX}:2"
    assert session.get(Invoice, "invoice").status == "PAID"
    session.close(); engine.dispose()


def test_stablecoin_reorg_before_finality_never_posts_payment(monkeypatch):
    engine, session = _db()
    _invoice(session)
    monkeypatch.setattr("apps.gateway.app.stablecoin_provider.load_billing_settings", lambda: _settings())
    result = create_or_resume_stablecoin_request(
        session, tenant_id="tenant", actor_id="user", invoice_id="invoice", idempotency_key="stablecoin-request-1",
    )
    event, _ = submit_stablecoin_transaction(
        session, tenant_id="tenant", actor_id="user", payment_attempt_id=result.attempt_id, tx_hash=TX,
    )
    event.state = "PROCESSING"
    event.block_hash = BLOCK_HASH
    session.commit()

    class ReorgReader:
        def inspect_transfer(self, **kwargs):
            return TransferEvidence("REORGED", 1, TX)

    process_stablecoin_event(session, event, reader=ReorgReader(), settings=_settings())
    session.commit()
    assert event.state == "REORGED"
    assert event.last_error_code == "stablecoin_reorg_detected"
    assert session.query(Payment).count() == 0
    session.close(); engine.dispose()
