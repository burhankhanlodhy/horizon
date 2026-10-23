"""Team subscriptions and consolidated savings-fee billing.

Team subscriptions are only activated by the verified Stripe webhook path in
``billing.handle_event``. This module never grants access from a browser return
URL or a locally requested checkout.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import db
import stripe
from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from savings_ledger import FEE_ELIGIBLE_SAVINGS_USD

logger = logging.getLogger("contextshrink.team_billing")
ENABLED = os.environ.get("CONTEXTSHRINK_TEAMS_ENABLED", "false").strip().lower() in {
    "1", "true", "yes", "on"
}
APP_URL = os.environ.get("APP_URL", "https://app.contextshrink.com").rstrip("/")
TEAM_PORTAL_CONFIGURATION_ID = os.environ.get("STRIPE_TEAM_PORTAL_CONFIGURATION_ID")
TEAM_PRICE_LOOKUP_KEY = os.environ.get(
    "STRIPE_TEAM_PRICE_LOOKUP_KEY", "contextshrink_team_seat_monthly"
)
SEAT_PRICE_USD = Decimal("5.00")
MIN_SEATS = 3
FEE_RATE = Decimal("0.05")
FEE_THRESHOLD = Decimal("20.00")
# Access rules match ContextShrink Code (code_plan.py): active and trialing are
# entitled; past_due keeps access for this long after the period end.
TEAM_ENTITLED_STATUSES = {"active", "trialing"}
TEAM_PAST_DUE_GRACE = timedelta(days=3)
_client = stripe.StripeClient(os.environ.get("STRIPE_SECRET_KEY", ""), max_network_retries=2) if os.environ.get("STRIPE_SECRET_KEY") else None
_price_id_cache: str | None = None


def money(value) -> Decimal:
    return Decimal(value or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def savings_fee(savings) -> Decimal:
    amount = money(savings)
    return money(amount * FEE_RATE) if amount > FEE_THRESHOLD else Decimal("0.00")


def _stripe():
    if _client is None:
        raise HTTPException(503, "Billing is not configured")
    return _client


async def _call(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


async def _price_id() -> str:
    global _price_id_cache
    if _price_id_cache is None:
        prices = await _call(
            _stripe().v1.prices.list,
            {"lookup_keys": [TEAM_PRICE_LOOKUP_KEY], "active": True, "limit": 1},
        )
        if not prices.data:
            raise HTTPException(503, "Team billing price is not set up")
        _price_id_cache = prices.data[0].id
    return _price_id_cache


async def _team_portal_configuration_id() -> str:
    if TEAM_PORTAL_CONFIGURATION_ID:
        return TEAM_PORTAL_CONFIGURATION_ID
    result = await _call(_stripe().v1.billing_portal.configurations.list, {"limit": 100, "active": True})
    for config in result.data:
        metadata = config.metadata.to_dict() if config.metadata else {}
        if metadata.get("contextshrink_scope") == "team":
            return config.id
    raise HTTPException(503, "Team billing portal is not set up")


def _expandable_id(value):
    return value if isinstance(value, str) else getattr(value, "id", None)


async def _schedule_seat_reduction(subscription_id: str, target: int, price_id: str):
    """Keep current seats through renewal, then change the recurring quantity."""
    sub = await _call(_stripe().v1.subscriptions.retrieve, subscription_id)
    current_items = [
        {"price": item.price.id, "quantity": int(item.quantity or 1)}
        for item in sub.items.data
    ]
    team_item = next((item for item in sub.items.data if item.price.id == price_id), None)
    if team_item is None:
        raise HTTPException(409, "Team seat price is missing from the subscription")
    new_items = [
        {"price": item.price.id, "quantity": target if item.id == team_item.id else int(item.quantity or 1)}
        for item in sub.items.data
    ]
    end_ts = int(team_item.current_period_end)
    schedule_id = _expandable_id(getattr(sub, "schedule", None))
    if schedule_id:
        schedule = await _call(_stripe().v1.subscription_schedules.retrieve, schedule_id)
        phase = schedule.phases[0]
        start_ts = int(phase.start_date)
    else:
        schedule = await _call(
            _stripe().v1.subscription_schedules.create,
            {"from_subscription": subscription_id},
            {"idempotency_key": f"team-seat-schedule-{subscription_id}-{target}-{end_ts}"},
        )
        schedule_id = schedule.id
        phase = schedule.phases[0]
        start_ts = int(phase.start_date)
        # Prefer the API's normalized current phase, which preserves any
        # additional subscription items and quantities.
        current_items = [
            {"price": _expandable_id(item.price), "quantity": int(item.quantity or 1)}
            for item in phase.items
        ]
    await _call(
        _stripe().v1.subscription_schedules.update,
        schedule_id,
        {
            "end_behavior": "release",
            "phases": [
                {
                    "start_date": start_ts,
                    "end_date": end_ts,
                    "items": current_items,
                    "proration_behavior": "none",
                },
                {
                    "start_date": end_ts,
                    "items": new_items,
                    "proration_behavior": "none",
                },
            ],
        },
        {"idempotency_key": f"team-seat-schedule-update-{subscription_id}-{target}-{end_ts}"},
    )
    return sub


async def _team_row(team_id):
    return await db._conn().fetchrow(
        "SELECT t.id AS team_id, t.name, t.created_by, s.seat_count, s.status, "
        "s.stripe_customer_id, s.stripe_subscription_id, s.current_period_start, "
        "s.current_period_end, s.cancel_at_period_end, s.pending_seat_count "
        "FROM core.teams t LEFT JOIN core.team_subscriptions s ON s.team_id=t.id "
        "WHERE t.id=$1",
        team_id,
    )


async def entitlement(team_id) -> bool:
    """Whether a Team has verified paid access right now.

    Active and trialing subscriptions are entitled. A past_due subscription stays
    entitled until three days after its period end, as ContextShrink Code does.
    An overdue savings fee removes access in every case.
    """
    row = await db._conn().fetchrow(
        "SELECT s.status, s.current_period_end, EXISTS(SELECT 1 FROM billing.team_cycles c "
        "WHERE c.team_id=s.team_id AND c.payment_status IN ('failed','void') "
        "AND c.payment_failed_at < now() - ($2::int * interval '1 day')) AS overdue "
        "FROM core.team_subscriptions s WHERE s.team_id=$1",
        team_id,
        db.FEE_GRACE_DAYS,
    )
    if row is None or row["overdue"]:
        return False
    if row["status"] in TEAM_ENTITLED_STATUSES:
        return True
    period_end = row["current_period_end"]
    return (
        row["status"] == "past_due"
        and period_end is not None
        and datetime.now(timezone.utc) < period_end + TEAM_PAST_DUE_GRACE
    )


async def estimate(team_id, user=None, *, viewer_membership_id=None, viewer_role=None) -> dict:
    """Estimate current Team month using only eligible, attributed ledger rows."""
    if user is not None:
        if isinstance(user, dict):
            viewer_membership_id = viewer_membership_id or user.get("membership_id")
            viewer_role = viewer_role or user.get("team_role") or user.get("role")
        else:
            viewer_membership_id = viewer_membership_id or getattr(user, "membership_id", None)
            viewer_role = viewer_role or getattr(user, "team_role", None) or getattr(user, "role", None)
    row = await _team_row(team_id)
    if row is None:
        raise HTTPException(404, "Team not found")
    start, end = row["current_period_start"], row["current_period_end"]
    if start is None or end is None:
        period = await db._conn().fetchrow(
            "SELECT date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS start, "
            "(date_trunc('month', now() AT TIME ZONE 'UTC') + interval '1 month') AT TIME ZONE 'UTC' AS end"
        )
        start, end = period["start"], period["end"]
    savings = await db._conn().fetchval(
        f"SELECT {FEE_ELIGIBLE_SAVINGS_USD} FROM metrics.proxy_events "
        "WHERE team_id=$1 AND occurred_at >= $2 AND occurred_at < $3",
        team_id,
        start,
        end,
    )
    breakdown = await db._conn().fetch(
        f"SELECT e.membership_id,e.user_id,u.name,m.role,m.left_at,{FEE_ELIGIBLE_SAVINGS_USD} AS savings_usd "
        "FROM metrics.proxy_events e LEFT JOIN core.team_memberships m ON m.id=e.membership_id "
        "LEFT JOIN core.users u ON u.id=e.user_id "
        "WHERE e.team_id=$1 AND e.occurred_at >= $2 AND e.occurred_at < $3 "
        "GROUP BY e.membership_id,e.user_id,u.name,m.role,m.left_at",
        team_id,
        start,
        end,
    )
    privileged = viewer_role in {"owner", "admin"}
    members = []
    for item in breakdown:
        if item["membership_id"] is None:
            if privileged:
                members.append({"membership_id": None, "user_id": None, "name": "Service usage", "role": None, "savings_usd": float(money(item["savings_usd"])), "removed": False})
            continue
        if not privileged and str(item["membership_id"]) != str(viewer_membership_id):
            continue
        members.append({
            "membership_id": str(item["membership_id"]),
            "user_id": str(item["user_id"]) if item["user_id"] else None,
            "name": item["name"] or "Former member",
            "role": item["role"],
            "savings_usd": float(money(item["savings_usd"])),
            "removed": item["left_at"] is not None,
        })
    seats = int(row["seat_count"] or MIN_SEATS)
    seats_usd = money(SEAT_PRICE_USD * seats)
    savings_usd = money(savings)
    fee_usd = savings_fee(savings_usd)
    if not privileged:
        own_savings = sum(
            (Decimal(str(item["savings_usd"])) for item in members),
            Decimal("0.00"),
        )
        savings_usd = money(own_savings)
        seats_usd = None
        fee_usd = None
    return {
        "team_id": str(team_id),
        "seat_count": seats,
        "seat_price_usd": float(SEAT_PRICE_USD),
        "seats_usd": float(seats_usd) if privileged else None,
        "savings_usd": float(savings_usd),
        "savings_fee_usd": float(fee_usd) if privileged else None,
        "total_usd": float(seats_usd + fee_usd) if privileged else None,
        "members": members,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
    }


async def _team_for_customer(customer_id: str, metadata_team_id: str | None = None):
    conn = db._conn()
    team_id = await conn.fetchval(
        "SELECT team_id FROM core.team_subscriptions WHERE stripe_customer_id=$1",
        customer_id,
    )
    if team_id is None and metadata_team_id:
        team_id = await conn.fetchval(
            "UPDATE core.team_subscriptions SET stripe_customer_id=$2, updated_at=now() "
            "WHERE team_id=$1::uuid AND stripe_customer_id IS NULL RETURNING team_id",
            metadata_team_id,
            customer_id,
        )
    return team_id


async def sync_subscription(subscription_id: str) -> None:
    """Sync a Team subscription from Stripe's authoritative current state."""
    sub = await _call(_stripe().v1.subscriptions.retrieve, subscription_id)
    customer = sub.customer if isinstance(sub.customer, str) else sub.customer.id
    metadata = sub.metadata.to_dict() if sub.metadata else {}
    team_id = await _team_for_customer(customer, metadata.get("team_id"))
    if team_id is None:
        return
    status = "canceled" if sub.status in {"canceled", "incomplete_expired"} else sub.status
    item = next((i for i in sub.items.data if getattr(i.price, "lookup_key", None) == TEAM_PRICE_LOOKUP_KEY), None)
    if item is None:
        item_price = await _price_id()
        item = next((i for i in sub.items.data if i.price.id == item_price), None)
    if item is None:
        logger.warning("Team subscription %s has no recognized seat price", subscription_id)
        return
    pstart = datetime.fromtimestamp(item.current_period_start, timezone.utc)
    pend = datetime.fromtimestamp(item.current_period_end, timezone.utc)
    duplicate_subscription = False
    async with db._conn().acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT stripe_subscription_id,status FROM core.team_subscriptions WHERE team_id=$1 FOR UPDATE",
                team_id,
            )
            existing_id = existing["stripe_subscription_id"] if existing else None
            existing_status = existing["status"] if existing else None
            duplicate_subscription = bool(
                existing_id
                and existing_id != subscription_id
                and existing_status not in {"canceled", "incomplete_expired"}
            )
            if not duplicate_subscription:
                await conn.execute(
                    "INSERT INTO core.team_subscriptions(team_id,seat_count,status,stripe_customer_id,"
                    "stripe_subscription_id,current_period_start,current_period_end,cancel_at_period_end,updated_at) "
                    "VALUES($1,$2,$3,$4,$5,$6,$7,$8,now()) ON CONFLICT(team_id) DO UPDATE SET "
                    "seat_count=EXCLUDED.seat_count,status=EXCLUDED.status,stripe_customer_id=EXCLUDED.stripe_customer_id,"
                    "stripe_subscription_id=EXCLUDED.stripe_subscription_id,current_period_start=EXCLUDED.current_period_start,"
                    "current_period_end=EXCLUDED.current_period_end,cancel_at_period_end=EXCLUDED.cancel_at_period_end,"
                    "pending_seat_count=CASE WHEN EXCLUDED.status='canceled' OR core.team_subscriptions.pending_seat_count=EXCLUDED.seat_count THEN NULL "
                    "ELSE core.team_subscriptions.pending_seat_count END,updated_at=now()",
                    team_id,
                    int(item.quantity),
                    status,
                    customer,
                    subscription_id,
                    pstart,
                    pend,
                    bool(sub.cancel_at_period_end or sub.cancel_at),
                )
    if duplicate_subscription:
        logger.error(
            "Team %s already has Stripe subscription %s; cancel duplicate %s",
            team_id,
            existing_id,
            subscription_id,
        )
        if sub.status not in {"canceled", "incomplete_expired"}:
            await _call(
                _stripe().v1.subscriptions.cancel,
                subscription_id,
                {"prorate": False, "cancellation_details": {"comment": "Duplicate ContextShrink Team subscription"}},
                {"idempotency_key": f"team-duplicate-subscription-{subscription_id}"},
            )


