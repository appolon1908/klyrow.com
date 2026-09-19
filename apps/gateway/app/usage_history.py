"""Tenant-local ledger aggregation; never calls providers, Odoo or telemetry."""
from __future__ import annotations

import base64
import binascii
from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .main import auth, db
from .billing import UsageEvent

router = APIRouter(tags=["Usage history"])
Granularity = Literal["day", "month"]


class UsageHistoryQuery(BaseModel):
    # In particular, never silently ignore project_id: this ledger currently
    # owns tenant identity only and cannot honestly apply a project filter.
    model_config = ConfigDict(extra="forbid")
    start: date | None = Field(default=None, alias="from", description="Inclusive UTC date; maximum window is 366 days.")
    end: date | None = Field(default=None, alias="to", description="Exclusive UTC date; defaults to tomorrow in UTC.")
    unit: str = Field(default="accepted_message", min_length=1, max_length=80)
    limit: int = Field(default=31, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=2048)


class UsageBucket(BaseModel):
    period_start: date
    quantity: int


class UsageHistoryPage(BaseModel):
    granularity: Granularity
    unit: str
    window_start: datetime
    window_end: datetime
    items: list[UsageBucket]
    next_cursor: str | None


class UsageCursor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    tenant_id: str
    granularity: Granularity
    unit: str
    start: date
    end: date
    after: date


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _decode_cursor(value: str) -> UsageCursor:
    try:
        raw = base64.b64decode(value, altchars=b"-_", validate=True)
        return UsageCursor.model_validate_json(raw)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(422, "invalid_cursor") from exc


def _window(query: UsageHistoryQuery, granularity: Granularity, tenant_id: str):
    cursor = _decode_cursor(query.cursor) if query.cursor is not None else None
    if cursor and (
        cursor.tenant_id != tenant_id or cursor.granularity != granularity
        or cursor.unit != query.unit
        or (query.start is not None and query.start != cursor.start)
        or (query.end is not None and query.end != cursor.end)
    ):
        raise HTTPException(422, "cursor_query_mismatch")
    try:
        end = query.end or (cursor.end if cursor else utc_today() + timedelta(days=1))
        start = query.start or (cursor.start if cursor else None)
        if start is None:
            if granularity == "day":
                start = end - timedelta(days=30)
            else:
                last_day = end - timedelta(days=1)
                month_index = last_day.year * 12 + last_day.month - 1 - 11
                year, month = divmod(month_index, 12)
                start = date(year, month + 1, 1)
        if not 0 < (end - start).days <= 366:
            raise ValueError("invalid window")
    except (ValueError, OverflowError) as exc:
        raise HTTPException(422, "invalid_usage_window") from exc
    if cursor:
        first_period = start if granularity == "day" else start.replace(day=1)
        if not first_period <= cursor.after < end or (granularity == "month" and cursor.after.day != 1):
            raise HTTPException(422, "invalid_cursor")
    return start, end, cursor


def usage_history(query: UsageHistoryQuery, granularity: Granularity, ctx: dict, session: Session) -> UsageHistoryPage:
    start, end, cursor = _window(query, granularity, ctx["tenant"])
    window_start = datetime.combine(start, time.min, tzinfo=timezone.utc)
    window_end = datetime.combine(end, time.min, tzinfo=timezone.utc)
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        # Explicit timezone is independent of the PostgreSQL session timezone.
        bucket = func.to_char(func.timezone("UTC", UsageEvent.occurred_at), "YYYY-MM-DD" if granularity == "day" else "YYYY-MM")
    elif dialect == "sqlite":
        bucket = func.strftime("%Y-%m-%d" if granularity == "day" else "%Y-%m", UsageEvent.occurred_at)
    else:
        raise HTTPException(503, "unsupported_usage_store")
    statement = select(bucket.label("period"), func.sum(UsageEvent.quantity).label("quantity")).where(
        UsageEvent.tenant_id == ctx["tenant"], UsageEvent.unit == query.unit,
        UsageEvent.occurred_at >= window_start, UsageEvent.occurred_at < window_end,
    )
    if cursor:
        statement = statement.where(bucket > cursor.after.isoformat()[:10 if granularity == "day" else 7])
    # Aggregate in SQL, never fetch a capped list of ledger rows and mistake
    # that partial list for the total. Return only periods with ledger entries.
    try:
        if dialect == "postgresql":
            # Transaction-local: a large tenant cannot run an unbounded report.
            session.execute(select(func.set_config("statement_timeout", "5000", True)))
        rows = session.execute(statement.group_by(bucket).order_by(bucket).limit(query.limit + 1)).all()
    except SQLAlchemyError as exc:
        raise HTTPException(503, "usage_store_unavailable") from exc
    items = [UsageBucket(period_start=date.fromisoformat(period if granularity == "day" else period + "-01"), quantity=int(quantity))
             for period, quantity in rows[:query.limit]]
    next_cursor = None
    if len(rows) > query.limit:
        state = UsageCursor(tenant_id=ctx["tenant"], granularity=granularity, unit=query.unit,
                            start=start, end=end, after=items[-1].period_start)
        next_cursor = base64.urlsafe_b64encode(state.model_dump_json().encode()).decode("ascii")
    return UsageHistoryPage(granularity=granularity, unit=query.unit, window_start=window_start,
                            window_end=window_end, items=items, next_cursor=next_cursor)


ERRORS = {
    401: {"description": "Authentication required"},
    403: {"description": "Tenant access denied or account suspended"},
    404: {"description": "Authenticated tenant resolution failed"},
    422: {"description": "Invalid date window, cursor, filter or pagination limit"},
    429: {"description": "Tenant request rate limit exceeded"},
    503: {"description": "Authorization or usage store unavailable"},
}


@router.get("/v1/usage/daily", response_model=UsageHistoryPage, responses=ERRORS,
            description="Daily UTC totals from the authoritative usage ledger. Periods without entries are omitted. Counts describe metered units, not recipient delivery.")
def daily_usage(query: Annotated[UsageHistoryQuery, Query()], ctx: dict = Depends(auth), session: Session = Depends(db)):
    return usage_history(query, "day", ctx, session)


@router.get("/v1/usage/monthly", response_model=UsageHistoryPage, responses=ERRORS,
            description="Monthly UTC totals within the requested date window. Boundary months may be partial. Periods without entries are omitted.")
def monthly_usage(query: Annotated[UsageHistoryQuery, Query()], ctx: dict = Depends(auth), session: Session = Depends(db)):
    return usage_history(query, "month", ctx, session)
