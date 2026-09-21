"""EVM USDC payment-request and chain-evidence authority.

The application never signs or broadcasts a blockchain transaction. A customer
submits a transaction hash after sending USDC externally. A read-only JSON-RPC
worker verifies chain ID, token contract, recipient, amount, block hash and
confirmation threshold before posting a canonical Klyrow Payment.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from .billing import Invoice
from .billing_config import BillingConfigError, _read_secret_file, load_billing_settings
from .billing_ledger import invoice_balance, post_settlement
from .main import Base, audit, scoped_idempotency_key, semantic_request_hash
from .payment_attempts import (
    ACTIVE_CHECKOUT_STATES,
    AUTHORIZED,
    CAPTURED,
    CREATED,
    FAILED,
    PENDING,
    REQUIRES_ACTION,
    PaymentAttempt,
    _apply_transition,
    _minor_from_decimal,
)

now = lambda: datetime.now(timezone.utc)
TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
PENDING_STATES = {"PENDING", "RETRY"}
LEASE_SECONDS = 60


class StablecoinError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class StablecoinPaymentRequest(Base):
    __tablename__ = "klyrow_stablecoin_payment_requests"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    invoice_id: Mapped[str] = mapped_column(String, index=True)
    payment_attempt_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    chain_id: Mapped[int] = mapped_column(Integer)
    token_contract: Mapped[str] = mapped_column(String)
    token_decimals: Mapped[int] = mapped_column(Integer)
    recipient_address: Mapped[str] = mapped_column(String)
    wallet_reference: Mapped[str] = mapped_column(String)
    amount_base_units: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="AWAITING_TRANSFER", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class StablecoinChainEvent(Base):
    __tablename__ = "klyrow_stablecoin_chain_events"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    invoice_id: Mapped[str] = mapped_column(String, index=True)
    payment_attempt_id: Mapped[str] = mapped_column(String, index=True)
    payment_request_id: Mapped[str] = mapped_column(String, index=True)
    chain_id: Mapped[int] = mapped_column(Integer)
    tx_hash: Mapped[str] = mapped_column(String)
    log_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    block_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    block_hash: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    token_contract: Mapped[str] = mapped_column(String)
    recipient_address: Mapped[str] = mapped_column(String)
    amount_base_units: Mapped[str] = mapped_column(String)
    confirmation_count: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String, default="PENDING", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("chain_id", "tx_hash", name="uq_klyrow_stablecoin_chain_tx"),)


@dataclass(frozen=True)
class StablecoinRequestResult:
    attempt_id: str
    request_id: str
    invoice_id: str
    chain_id: int
    token_contract: str
    token_decimals: int
    recipient_address: str
    wallet_reference: str
    amount_base_units: str
    amount: str
    currency: str
    expires_at: datetime


@dataclass(frozen=True)
class TransferEvidence:
    state: str
    chain_id: int
    tx_hash: str
    log_index: int | None = None
    block_number: int | None = None
    block_hash: str | None = None
    confirmations: int = 0
    evidence: dict | None = None


def _settings():
    try:
        settings = load_billing_settings()
    except BillingConfigError:
        raise HTTPException(503, "stablecoin_payment_disabled") from None
    stablecoin = settings.stablecoin
    if (
        not settings.enabled
        or not stablecoin.enabled
        or stablecoin.environment not in {"sandbox", "production"}
        or stablecoin.environment == "production"
        and (not settings.live_charging_enabled or not stablecoin.production_approved)
    ):
        raise HTTPException(503, "stablecoin_payment_disabled")
    return settings


def _amount_base_units(amount_minor: int, decimals: int) -> int:
    if decimals < 2:
        raise StablecoinError("stablecoin_decimals_invalid")
    return amount_minor * (10 ** (decimals - 2))


def create_or_resume_stablecoin_request(
    session: Session,
    *,
    tenant_id: str,
    actor_id: str,
    invoice_id: str,
    idempotency_key: str,
) -> StablecoinRequestResult:
    settings = _settings()
    stablecoin = settings.stablecoin
    storage_key = scoped_idempotency_key(
        {"tenant": tenant_id, "sub": actor_id},
        idempotency_key,
        action="billing.stablecoin.create",
        resource="stablecoin_payment_request",
    )
    fingerprint = semantic_request_hash(
        action="billing.stablecoin.create",
        resource="stablecoin_payment_request",
        payload={
            "tenant_id": tenant_id,
            "invoice_id": invoice_id,
            "provider": "stablecoin",
            "chain_id": stablecoin.chain_id,
            "token_contract": stablecoin.usdc_contract,
        },
    )
    invoice = session.scalar(
        select(Invoice).where(Invoice.id == invoice_id, Invoice.tenant_id == tenant_id).with_for_update()
    )
    if invoice is None:
        raise HTTPException(404, "invoice_not_found")
    if invoice.currency != "USD":
        raise HTTPException(409, "stablecoin_usd_invoice_required")

    existing = session.scalar(select(PaymentAttempt).where(
        PaymentAttempt.tenant_id == tenant_id,
        PaymentAttempt.idempotency_key == storage_key,
    ))
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise HTTPException(409, "idempotency_key_payload_mismatch")
        request = session.scalar(select(StablecoinPaymentRequest).where(
            StablecoinPaymentRequest.payment_attempt_id == existing.id,
            StablecoinPaymentRequest.tenant_id == tenant_id,
        ))
        if request is not None and existing.status in ACTIVE_CHECKOUT_STATES:
            return _request_result(existing, request)

    active = session.scalar(select(PaymentAttempt).where(
        PaymentAttempt.tenant_id == tenant_id,
        PaymentAttempt.invoice_id == invoice_id,
        PaymentAttempt.provider.in_(("stripe", "paypal", "stablecoin")),
        PaymentAttempt.status.in_(ACTIVE_CHECKOUT_STATES),
    ))
    if active is not None:
        raise HTTPException(409, "checkout_already_in_progress")

    balance = invoice_balance(session, invoice)
    if invoice.status in {"VOID", "CREDITED"} or balance.remaining_due <= 0:
        raise HTTPException(409, "invoice_not_payable")
    amount_minor = _minor_from_decimal(balance.remaining_due)
    amount_base_units = _amount_base_units(amount_minor, stablecoin.decimals)
    attempt = PaymentAttempt(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        invoice_id=invoice.id,
        customer_id=actor_id,
        provider="stablecoin",
        idempotency_key=storage_key,
        request_fingerprint=fingerprint,
        amount_minor=amount_minor,
        currency="USD",
        status=CREATED,
        created_by=actor_id,
    )
    request = StablecoinPaymentRequest(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        invoice_id=invoice.id,
        payment_attempt_id=attempt.id,
        chain_id=stablecoin.chain_id,
        token_contract=stablecoin.usdc_contract.lower(),
        token_decimals=stablecoin.decimals,
        recipient_address=stablecoin.receive_address.lower(),
        wallet_reference=stablecoin.wallet_reference,
        amount_base_units=str(amount_base_units),
        state="AWAITING_TRANSFER",
        expires_at=now() + timedelta(hours=24),
    )
    attempt.provider_attempt_reference = request.id
    session.add_all([attempt, request])
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "checkout_already_in_progress") from None
    _apply_transition(
        session, attempt, PENDING,
        event_type="billing.stablecoin.request_created",
        source="browser_bff", ctx={"sub": actor_id}, idempotency_key=storage_key,
    )
    _apply_transition(
        session, attempt, REQUIRES_ACTION,
        event_type="billing.stablecoin.transfer_required",
        source="stablecoin", ctx={"sub": actor_id},
        next_action_type="stablecoin_transfer",
        next_action_reference=request.id,
    )
    audit(session, {"tenant": tenant_id, "sub": actor_id}, "billing.stablecoin.request_created")
    session.commit()
    return _request_result(attempt, request)


def _request_result(attempt: PaymentAttempt, request: StablecoinPaymentRequest) -> StablecoinRequestResult:
    return StablecoinRequestResult(
        attempt_id=attempt.id,
        request_id=request.id,
        invoice_id=attempt.invoice_id,
        chain_id=request.chain_id,
        token_contract=request.token_contract,
        token_decimals=request.token_decimals,
        recipient_address=request.recipient_address,
        wallet_reference=request.wallet_reference,
        amount_base_units=request.amount_base_units,
        amount=f"{Decimal(attempt.amount_minor) / Decimal(100):.2f}",
        currency=attempt.currency,
        expires_at=request.expires_at,
    )


def submit_stablecoin_transaction(
    session: Session,
    *,
    tenant_id: str,
    actor_id: str,
    payment_attempt_id: str,
    tx_hash: str,
) -> tuple[StablecoinChainEvent, bool]:
    tx_hash = tx_hash.strip().lower()
    if not TX_HASH.fullmatch(tx_hash):
        raise HTTPException(422, "stablecoin_transaction_hash_invalid")
    attempt = session.scalar(select(PaymentAttempt).where(
        PaymentAttempt.id == payment_attempt_id,
        PaymentAttempt.tenant_id == tenant_id,
    ).with_for_update())
    if attempt is None or attempt.provider != "stablecoin":
        raise HTTPException(404, "payment_attempt_not_found")
    if attempt.status not in {PENDING, REQUIRES_ACTION, AUTHORIZED}:
        raise HTTPException(409, "payment_attempt_not_active")
    request = session.scalar(select(StablecoinPaymentRequest).where(
        StablecoinPaymentRequest.payment_attempt_id == attempt.id,
        StablecoinPaymentRequest.tenant_id == tenant_id,
    ))
    if request is None:
        raise HTTPException(409, "stablecoin_payment_request_missing")

    existing = session.scalar(select(StablecoinChainEvent).where(
        StablecoinChainEvent.chain_id == request.chain_id,
        StablecoinChainEvent.tx_hash == tx_hash,
    ))
    if existing is not None:
        if existing.payment_attempt_id != attempt.id or existing.tenant_id != tenant_id:
            raise HTTPException(409, "stablecoin_transaction_already_claimed")
        return existing, True

    event = StablecoinChainEvent(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        invoice_id=attempt.invoice_id,
        payment_attempt_id=attempt.id,
        payment_request_id=request.id,
        chain_id=request.chain_id,
        tx_hash=tx_hash,
        token_contract=request.token_contract,
        recipient_address=request.recipient_address,
        amount_base_units=request.amount_base_units,
        state="PENDING",
        evidence_json=json.dumps({"submitted_by": actor_id}, sort_keys=True, separators=(",", ":")),
    )
    session.add(event)
    request.state = "VERIFYING"
    audit(session, {"tenant": tenant_id, "sub": actor_id}, "billing.stablecoin.transaction_submitted")
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.scalar(select(StablecoinChainEvent).where(
            StablecoinChainEvent.chain_id == request.chain_id,
            StablecoinChainEvent.tx_hash == tx_hash,
        ))
        if existing and existing.payment_attempt_id == attempt.id and existing.tenant_id == tenant_id:
            return existing, True
        raise HTTPException(409, "stablecoin_transaction_already_claimed") from None
    return event, False


class EvmStablecoinReader:
    def __init__(self, *, rpc_url: str, api_token: str, chain_id: int, transport: httpx.BaseTransport | None = None):
        parsed = urlsplit(rpc_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise StablecoinError("stablecoin_rpc_url_invalid")
        self._rpc_url = rpc_url
        self._api_token = api_token
        self._chain_id = chain_id
        self._transport = transport
        self._counter = 0

    def _rpc(self, method: str, params: list):
        self._counter += 1
        headers = {"Content-Type": "application/json"}
        if self._api_token:
            headers["Authorization"] = "Bearer " + self._api_token
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=httpx.Timeout(10.0, connect=3.0),
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = client.post(
                    self._rpc_url,
                    json={"jsonrpc": "2.0", "id": self._counter, "method": method, "params": params},
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
        except (httpx.TimeoutException, httpx.TransportError):
            raise StablecoinError("stablecoin_rpc_ambiguous", retryable=True) from None
        except (httpx.HTTPError, ValueError, TypeError):
            raise StablecoinError("stablecoin_rpc_unavailable", retryable=True) from None
        if not isinstance(data, dict) or data.get("error") is not None or "result" not in data:
            raise StablecoinError("stablecoin_rpc_invalid_response", retryable=True)
        return data["result"]

    def inspect_transfer(
        self,
        *,
        tx_hash: str,
        token_contract: str,
        recipient_address: str,
        amount_base_units: int,
        confirmation_threshold: int,
        prior_block_hash: str | None = None,
    ) -> TransferEvidence:
        chain_hex = self._rpc("eth_chainId", [])
        try:
            chain_id = int(chain_hex, 16)
        except (TypeError, ValueError):
            raise StablecoinError("stablecoin_chain_id_invalid_response", retryable=True) from None
        if chain_id != self._chain_id:
            raise StablecoinError("stablecoin_network_mismatch")

        receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
        if receipt is None:
            if prior_block_hash:
                return TransferEvidence("REORGED", chain_id, tx_hash)
            return TransferEvidence("PENDING", chain_id, tx_hash)
        if not isinstance(receipt, dict):
            raise StablecoinError("stablecoin_receipt_invalid", retryable=True)
        if receipt.get("status") != "0x1":
            raise StablecoinError("stablecoin_transaction_failed")

        block_hash = receipt.get("blockHash")
        block_hex = receipt.get("blockNumber")
        if not isinstance(block_hash, str) or not isinstance(block_hex, str):
            raise StablecoinError("stablecoin_receipt_unmined", retryable=True)
        try:
            block_number = int(block_hex, 16)
        except ValueError:
            raise StablecoinError("stablecoin_receipt_invalid", retryable=True) from None
        if prior_block_hash and prior_block_hash.lower() != block_hash.lower():
            return TransferEvidence("REORGED", chain_id, tx_hash, block_number=block_number, block_hash=block_hash)

        expected_topic_to = "0x" + ("0" * 24) + recipient_address.lower().removeprefix("0x")
        matches = []
        logs = receipt.get("logs", [])
        if not isinstance(logs, list):
            raise StablecoinError("stablecoin_receipt_invalid", retryable=True)
        for log in logs:
            if not isinstance(log, dict) or str(log.get("address") or "").lower() != token_contract.lower():
                continue
            topics = log.get("topics", [])
            if not isinstance(topics, list) or len(topics) < 3:
                continue
            if str(topics[0]).lower() != TRANSFER_TOPIC or str(topics[2]).lower() != expected_topic_to:
                continue
            try:
                amount = int(str(log.get("data") or ""), 16)
                log_index = int(str(log.get("logIndex") or ""), 16)
            except ValueError:
                continue
            if amount == amount_base_units:
                matches.append((log_index, log))
        if len(matches) != 1:
            raise StablecoinError("stablecoin_transfer_log_mismatch")

        block = self._rpc("eth_getBlockByNumber", [block_hex, False])
        if not isinstance(block, dict) or str(block.get("hash") or "").lower() != block_hash.lower():
            return TransferEvidence("REORGED", chain_id, tx_hash, block_number=block_number, block_hash=block_hash)
        latest_hex = self._rpc("eth_blockNumber", [])
        try:
            latest = int(latest_hex, 16)
        except (TypeError, ValueError):
            raise StablecoinError("stablecoin_block_number_invalid", retryable=True) from None
        confirmations = max(0, latest - block_number + 1)
        state = "FINALIZED" if confirmations >= confirmation_threshold else "PENDING"
        return TransferEvidence(
            state, chain_id, tx_hash,
            log_index=matches[0][0],
            block_number=block_number,
            block_hash=block_hash,
            confirmations=confirmations,
            evidence={
                "chain_id": chain_id,
                "tx_hash": tx_hash,
                "block_number": block_number,
                "block_hash": block_hash,
                "log_index": matches[0][0],
                "confirmations": confirmations,
                "token_contract": token_contract.lower(),
                "recipient_address": recipient_address.lower(),
                "amount_base_units": str(amount_base_units),
            },
        )


def get_stablecoin_reader(*, settings=None, transport: httpx.BaseTransport | None = None) -> EvmStablecoinReader:
    try:
        settings = settings or load_billing_settings()
        stablecoin = settings.stablecoin
        if not settings.enabled or not stablecoin.enabled:
            raise BillingConfigError("stablecoin_disabled")
        if stablecoin.environment == "production" and (not settings.live_charging_enabled or not stablecoin.production_approved):
            raise BillingConfigError("stablecoin_production_not_approved")
        token = _read_secret_file("KLYROW_STABLECOIN_SECRET_FILE", None)
    except BillingConfigError as exc:
        raise StablecoinError("stablecoin_disabled") from exc
    return EvmStablecoinReader(
        rpc_url=stablecoin.api_base_url,
        api_token=token,
        chain_id=stablecoin.chain_id,
        transport=transport,
    )


def claim_stablecoin_events(session: Session, *, worker_id: str, limit: int = 20) -> list[StablecoinChainEvent]:
    cutoff = now() - timedelta(seconds=LEASE_SECONDS)
    stale = session.scalars(select(StablecoinChainEvent).where(
        StablecoinChainEvent.state == "PROCESSING",
        StablecoinChainEvent.claimed_at.is_not(None),
        StablecoinChainEvent.claimed_at <= cutoff,
    ).with_for_update(skip_locked=True)).all()
    for item in stale:
        item.state = "RETRY" if item.attempt_count < 8 else "FAILED"
        item.last_error_code = "stablecoin_claim_expired"
        item.claimed_at = None
        item.claimed_by = None
        item.next_retry_at = now() if item.state == "RETRY" else None

    rows = session.scalars(select(StablecoinChainEvent).where(
        StablecoinChainEvent.state.in_(tuple(PENDING_STATES)),
        StablecoinChainEvent.next_retry_at.is_(None) | (StablecoinChainEvent.next_retry_at <= now()),
    ).order_by(StablecoinChainEvent.created_at).with_for_update(skip_locked=True).limit(limit)).all()
    for item in rows:
        item.state = "PROCESSING"
        item.claimed_by = worker_id
        item.claimed_at = now()
        item.attempt_count += 1
        item.updated_at = now()
    return rows


def process_stablecoin_event(session: Session, event: StablecoinChainEvent, *, reader=None, settings=None) -> None:
    if event.state != "PROCESSING":
        return
    settings = settings or load_billing_settings()
    stablecoin = settings.stablecoin
    attempt = session.scalar(select(PaymentAttempt).where(
        PaymentAttempt.id == event.payment_attempt_id,
        PaymentAttempt.tenant_id == event.tenant_id,
    ).with_for_update())
    request = session.scalar(select(StablecoinPaymentRequest).where(
        StablecoinPaymentRequest.id == event.payment_request_id,
        StablecoinPaymentRequest.tenant_id == event.tenant_id,
    ).with_for_update())
    if attempt is None or request is None or attempt.provider != "stablecoin" or attempt.invoice_id != event.invoice_id:
        event.state = "FAILED"
        event.last_error_code = "stablecoin_correlation_mismatch"
        return
    if attempt.status == CAPTURED:
        event.state = "FINALIZED"
        event.updated_at = now()
        return
    if attempt.status not in {PENDING, REQUIRES_ACTION, AUTHORIZED}:
        event.state = "FAILED"
        event.last_error_code = "stablecoin_attempt_not_active"
        return

    reader = reader or get_stablecoin_reader(settings=settings)
    try:
        evidence = reader.inspect_transfer(
            tx_hash=event.tx_hash,
            token_contract=request.token_contract,
            recipient_address=request.recipient_address,
            amount_base_units=int(request.amount_base_units),
            confirmation_threshold=stablecoin.confirmation_threshold,
            prior_block_hash=event.block_hash,
        )
    except StablecoinError as exc:
        event.last_error_code = exc.code
        event.claimed_at = None
        event.claimed_by = None
        event.updated_at = now()
        if exc.retryable and event.attempt_count < 8:
            event.state = "RETRY"
            event.next_retry_at = now() + timedelta(seconds=min(900, 2 ** event.attempt_count))
        else:
            event.state = "FAILED"
            event.next_retry_at = None
            if attempt.status in {PENDING, REQUIRES_ACTION, AUTHORIZED} and not exc.retryable:
                _apply_transition(
                    session, attempt, FAILED,
                    event_type="payment_attempt.stablecoin_failed",
                    source="chain_verifier", ctx={"sub": "stablecoin-worker"},
                    provider_event_reference=event.tx_hash,
                    failure_code=exc.code, failure_message=exc.code,
                )
        return

    event.block_number = evidence.block_number
    event.block_hash = evidence.block_hash
    event.log_index = evidence.log_index
    event.confirmation_count = evidence.confirmations
    event.evidence_json = json.dumps(evidence.evidence or {}, sort_keys=True, separators=(",", ":"))
    event.claimed_at = None
    event.claimed_by = None
    event.updated_at = now()
    event.last_error_code = None

    if evidence.state == "REORGED":
        event.state = "REORGED"
        event.last_error_code = "stablecoin_reorg_detected"
        request.state = "REORGED"
        return
    if evidence.state == "PENDING":
        event.state = "PENDING"
        event.next_retry_at = now() + timedelta(seconds=30)
        if attempt.status == REQUIRES_ACTION:
            _apply_transition(
                session, attempt, AUTHORIZED,
                event_type="billing.stablecoin.transfer_observed",
                source="chain_verifier", ctx={"sub": "stablecoin-worker"},
                provider_event_reference=event.tx_hash,
            )
        return
    if evidence.state != "FINALIZED" or evidence.log_index is None:
        event.state = "FAILED"
        event.last_error_code = "stablecoin_evidence_invalid"
        return

    if attempt.status == REQUIRES_ACTION:
        _apply_transition(
            session, attempt, AUTHORIZED,
            event_type="billing.stablecoin.transfer_finalizing",
            source="chain_verifier", ctx={"sub": "stablecoin-worker"},
            provider_event_reference=event.tx_hash,
        )
        session.flush()
    payment = post_settlement(
        session,
        tenant_id=attempt.tenant_id,
        invoice_id=attempt.invoice_id,
        provider="stablecoin",
        provider_reference=f"{event.chain_id}:{event.tx_hash}:{evidence.log_index}",
        amount=Decimal(attempt.amount_minor) / Decimal(100),
        currency=attempt.currency,
        confirmed_by="stablecoin-chain-worker",
        payment_attempt_id=attempt.id,
    )
    _apply_transition(
        session, attempt, CAPTURED,
        event_type="payment_attempt.provider_captured",
        source="chain_verifier", ctx={"sub": "stablecoin-worker"},
        provider_event_reference=f"{event.tx_hash}:{evidence.log_index}",
    )
    event.state = "FINALIZED"
    event.next_retry_at = None
    request.state = "SETTLED"
    return payment


def process_stablecoin_chain_events(session: Session, *, worker_id: str = "billing-worker", limit: int = 20) -> int:
    rows = claim_stablecoin_events(session, worker_id=worker_id, limit=limit)
    session.commit()
    processed = 0
    for item in rows:
        item = session.get(StablecoinChainEvent, item.id)
        try:
            process_stablecoin_event(session, item)
            session.commit()
        except Exception:
            session.rollback()
            item = session.get(StablecoinChainEvent, item.id)
            if item:
                item.state = "RETRY" if item.attempt_count < 8 else "FAILED"
                item.last_error_code = "stablecoin_processing_transient_failure"
                item.claimed_at = None
                item.claimed_by = None
                item.next_retry_at = now() + timedelta(seconds=min(900, 2 ** item.attempt_count)) if item.state == "RETRY" else None
                item.updated_at = now()
                session.commit()
        processed += 1
    return processed