async def _handle_cycle_invoice(invoice, subscription_id: str) -> None:
    """Freeze the completed Team cycle and attach its fee to this draft invoice."""
    customer_id = invoice.customer if isinstance(invoice.customer, str) else invoice.customer.id
    team_id = await _team_for_customer(customer_id)
    if team_id is None:
        return
    period_start = datetime.fromtimestamp(invoice.period_start, timezone.utc)
    period_end = datetime.fromtimestamp(invoice.period_end, timezone.utc)
    key = f"team-cycle-{team_id}-{int(period_start.timestamp())}"
    async with db._conn().acquire() as conn:
        locked = await conn.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1,0))", key)
        if not locked:
            raise HTTPException(503, "This Team billing period is being processed. Please retry.")
        try:
            # The snapshot and all member rows commit atomically before Stripe
            # is called, so a crash cannot leave a partial frozen snapshot.
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO billing.team_cycles(team_id,period_start,period_end,savings_usd,fee_cents,"
                    "stripe_invoice_id,payment_status,reconciliation_state,created_at,updated_at) "
                    "SELECT $1,$2,$3,0,0,$4,'pending','snapshot_pending',now(),now() "
                    "ON CONFLICT(team_id,period_start) DO NOTHING",
                    team_id, period_start, period_end, invoice.id,
                )
                cycle = await conn.fetchrow(
                    "SELECT savings_usd,fee_cents,stripe_invoice_id,reconciliation_state FROM billing.team_cycles "
                    "WHERE team_id=$1 AND period_start=$2 FOR UPDATE",
                    team_id,
                    period_start,
                )
                if cycle["stripe_invoice_id"] and cycle["stripe_invoice_id"] != invoice.id:
                    raise HTTPException(502, "Team billing cycle is linked to another Stripe invoice")
                if cycle["reconciliation_state"] == "snapshot_pending":
                    await _freeze_cycle(conn, team_id, period_start, period_end)
                    cycle = await conn.fetchrow(
                        "SELECT savings_usd,fee_cents,stripe_invoice_id FROM billing.team_cycles WHERE team_id=$1 AND period_start=$2",
                        team_id,
                        period_start,
                    )
                fee_cents = int(cycle["fee_cents"] or 0)
            if fee_cents:
                items = await _call(
                    _stripe().v1.invoice_items.list,
                    {"invoice": invoice.id, "limit": 100},
                )
                matching = [i for i in items.data if (i.metadata or {}).get("team_cycle_key") == key]
                if not matching:
                    await _call(
                        _stripe().v1.invoice_items.create,
                        {
                            "customer": customer_id,
                            "invoice": invoice.id,
                            "currency": "usd",
                            "amount": fee_cents,
                            "description": f"Team savings fee, {period_start:%Y-%m-%d} to {period_end:%Y-%m-%d}",
                            "metadata": {"team_id": str(team_id), "team_cycle_key": key},
                        },
                        {"idempotency_key": key + "-fee-item"},
                    )
            async with conn.transaction():
                await conn.execute(
                    "UPDATE billing.team_cycles SET stripe_invoice_id=$3,reconciliation_state='attached',updated_at=now() "
                    "WHERE team_id=$1 AND period_start=$2",
                    team_id,
                    period_start,
                    invoice.id,
                )
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1,0))", key)


async def _freeze_cycle(conn, team_id, start, end):
    rows = await conn.fetch(
        f"SELECT membership_id,user_id,{FEE_ELIGIBLE_SAVINGS_USD} AS savings_usd FROM metrics.proxy_events "
        "WHERE team_id=$1 AND occurred_at >= $2 AND occurred_at < $3 "
        "GROUP BY membership_id,user_id",
        team_id,
        start,
        end,
    )
    total, member_sums, service_savings = aggregate_cycle_rows(rows)
    fee_cents = int(savings_fee(total) * 100)
    await conn.execute(
        "UPDATE billing.team_cycles SET savings_usd=$3,fee_cents=$4,service_savings_usd=$5, "
        "reconciliation_state='frozen',updated_at=now() "
        "WHERE team_id=$1 AND period_start=$2",
        team_id,
        start,
        total,
        fee_cents,
        service_savings,
    )
    for membership_id, user_id, value in member_sums.values():
        await conn.execute(
            "INSERT INTO billing.team_cycle_members(team_id,period_start,membership_id,user_id,savings_usd) "
            "VALUES($1,$2,$3,$4,$5) ON CONFLICT(team_id,period_start,membership_id) "
            "DO UPDATE SET user_id=EXCLUDED.user_id,savings_usd=EXCLUDED.savings_usd",
            team_id,
            start,
            membership_id,
            user_id,
            money(value),
        )


async def _settle_final_cycle(team_id, subscription, customer_id: str) -> None:
    """Collect the final Team savings fee on cancellation without seat charges."""
    items = getattr(getattr(subscription, "items", None), "data", []) or []
    team_item = next(
        (item for item in items if getattr(item.price, "lookup_key", None) == TEAM_PRICE_LOOKUP_KEY),
        items[0] if items else None,
    )
    if team_item is None or not getattr(team_item, "current_period_start", None):
        return
    start = datetime.fromtimestamp(team_item.current_period_start, timezone.utc)
    ended_at = getattr(subscription, "ended_at", None) or getattr(subscription, "canceled_at", None)
    end = datetime.fromtimestamp(ended_at, timezone.utc) if ended_at else datetime.now(timezone.utc)
    if end <= start:
        return
    key = f"team-final-{team_id}-{int(start.timestamp())}"
    async with db._conn().acquire() as conn:
        locked = await conn.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1,0))", key)
        if not locked:
            raise HTTPException(503, "Final Team billing cycle is being processed")
        try:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO billing.team_cycles(team_id,period_start,period_end,savings_usd,fee_cents,"
                    "stripe_invoice_id,payment_status,reconciliation_state,created_at,updated_at) "
                    "VALUES($1,$2,$3,0,0,NULL,'pending','snapshot_pending',now(),now()) "
                    "ON CONFLICT(team_id,period_start) DO NOTHING",
                    team_id,
                    start,
                    end,
                )
                cycle = await conn.fetchrow(
                    "SELECT savings_usd,fee_cents,stripe_invoice_id,reconciliation_state FROM billing.team_cycles "
                    "WHERE team_id=$1 AND period_start=$2 FOR UPDATE",
                    team_id,
                    start,
                )
                if cycle["reconciliation_state"] == "snapshot_pending":
                    await _freeze_cycle(conn, team_id, start, end)
                    cycle = await conn.fetchrow(
                        "SELECT savings_usd,fee_cents,stripe_invoice_id,reconciliation_state FROM billing.team_cycles "
                        "WHERE team_id=$1 AND period_start=$2",
                        team_id,
                        start,
                    )
            fee_cents = int(cycle["fee_cents"] or 0)
            if fee_cents == 0:
                await conn.execute(
                    "UPDATE billing.team_cycles SET payment_status='paid',reconciliation_state='settled',updated_at=now() "
                    "WHERE team_id=$1 AND period_start=$2",
                    team_id,
                    start,
                )
                return
            invoice_id = cycle["stripe_invoice_id"]
            if not invoice_id:
                invoice = await _call(
                    _stripe().v1.invoices.create,
                    {
                        "customer": customer_id,
                        "currency": "usd",
                        "collection_method": "charge_automatically",
                        "auto_advance": False,
                        "description": f"Final Team savings fee, {start:%Y-%m-%d} to {end:%Y-%m-%d}",
                        "metadata": {"team_id": str(team_id), "team_cycle_key": key},
                    },
                    {"idempotency_key": key + "-invoice"},
                )
                invoice_id = invoice.id
                await conn.execute(
                    "UPDATE billing.team_cycles SET stripe_invoice_id=$3,updated_at=now() "
                    "WHERE team_id=$1 AND period_start=$2",
                    team_id,
                    start,
                    invoice_id,
                )
            existing_items = await _call(_stripe().v1.invoice_items.list, {"invoice": invoice_id, "limit": 100})
            if not any((item.metadata or {}).get("team_cycle_key") == key for item in existing_items.data):
                await _call(
                    _stripe().v1.invoice_items.create,
                    {
                        "customer": customer_id,
                        "invoice": invoice_id,
                        "currency": "usd",
                        "amount": fee_cents,
                        "description": f"Final Team savings fee, {start:%Y-%m-%d} to {end:%Y-%m-%d}",
                        "metadata": {"team_id": str(team_id), "team_cycle_key": key},
                    },
                    {"idempotency_key": key + "-fee-item"},
                )
            await _call(_stripe().v1.invoices.finalize_invoice, invoice_id, {}, {"idempotency_key": key + "-finalize"})
            await _call(
                _stripe().v1.invoices.pay,
                invoice_id,
                {"off_session": True},
                {"idempotency_key": key + "-pay"},
            )
            await conn.execute(
                "UPDATE billing.team_cycles SET reconciliation_state='attached',updated_at=now() "
                "WHERE team_id=$1 AND period_start=$2",
                team_id,
                start,
            )
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1,0))", key)


def aggregate_cycle_rows(rows):
    """Sum eligible rows, keeping shared service usage in the total only."""
    member_sums: dict[str, tuple[object, object, Decimal]] = {}
    total = Decimal("0")
    service_savings = Decimal("0")
    for row in rows:
        value = Decimal(row["savings_usd"] or 0)
        total += value
        if row["membership_id"] is not None:
            key = str(row["membership_id"])
            prior = member_sums.get(key)
            member_sums[key] = (
                row["membership_id"],
                row["user_id"],
                value + (prior[2] if prior else Decimal("0")),
            )
        else:
            service_savings += value
    return money(total), member_sums, money(service_savings)


async def _adjustment_item_exists(invoice_id: str, key: str) -> bool:
    items = await _call(_stripe().v1.invoice_items.list, {"invoice": invoice_id, "limit": 100})
    return any((item.metadata or {}).get("team_cycle_key") == key for item in items.data)


async def _create_adjustment_item(team_id, start, key: str, cents: int, invoice_id: str, customer_id: str) -> None:
    # The Stripe idempotency key includes the invoice: a retry against a different
    # invoice must not reuse a key whose parameters (the invoice) have changed.
    await _call(
        _stripe().v1.invoice_items.create,
        {
            "customer": customer_id,
            "invoice": invoice_id,
            "currency": "usd",
            "amount": cents,
            "description": f"Team savings fee adjustment for {start:%Y-%m-%d} cycle",
            "metadata": {"team_id": str(team_id), "team_cycle_key": key},
        },
        {"idempotency_key": f"{key}:{invoice_id}"},
    )


async def _attach_adjustment(conn, team_id, start, key: str, cents: int, live) -> None:
    """Record an adjustment as applied. The intent and the cycle commit together."""
    async with conn.transaction():
        await conn.execute(
            "UPDATE billing.team_fee_adjustments SET status='attached' WHERE adjustment_key=$1",
            key,
        )
        await conn.execute(
            "UPDATE billing.team_cycles SET fee_adjustment_cents=fee_adjustment_cents+$3,late_savings_usd=$4, "
            "reconciliation_state='settled',updated_at=now() WHERE team_id=$1 AND period_start=$2",
            team_id,
            start,
            cents,
            money(live),
        )


async def _settle_pending_adjustment(conn, team_id, start, intent, live, current_invoice_id, customer_id) -> int:
    """Apply an intent an earlier run recorded but never confirmed. Returns its cents.

    The intent's recorded invoice is checked first. If Stripe already holds the
    item there, the earlier run did charge it and only the bookkeeping was lost,
    so nothing is charged again. Otherwise the item goes onto the current invoice.
    """
    key, cents, recorded = intent["adjustment_key"], int(intent["cents"]), intent["stripe_invoice_id"]
    if recorded and await _adjustment_item_exists(recorded, key):
        await _attach_adjustment(conn, team_id, start, key, cents, live)
        return cents
    if recorded != current_invoice_id:
        await conn.execute(
            "UPDATE billing.team_fee_adjustments SET stripe_invoice_id=$2 WHERE adjustment_key=$1",
            key,
            current_invoice_id,
        )
    if not await _adjustment_item_exists(current_invoice_id, key):
        await _create_adjustment_item(team_id, start, key, cents, current_invoice_id, customer_id)
    await _attach_adjustment(conn, team_id, start, key, cents, live)
    return cents


async def _reconcile_cycle(conn, team_id, row, current_invoice_id: str, customer_id: str) -> None:
    start, end = row["period_start"], row["period_end"]
    live = money(
        await conn.fetchval(
            f"SELECT {FEE_ELIGIBLE_SAVINGS_USD} FROM metrics.proxy_events WHERE team_id=$1 AND occurred_at >= $2 AND occurred_at < $3",
            team_id,
            start,
            end,
        )
    )
    # Pending intents from earlier runs settle before any new delta is computed,
    # so a delta is never charged a second time under a different key.
    pending = await conn.fetch(
        "SELECT adjustment_key,cents,stripe_invoice_id FROM billing.team_fee_adjustments "
        "WHERE team_id=$1 AND period_start=$2 AND status='pending' ORDER BY created_at,adjustment_key",
        team_id,
        start,
    )
    applied = 0
    for intent in pending:
        applied += await _settle_pending_adjustment(
            conn, team_id, start, dict(intent), live, current_invoice_id, customer_id
        )
    target = int(savings_fee(live) * 100)
    already_charged = int(row["fee_cents"] or 0) + int(row["fee_adjustment_cents"] or 0) + applied
    delta = target - already_charged
    if not delta:
        await conn.execute(
            "UPDATE billing.team_cycles SET reconciliation_state='settled',late_savings_usd=$3,updated_at=now() "
            "WHERE team_id=$1 AND period_start=$2",
            team_id,
            start,
            live,
        )
        return
    # The key depends on the cycle and target, not on the invoice, so a later run
    # finds this intent again. The sequence keeps keys unique when a total moves
    # back to an earlier value.
    sequence = int(
        await conn.fetchval(
            "SELECT count(*) FROM billing.team_fee_adjustments WHERE team_id=$1 AND period_start=$2",
            team_id,
            start,
        )
        or 0
    )
    key = f"team-fee-adj-{team_id}-{int(start.timestamp())}-{target}-{sequence}"
    # The intent commits before Stripe is called. No transaction stays open
    # across a Stripe call.
    await conn.execute(
        "INSERT INTO billing.team_fee_adjustments(adjustment_key,team_id,period_start,cents,stripe_invoice_id,status,created_at) "
        "VALUES($1,$2,$3,$4,$5,'pending',now())",
        key,
        team_id,
        start,
        delta,
        current_invoice_id,
    )
    if not await _adjustment_item_exists(current_invoice_id, key):
        await _create_adjustment_item(team_id, start, key, delta, current_invoice_id, customer_id)
    await _attach_adjustment(conn, team_id, start, key, delta, live)


async def _reconcile_late_cycles(team_id, current_invoice_id: str, customer_id: str, before_period_start) -> None:
    """Carry fee deltas from completed cycles onto this invoice, each charged once.

    Each delta is recorded as a pending intent and committed before Stripe is
    called. If a later step fails, the next run finds the intent and checks its
    recorded invoice before charging anything again.
    """
    lock_key = f"team-late-reconcile-{team_id}"
    async with db._conn().acquire() as conn:
        locked = await conn.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1,0))", lock_key)
        if not locked:
            raise HTTPException(503, "Team late usage reconciliation is already running")
        try:
            rows = await conn.fetch(
                "SELECT period_start,period_end,savings_usd,fee_cents,fee_adjustment_cents "
                "FROM billing.team_cycles WHERE team_id=$1 AND period_end <= $2 "
                "AND payment_status='paid' AND reconciliation_state IN ('attached','needs_reconciliation','settled') "
                "ORDER BY period_start",
                team_id,
                before_period_start,
            )
            for row in rows:
                await _reconcile_cycle(conn, team_id, row, current_invoice_id, customer_id)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1,0))", lock_key)


async def handle_event(event) -> bool:
    """Handle a Team event when its metadata/customer identifies a Team."""
    obj = event.data.object
    kind = event.type
    metadata = obj.metadata.to_dict() if getattr(obj, "metadata", None) else {}
    customer = getattr(obj, "customer", None)
    if not customer and kind.startswith("checkout.session"):
        customer = getattr(obj, "customer", None)
    if customer and not isinstance(customer, str):
        customer = customer.id
    team_id = await _team_for_customer(customer, metadata.get("team_id")) if customer else None
    if kind == "customer.subscription.deleted" and team_id:
        await _settle_final_cycle(team_id, obj, customer)
        await sync_subscription(obj.id)
        return True
    if kind.startswith("customer.subscription") or kind == "checkout.session.completed":
        sub_id = getattr(obj, "id", None) if kind.startswith("customer.subscription") else getattr(obj, "subscription", None)
        if sub_id and team_id:
            await sync_subscription(sub_id if isinstance(sub_id, str) else sub_id.id)
            return True
    if kind == "invoice.created":
        from billing import _invoice_subscription_id

        sub_id = _invoice_subscription_id(obj)
        if team_id and sub_id and obj.billing_reason == "subscription_cycle":
            period_start = datetime.fromtimestamp(obj.period_start, timezone.utc)
            await _handle_cycle_invoice(obj, sub_id)
            await _reconcile_late_cycles(team_id, obj.id, customer, period_start)
            return True
    if kind in {"invoice.paid", "invoice.payment_failed", "invoice.voided"} and team_id:
        status = {"invoice.paid": "paid", "invoice.payment_failed": "failed", "invoice.voided": "void"}[kind]
        failed_at = datetime.now(timezone.utc) if status in {"failed", "void"} else None
        await db._conn().execute(
            "UPDATE billing.team_cycles SET payment_status=$2,payment_failed_at=COALESCE(payment_failed_at,$3), "
            "invoice_url=COALESCE($4,invoice_url),reconciliation_state=CASE WHEN $2='paid' THEN 'needs_reconciliation' ELSE reconciliation_state END,updated_at=now() "
            "WHERE stripe_invoice_id=$1 AND payment_status NOT IN ('paid','void') OR stripe_invoice_id=$1 AND $2='paid'",
            obj.id,
            status,
            failed_at,
            getattr(obj, "hosted_invoice_url", None),
        )
        sub_id = None
        from billing import _invoice_subscription_id

        sub_id = _invoice_subscription_id(obj)
        if sub_id:
            await sync_subscription(sub_id)
        return True
    # Once the customer maps to a Team, consume every event for it so none of
    # its invoices can fall through to personal Pro savings billing.
    return bool(team_id)


async def enforce_unpaid_fees() -> int:
    """Cancel Team subscriptions whose consolidated savings fee passed grace."""
    rows = await db._conn().fetch(
        "SELECT s.team_id,s.stripe_subscription_id FROM core.team_subscriptions s "
        "WHERE s.stripe_subscription_id IS NOT NULL AND s.status IN ('active','trialing') "
        "AND EXISTS (SELECT 1 FROM billing.team_cycles c WHERE c.team_id=s.team_id "
        "AND c.payment_status IN ('failed','void') AND c.payment_failed_at < now() - ($1::int * interval '1 day'))",
        db.FEE_GRACE_DAYS,
    )
    cancelled = 0
    for row in rows:
        subscription_id = row["stripe_subscription_id"]
        try:
            sub = await _call(_stripe().v1.subscriptions.retrieve, subscription_id)
            if sub.status not in {"canceled", "incomplete_expired"}:
                await _call(
                    _stripe().v1.subscriptions.update,
                    subscription_id,
                    {"metadata": {"team_id": str(row['team_id']), "cancel_reason": "unpaid_team_savings_fee"}},
                )
                await _call(
                    _stripe().v1.subscriptions.cancel,
                    subscription_id,
                    {"prorate": False, "cancellation_details": {"comment": f"Team savings fee unpaid after {db.FEE_GRACE_DAYS}-day grace period"}},
                )
                await sync_subscription(subscription_id)
                cancelled += 1
        except Exception:
            logger.exception("Could not cancel overdue Team subscription %s; retry", subscription_id)
    return cancelled


class SeatChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seat_count: int = Field(ge=MIN_SEATS)


async def _team_context(team_context, user, team_id, minimum_role="owner"):
    try:
        result = team_context(user, team_id, minimum_role)
    except TypeError:
        result = team_context(user, team_id, minimum_role=minimum_role)
    if asyncio.iscoroutine(result):
        result = await result
    return result


def install(app, current_user, team_context):
    """Install Team-only billing routes; auth helper is supplied by teams API."""

    def enabled():
        if not ENABLED:
            raise HTTPException(404, "Team billing is not enabled")

    @app.post("/teams/{team_id}/billing/checkout")
    async def checkout(team_id: str, body: SeatChangeIn, user=Depends(current_user)):
        enabled()
        await _team_context(team_context, user, team_id)
        if body.seat_count < MIN_SEATS:
            raise HTTPException(422, "Team subscriptions require at least three seats")
        # Serialize all checkout attempts for this Team. Persist the key before
        # calling Stripe so a crash after Stripe creates the session is replayable.
        pool = db._conn()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT t.name,s.* FROM core.teams t JOIN core.team_subscriptions s ON s.team_id=t.id "
                    "WHERE t.id=$1 FOR UPDATE OF s",
                    team_id,
                )
                if row is None:
                    raise HTTPException(404, "Team not found")
                if row["stripe_subscription_id"] and row["status"] not in {"canceled", "incomplete_expired"}:
                    raise HTTPException(409, "This Team already has a subscription")

                customer_id = row["stripe_customer_id"]
                if not customer_id:
                    customer = await _call(
                        _stripe().v1.customers.create,
                        {"name": row["name"], "metadata": {"team_id": str(team_id), "owner_user_id": str(user.get("user_id", user.get("id"))) }},
                        {"idempotency_key": f"team-customer-{team_id}"},
                    )
                    customer_id = customer.id
                    await conn.execute(
                        "UPDATE core.team_subscriptions SET stripe_customer_id=$2,updated_at=now() WHERE team_id=$1",
                        team_id,
                        customer_id,
                    )

                requested_seats = int(body.seat_count)
                previous_seats = row["checkout_seat_count"]
                previous_id = row["stripe_checkout_session_id"]
                previous_key = row["stripe_checkout_idempotency_key"]
                reuse_previous_key = True
                if previous_seats == requested_seats and previous_id:
                    existing = await _call(_stripe().v1.checkout.sessions.retrieve, previous_id)
                    if getattr(existing, "status", None) == "open" and getattr(existing, "url", None):
                        return {"checkout_url": existing.url}
                    if getattr(existing, "status", None) == "complete":
                        raise HTTPException(409, "Checkout completed; Team subscription confirmation is pending")
                    reuse_previous_key = False

                sessions = await _call(
                    _stripe().v1.checkout.sessions.list,
                    {"customer": customer_id, "status": "open", "limit": 100},
                )
                completed_sessions = await _call(
                    _stripe().v1.checkout.sessions.list,
                    {"customer": customer_id, "status": "complete", "limit": 100},
                )
                for candidate in completed_sessions.data:
                    metadata = candidate.metadata.to_dict() if hasattr(candidate.metadata, "to_dict") else (candidate.metadata or {})
                    if str(metadata.get("team_id")) == str(team_id):
                        raise HTTPException(409, "Checkout completed; Team subscription confirmation is pending")
                open_matches = []
                for candidate in sessions.data:
                    metadata = candidate.metadata.to_dict() if hasattr(candidate.metadata, "to_dict") else (candidate.metadata or {})
                    if str(metadata.get("team_id")) == str(team_id):
                        open_matches.append((candidate, metadata))
                for candidate, metadata in open_matches:
                    candidate_seats = int(metadata.get("seat_count", 0) or 0)
                    if candidate_seats == requested_seats and getattr(candidate, "url", None):
                        idem_key = metadata.get("checkout_idempotency_key") or previous_key or f"team-checkout-{team_id}-{uuid.uuid4().hex}"
                        await conn.execute(
                            "UPDATE core.team_subscriptions SET stripe_checkout_session_id=$2,stripe_checkout_idempotency_key=$3,"
                            "checkout_seat_count=$4,updated_at=now() WHERE team_id=$1",
                            team_id, candidate.id, idem_key, requested_seats,
                        )
                        return {"checkout_url": candidate.url}
                    await _call(_stripe().v1.checkout.sessions.expire, candidate.id)

                # Also expire the known session if Stripe did not return it in the
                # open-session scan (for example, a stale local ID).
                if previous_id and previous_seats != requested_seats:
                    try:
                        known = await _call(_stripe().v1.checkout.sessions.retrieve, previous_id)
                        if getattr(known, "status", None) == "open":
                            await _call(_stripe().v1.checkout.sessions.expire, previous_id)
                    except Exception:
                        logger.info("Could not retrieve stale Team checkout session %s", previous_id)

                idem_key = previous_key if reuse_previous_key and previous_seats == requested_seats and previous_key else f"team-checkout-{team_id}-{uuid.uuid4().hex}"
                await conn.execute(
                    "UPDATE core.team_subscriptions SET stripe_checkout_session_id=NULL,stripe_checkout_idempotency_key=$2,"
                    "checkout_seat_count=$3,updated_at=now() WHERE team_id=$1",
                    team_id, idem_key, requested_seats,
                )
                session = await _call(
                    _stripe().v1.checkout.sessions.create,
                    {
                        "mode": "subscription",
                        "customer": customer_id,
                        "client_reference_id": str(team_id),
                        "line_items": [{"price": await _price_id(), "quantity": requested_seats}],
                        "subscription_data": {"metadata": {"team_id": str(team_id)}},
                        "metadata": {"team_id": str(team_id), "seat_count": str(requested_seats), "checkout_idempotency_key": idem_key},
                        "integration_identifier": "contextshrink-team-checkout-bqztwral",
                        "success_url": f"{APP_URL}/subscriptions?team_checkout=success",
                        "cancel_url": f"{APP_URL}/subscriptions?team_checkout=cancelled",
                    },
                    {"idempotency_key": idem_key},
                )
                await conn.execute(
                    "UPDATE core.team_subscriptions SET stripe_checkout_session_id=$2,updated_at=now() WHERE team_id=$1",
                    team_id, session.id,
                )
                return {"checkout_url": session.url}

    @app.post("/teams/{team_id}/billing/portal")
    async def portal(team_id: str, user=Depends(current_user)):
        enabled()
        await _team_context(team_context, user, team_id)
        row = await _team_row(team_id)
        if not row or not row["stripe_customer_id"]:
            raise HTTPException(404, "No Team billing account yet")
        session = await _call(
            _stripe().v1.billing_portal.sessions.create,
            {
                "customer": row["stripe_customer_id"],
                "configuration": await _team_portal_configuration_id(),
                "return_url": f"{APP_URL}/subscriptions",
            },
        )
        return {"portal_url": session.url}

    @app.get("/teams/{team_id}/billing/estimate")
    async def get_estimate(team_id: str, user=Depends(current_user)):
        enabled()
        context = await _team_context(team_context, user, team_id, minimum_role="member")
        return await estimate(
            team_id,
            user,
            viewer_membership_id=(context.get("membership_id") if isinstance(context, dict) else None),
            viewer_role=(context.get("role") if isinstance(context, dict) else None),
        )

    @app.get("/teams/{team_id}/billing/seats/preview")
    async def preview_seats(team_id: str, seat_count: int = Query(ge=MIN_SEATS), user=Depends(current_user)):
        enabled()
        await _team_context(team_context, user, team_id)
        row = await _team_row(team_id)
        if not row or not row["stripe_subscription_id"]:
            raise HTTPException(409, "Start Team billing through checkout first")
        current, target = int(row["seat_count"]), int(seat_count)
        if target < current:
            occupied = await db._conn().fetchval(
                "SELECT count(*) FROM core.team_memberships WHERE team_id=$1 AND left_at IS NULL",
                team_id,
            )
            reserved = await db._conn().fetchval(
                "SELECT count(*) FROM core.team_invitations WHERE team_id=$1 AND status='pending' AND expires_at > now()",
                team_id,
            )
            if int(occupied or 0) + int(reserved or 0) > target:
                raise HTTPException(409, "Purchased seats must cover active members and invitations")
        prorated = Decimal("0.00")
        if target > current and row["current_period_start"] and row["current_period_end"]:
            now = datetime.now(timezone.utc)
            period_seconds = max(1, (row["current_period_end"] - row["current_period_start"]).total_seconds())
            remaining = max(0, (row["current_period_end"] - now).total_seconds())
            prorated = money(SEAT_PRICE_USD * (target - current) * Decimal(str(remaining / period_seconds)))
        return {
            "current_seat_count": current,
            "seat_count": target,
            "prorated_amount_usd": float(prorated),
            "next_invoice_amount_usd": float(SEAT_PRICE_USD * target),
            "effective_at": "immediately" if target > current else (row["current_period_end"].isoformat() if row["current_period_end"] else None),
            "currency": "USD",
        }

    @app.patch("/teams/{team_id}/billing/seats")
    async def change_seats(team_id: str, body: SeatChangeIn, user=Depends(current_user)):
        enabled()
        await _team_context(team_context, user, team_id)
        exists = await _team_row(team_id)
        if not exists or not exists["stripe_subscription_id"]:
            raise HTTPException(409, "Start Team billing through checkout first")
        target = body.seat_count
        updated_subscription_id = None
        async with db._conn().acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT seat_count,pending_seat_count,stripe_subscription_id,current_period_start,current_period_end "
                    "FROM core.team_subscriptions WHERE team_id=$1 FOR UPDATE",
                    team_id,
                )
                current = int(row["seat_count"])
                subscription_id = row["stripe_subscription_id"]
                price_id = await _price_id()
                if target < current:
                    occupied = await conn.fetchval(
                        "SELECT count(*) FROM core.team_memberships WHERE team_id=$1 AND left_at IS NULL",
                        team_id,
                    )
                    reserved = await conn.fetchval(
                        "SELECT count(*) FROM core.team_invitations WHERE team_id=$1 AND status='pending' AND expires_at > now()",
                        team_id,
                    )
                    if int(occupied or 0) + int(reserved or 0) > target:
                        raise HTTPException(409, "Purchased seats must cover active members and invitations")
                    await _schedule_seat_reduction(subscription_id, target, price_id)
                    await conn.execute(
                        "UPDATE core.team_subscriptions SET pending_seat_count=$2,updated_at=now() WHERE team_id=$1",
                        team_id,
                        target,
                    )
                    pending = target
                    effective_at = row["current_period_end"]
                    prorated_amount = 0
                elif target > current:
                    sub = await _call(_stripe().v1.subscriptions.retrieve, subscription_id)
                    scheduled_id = _expandable_id(getattr(sub, "schedule", None))
                    if scheduled_id:
                        await _call(_stripe().v1.subscription_schedules.release, scheduled_id)
                    item = next((i for i in sub.items.data if i.price.id == price_id), None)
                    if item is None:
                        raise HTTPException(409, "Team seat price is missing from the subscription")
                    updated = await _call(
                        _stripe().v1.subscriptions.update,
                        subscription_id,
                        {"items": [{"id": item.id, "quantity": target}], "proration_behavior": "create_prorations"},
                        {"idempotency_key": f"team-seats-{team_id}-{target}-{int(datetime.now(timezone.utc).timestamp() // 60)}"},
                    )
                    updated_subscription_id = updated.id
                    await conn.execute(
                        "UPDATE core.team_subscriptions SET pending_seat_count=NULL,updated_at=now() WHERE team_id=$1",
                        team_id,
                    )
                    pending = None
                    effective_at = row["current_period_end"]
                    prorated_amount = None
                else:
                    if row["pending_seat_count"] is not None:
                        sub = await _call(_stripe().v1.subscriptions.retrieve, subscription_id)
                        scheduled_id = _expandable_id(getattr(sub, "schedule", None))
                        if scheduled_id:
                            await _call(_stripe().v1.subscription_schedules.release, scheduled_id)
                        await conn.execute(
                            "UPDATE core.team_subscriptions SET pending_seat_count=NULL,updated_at=now() WHERE team_id=$1",
                            team_id,
                        )
                    pending = None
                    effective_at = row["current_period_end"]
                    prorated_amount = 0
        if updated_subscription_id:
            # Stripe's verified subscription webhook remains canonical; sync
            # only after releasing the row lock to avoid webhook deadlock.
            await sync_subscription(updated_subscription_id)
        return {
            "seat_count": target if target > current else current,
            "pending_seat_count": pending,
            "effective_at": effective_at.isoformat() if effective_at else None,
            "prorated_amount_usd": prorated_amount,
            "next_invoice_amount_usd": float(SEAT_PRICE_USD * (pending or target)),
        }
