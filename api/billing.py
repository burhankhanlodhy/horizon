"""Stripe billing for paid plans.

Pro has a $0 monthly Stripe subscription (Checkout saves the payment method)
plus a savings fee computed from the account's own proxy ledger: 5% of the
whole cycle's savings when they exceed $20. At each renewal the fee for the
cycle that just ended is invoiced separately, keyed by (account, period) so a
period is never charged twice. Plan state is synced only from verified Stripe
webhooks; the browser can never grant a paid plan.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

import db
import stripe
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("contextshrink.billing")

SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
APP_URL = os.environ.get("APP_URL", "https://app.contextshrink.com").rstrip("/")
PRO_PRICE_LOOKUP_KEY = os.environ.get("STRIPE_PRO_PRICE_LOOKUP_KEY", "contextshrink_pro_monthly")
# Tags Checkout Sessions in the Stripe Dashboard (fixed random suffix).
CHECKOUT_INTEGRATION_ID = "contextshrink-pro-checkout-qhzvmtra"

SAVINGS_FEE_RATE = Decimal("0.05")
SAVINGS_FEE_THRESHOLD_USD = Decimal("20.00")

# Subscriptions cancelled because a savings fee stayed unpaid past the grace
# period are tagged with this so no final-period fee is billed to the same card.
UNPAID_FEE_CANCEL_REASON = "unpaid_savings_fee"
ENFORCE_INTERVAL_SECONDS = 600

# Stripe statuses that keep paid entitlements; everything else is not "active".
ENTITLED_STATUSES = {"active", "trialing"}
ENDED_STATUSES = {"canceled", "incomplete_expired"}

_client = stripe.StripeClient(SECRET_KEY, max_network_retries=2) if SECRET_KEY else None
_price_ids: dict[str, str] = {}


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def savings_fee(savings: Decimal) -> Decimal:
    """5% of the full cycle savings, waived at or below the $20 threshold."""
    savings = money(savings)
    return money(savings * SAVINGS_FEE_RATE) if savings > SAVINGS_FEE_THRESHOLD_USD else Decimal("0.00")


async def ledger_savings(user_id, start: datetime, end: datetime, connection=None) -> Decimal:
    value = await (connection if connection is not None else db._conn()).fetchval(
        "SELECT COALESCE(SUM(NULLIF(data->>'savings_usd','')::numeric),0) "
        "FROM metrics.proxy_events WHERE user_id=$1 AND occurred_at >= $2 AND occurred_at < $3",
        user_id,
        start,
        end,
    )
    return Decimal(value or 0)


def _stripe():
    if _client is None:
        raise HTTPException(503, "Billing is not configured")
    return _client


async def _call(fn, *args, **kwargs):
    # stripe-python is synchronous; keep it off the event loop.
    return await asyncio.to_thread(fn, *args, **kwargs)


async def _price_id(lookup_key: str) -> str:
    if lookup_key not in _price_ids:
        prices = await _call(
            _stripe().v1.prices.list, {"lookup_keys": [lookup_key], "active": True, "limit": 1}
        )
        if not prices.data:
            raise HTTPException(503, "Billing price is not set up")
        _price_ids[lookup_key] = prices.data[0].id
    return _price_ids[lookup_key]


def _ts(value) -> datetime | None:
    return datetime.fromtimestamp(value, timezone.utc) if value else None


def _expandable_id(value) -> str | None:
    return value if isinstance(value, str) or value is None else value.id


async def _billing_row(user_id):
    return await db._conn().fetchrow(
        "SELECT plan, status, stripe_customer_id, stripe_subscription_id, cancel_at_period_end "
        "FROM core.subscriptions WHERE user_id=$1",
        user_id,
    )


async def _ensure_customer(user) -> str:
    await db.ensure_subscription(str(user["user_id"]))
    row = await _billing_row(user["user_id"])
    if row and row["stripe_customer_id"]:
        return row["stripe_customer_id"]
    customer = await _call(
        _stripe().v1.customers.create,
        {"email": user["email"], "name": user["name"], "metadata": {"user_id": str(user["user_id"])}},
        {"idempotency_key": f"customer-{user['user_id']}"},
    )
    # Keep the first stored customer if two requests raced.
    return await db._conn().fetchval(
        "UPDATE core.subscriptions SET stripe_customer_id=COALESCE(stripe_customer_id,$2), "
        "updated_at=now() WHERE user_id=$1 RETURNING stripe_customer_id",
        user["user_id"],
        customer.id,
    )


# ---------------------------------------------------------------- webhooks


async def _user_for_customer(customer_id: str, metadata_user_id: str | None):
    user_id = await db._conn().fetchval(
        "SELECT user_id FROM core.subscriptions WHERE stripe_customer_id=$1", customer_id
    )
    if user_id is None and metadata_user_id:
        # Fallback only: the customer is created by this API, so it is normally stored.
        user_id = await db._conn().fetchval(
            "UPDATE core.subscriptions SET stripe_customer_id=$2, updated_at=now() "
            "WHERE user_id=$1::uuid AND stripe_customer_id IS NULL RETURNING user_id",
            metadata_user_id,
            customer_id,
        )
    return user_id


async def _plan_for(subscription) -> Literal["pro"] | None:
    pro_price = await _price_id(PRO_PRICE_LOOKUP_KEY)
    for item in subscription["items"].data:
        if item.price.id == pro_price:
            return "pro"
    return None


async def sync_subscription(subscription_id: str) -> None:
    """Mirror a Stripe subscription onto the account, from Stripe's current state."""
    sub = await _call(_stripe().v1.subscriptions.retrieve, subscription_id)
    customer_id = _expandable_id(sub.customer)
    # StripeObject is not a dict in stripe-python 16; convert before .get().
    metadata = sub.metadata.to_dict() if sub.metadata else {}
    user_id = await _user_for_customer(customer_id, metadata.get("user_id"))
    if user_id is None:
        logger.warning("Stripe subscription %s has no matching account", subscription_id)
        return
    if sub.status in ENDED_STATUSES:
        await db._conn().execute(
            "UPDATE core.subscriptions SET plan='free', status='active', stripe_subscription_id=NULL, "
            "cancel_at_period_end=false, cancel_at=NULL, current_period_start=NULL, "
            "current_period_end=NULL, updated_at=now() WHERE user_id=$1 AND stripe_subscription_id=$2",
            user_id,
            subscription_id,
        )
        return
    plan = await _plan_for(sub)
    if plan is None:
        logger.warning("Stripe subscription %s has no recognised plan price", subscription_id)
        return
    item = sub["items"].data[0]
    period_end = _ts(item.current_period_end)
    # Newer API versions schedule a cancellation (e.g. from the Customer
    # Portal) with cancel_at, leaving cancel_at_period_end false.
    cancel_at = _ts(sub.cancel_at) or (period_end if sub.cancel_at_period_end else None)
    await db._conn().execute(
        "UPDATE core.subscriptions SET plan=$2, status=$3, stripe_subscription_id=$4, "
        "current_period_start=$5, current_period_end=$6, cancel_at_period_end=$7, cancel_at=$8, "
        "updated_at=now() WHERE user_id=$1",
        user_id,
        plan,
        "active" if sub.status in ENTITLED_STATUSES else sub.status,
        subscription_id,
        _ts(item.current_period_start),
        period_end,
        cancel_at is not None,
        cancel_at,
    )


async def renew_subscription(subscription_id: str) -> None:
    """Undo a scheduled cancellation so the subscription keeps renewing."""
    sub = await _call(_stripe().v1.subscriptions.retrieve, subscription_id)
    if sub.status in ENDED_STATUSES:
        raise HTTPException(409, "This subscription has ended. Upgrade through checkout.")
    # Clear whichever mechanism scheduled it; "" unsets cancel_at.
    if sub.cancel_at:
        await _call(_stripe().v1.subscriptions.update, subscription_id, {"cancel_at": ""})
    elif sub.cancel_at_period_end:
        await _call(
            _stripe().v1.subscriptions.update, subscription_id, {"cancel_at_period_end": False}
        )
    # Reflect it now; the customer.subscription.updated webhook agrees.
    await sync_subscription(subscription_id)


async def charge_savings_fee(
    user_id, customer_id: str, start: datetime, end: datetime, payment_method: str | None
) -> None:
    """Resume the same period invoice, including after an interrupted Stripe call."""
    key = f"savings-fee-{user_id}-{int(start.timestamp())}"
    # Session lock across API workers; writes below commit individually so a
    # failure never rolls back an invoice ID already assigned by Stripe.
    async with db._conn().acquire() as conn:
        locked = await conn.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1,0))", key)
        if not locked:
            raise HTTPException(503, "This billing period is being processed. Please retry.")
        try:
            await _resume_savings_fee(conn, user_id, customer_id, start, end, payment_method, key)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1,0))", key)


async def _resume_savings_fee(conn, user_id, customer_id, start, end, payment_method, key):
    existing = await conn.fetchrow(
        "SELECT savings_usd, fee_cents, period_end, stripe_invoice_id FROM billing.savings_fees "
        "WHERE user_id=$1 AND period_start=$2",
        user_id,
        start,
    )
    if existing is None:
        savings = await ledger_savings(user_id, start, end, conn)
        fee_cents = int(savings_fee(savings) * 100)
        await conn.execute(
            "INSERT INTO billing.savings_fees(user_id, period_start, period_end, savings_usd, fee_cents, "
            "payment_status) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT (user_id, period_start) DO NOTHING",
            user_id,
            start,
            end,
            savings,
            fee_cents,
            "none" if fee_cents == 0 else "pending",
        )
        existing = await conn.fetchrow(
            "SELECT savings_usd, fee_cents, period_end, stripe_invoice_id FROM billing.savings_fees "
            "WHERE user_id=$1 AND period_start=$2",
            user_id,
            start,
        )
    # Late telemetry must not change the amount or Stripe request parameters
    # between attempts for a period whose fee has already been recorded.
    savings, fee_cents, end = existing["savings_usd"], existing["fee_cents"], existing["period_end"]
    if fee_cents == 0:
        return
    period = f"{start:%Y-%m-%d} to {end:%Y-%m-%d}"
    client = _stripe()
    if existing["stripe_invoice_id"]:
        invoice = await _call(client.v1.invoices.retrieve, existing["stripe_invoice_id"])
    else:
        # Recover if Stripe created the invoice but the response or DB write
        # failed. Listing remains usable after Stripe's idempotency cache expires.
        def find_invoice():
            invoices = client.v1.invoices.list({"customer": customer_id, "limit": 100})
            matches = []
            for candidate in invoices.auto_paging_iter():
                metadata = candidate.metadata.to_dict() if candidate.metadata else {}
                if metadata.get("user_id") == str(user_id) and metadata.get("period_start") == start.isoformat():
                    matches.append(candidate)
            return matches

        matches = await _call(find_invoice)
        if len(matches) > 1:
            logger.error("Multiple savings invoices for period %s", key)
            raise HTTPException(502, "Billing invoice needs reconciliation")
        if matches:
            invoice = matches[0]
        else:
            invoice_params = {
                "customer": customer_id,
                "collection_method": "charge_automatically",
                # An incomplete invoice must never finalize itself while the
                # webhook is waiting for a retry to attach its fee line.
                "auto_advance": False,
                "pending_invoice_items_behavior": "exclude",
                "description": f"ContextShrink Pro savings fee, {period}",
                "metadata": {"user_id": str(user_id), "period_start": start.isoformat()},
            }
            if payment_method:
                invoice_params["default_payment_method"] = payment_method
            invoice = await _call(
                client.v1.invoices.create, invoice_params, {"idempotency_key": key + "-invoice"}
            )
    # Link the invoice before finalizing: finalizing attempts payment, and the
    # resulting invoice.paid / invoice.payment_failed webhook looks it up.
    await conn.execute(
        "UPDATE billing.savings_fees SET stripe_invoice_id=$3 WHERE user_id=$1 AND period_start=$2",
        user_id,
        start,
        invoice.id,
    )
    if invoice.status == "draft" and invoice.auto_advance:
        # Also pause drafts left by the previous implementation.
        invoice = await _call(client.v1.invoices.update, invoice.id, {"auto_advance": False})

    def invoice_items():
        return list(client.v1.invoice_items.list({"invoice": invoice.id, "limit": 100}).auto_paging_iter())

    items = await _call(invoice_items)
    if items and (len(items) != 1 or items[0].amount != fee_cents or items[0].currency != "usd"):
        logger.error("Unexpected savings fee lines on invoice %s", invoice.id)
        raise HTTPException(502, "Billing invoice needs reconciliation")
    if not items:
        if invoice.status != "draft":
            # An old empty invoice may already have auto-finalized. Do not
            # silently mark it complete or generate an additional charge.
            logger.error("Savings invoice %s finalized without its fee", invoice.id)
            raise HTTPException(502, "Billing invoice needs reconciliation")
        await _call(
            client.v1.invoice_items.create,
            {
                "customer": customer_id,
                "invoice": invoice.id,
                "currency": "usd",
                "amount": fee_cents,
                "description": f"5% of ${money(savings)} saved, {period}",
            },
            {"idempotency_key": key + "-item"},
        )
    # If finalization succeeded but its response was lost, retrieve above
    # returns the finalized invoice; its URL still needs to be saved locally.
    finalized = invoice
    if invoice.status == "draft":
        finalized = await _call(
            client.v1.invoices.finalize_invoice,
            invoice.id,
            {"auto_advance": True},
            {"idempotency_key": key + "-finalize"},
        )
    await conn.execute(
        "UPDATE billing.savings_fees SET invoice_url=$2 WHERE stripe_invoice_id=$1",
        invoice.id,
        finalized.hosted_invoice_url,
    )


async def record_fee_payment(invoice, outcome: Literal["paid", "failed", "void"]) -> None:
    """Track a savings-fee invoice's payment; no-op for other invoices."""
    if outcome == "failed":
        # Keep the first failure time (the grace clock) across retries, and
        # never let a late failure event override an invoice already paid.
        await db._conn().execute(
            "UPDATE billing.savings_fees SET payment_status='failed', "
            "payment_failed_at=COALESCE(payment_failed_at, now()), "
            "invoice_url=COALESCE($2, invoice_url) "
            "WHERE stripe_invoice_id=$1 AND payment_status NOT IN ('paid','void')",
            invoice.id,
            invoice.hosted_invoice_url,
        )
    else:
        await db._conn().execute(
            "UPDATE billing.savings_fees SET payment_status=$2, payment_failed_at=NULL "
            "WHERE stripe_invoice_id=$1",
            invoice.id,
            outcome,
        )


async def has_unpaid_fee(user_id) -> bool:
    return bool(
        await db._conn().fetchval(
            "SELECT 1 FROM billing.savings_fees WHERE user_id=$1 AND payment_status='failed' LIMIT 1",
            user_id,
        )
    )


async def enforce_unpaid_fees() -> int:
    """Cancel paid plans whose savings fee is unpaid past the grace period.

    The account drops to Free; it can upgrade again once the fee is paid.
    """
    rows = await db._conn().fetch(
        "SELECT user_id, stripe_subscription_id FROM core.subscriptions "
        "WHERE stripe_subscription_id IS NOT NULL AND billing.fee_overdue(user_id, $1)",
        db.FEE_GRACE_DAYS,
    )
    cancelled = 0
    for row in rows:
        sub_id = row["stripe_subscription_id"]
        try:
            sub = await _call(_stripe().v1.subscriptions.retrieve, sub_id)
            if sub.status not in ENDED_STATUSES:
                # Tag first so the deletion webhook skips the final-period fee.
                await _call(
                    _stripe().v1.subscriptions.update,
                    sub_id,
                    {"metadata": {"cancel_reason": UNPAID_FEE_CANCEL_REASON}},
                )
                await _call(
                    _stripe().v1.subscriptions.cancel,
                    sub_id,
                    {
                        "prorate": False,
                        "cancellation_details": {
                            "comment": f"Savings fee unpaid after {db.FEE_GRACE_DAYS}-day grace period"
                        },
                    },
                )
            await sync_subscription(sub_id)
            cancelled += 1
            logger.warning("Cancelled subscription %s: savings fee unpaid past grace", sub_id)
        except Exception:
            logger.exception("Could not cancel overdue subscription %s; will retry", sub_id)
    return cancelled


async def enforce_unpaid_fees_forever() -> None:
    while True:
        try:
            if _client is not None:
                await enforce_unpaid_fees()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unpaid-fee enforcement failed; retrying")
        await asyncio.sleep(ENFORCE_INTERVAL_SECONDS)


async def fee_payment_issue(user_id) -> dict | None:
    """The oldest unpaid savings fee, for the dashboard; None when all are settled."""
    row = await db._conn().fetchrow(
        "SELECT fee_cents, invoice_url, payment_failed_at FROM billing.savings_fees "
        "WHERE user_id=$1 AND payment_status='failed' ORDER BY payment_failed_at LIMIT 1",
        user_id,
    )
    if row is None:
        return None
    pause_at = row["payment_failed_at"] + timedelta(days=db.FEE_GRACE_DAYS)
    return {
        "amount_usd": row["fee_cents"] / 100,
        "invoice_url": row["invoice_url"],
        "failed_at": row["payment_failed_at"].isoformat(),
        "pause_at": pause_at.isoformat(),
        "paused": pause_at <= datetime.now(timezone.utc),
    }


def _invoice_subscription_id(invoice) -> str | None:
    details = getattr(getattr(invoice, "parent", None), "subscription_details", None)
    return _expandable_id(details.subscription) if details else None


async def handle_event(event) -> None:
    obj = event.data.object
    kind = event.type
    if kind == "checkout.session.completed":
        if obj.mode == "subscription" and obj.subscription:
            await sync_subscription(_expandable_id(obj.subscription))
    elif kind in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.paused",
        "customer.subscription.resumed",
    ):
        await sync_subscription(obj.id)
    elif kind == "customer.subscription.deleted":
        await sync_subscription(obj.id)
        # Cancellation creates no renewal invoice: bill the final partial period,
        # unless the plan was cancelled for an unpaid fee (same card would fail).
        user_id = await _user_for_customer(_expandable_id(obj.customer), None)
        item = obj["items"].data[0]
        metadata = obj.metadata.to_dict() if obj.metadata else {}
        if user_id and metadata.get("cancel_reason") != UNPAID_FEE_CANCEL_REASON:
            ended = _ts(obj.ended_at or obj.canceled_at) or datetime.now(timezone.utc)
            await charge_savings_fee(
                user_id,
                _expandable_id(obj.customer),
                _ts(item.current_period_start),
                ended,
                _expandable_id(obj.default_payment_method),
            )
    elif kind in ("invoice.paid", "invoice.payment_failed", "invoice.voided"):
        outcome = {"invoice.paid": "paid", "invoice.payment_failed": "failed"}.get(kind, "void")
        await record_fee_payment(obj, outcome)
        sub_id = _invoice_subscription_id(obj)
        if sub_id:
            await sync_subscription(sub_id)
    elif kind == "invoice.created":
        sub_id = _invoice_subscription_id(obj)
        # A renewal invoice's period is the cycle that just ended.
        if obj.billing_reason == "subscription_cycle" and sub_id:
            user_id = await _user_for_customer(_expandable_id(obj.customer), None)
            if user_id:
                sub = await _call(_stripe().v1.subscriptions.retrieve, sub_id)
                await charge_savings_fee(
                    user_id,
                    _expandable_id(obj.customer),
                    _ts(obj.period_start),
                    _ts(obj.period_end),
                    _expandable_id(sub.default_payment_method),
                )


# ------------------------------------------------------------------ routes


class CheckoutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: Literal["pro"]


def install(app, current_user):
    @app.exception_handler(stripe.StripeError)
    async def stripe_error(request, exc):
        # Never echo provider details; Stripe retries webhooks on 5xx.
        logger.error("Stripe request failed: %s %s", type(exc).__name__, getattr(exc, "code", ""))
        return JSONResponse({"detail": "Billing provider error. Please try again."}, status_code=502)

    @app.post("/billing/checkout")
    async def checkout(body: CheckoutIn, user=Depends(current_user)):
        row = await _billing_row(user["user_id"])
        if row and row["stripe_subscription_id"]:
            raise HTTPException(409, "You already have a subscription. Use Manage billing to change it.")
        if await has_unpaid_fee(user["user_id"]):
            raise HTTPException(409, "Pay your outstanding savings fee before upgrading.")
        customer_id = await _ensure_customer(user)
        session = await _call(
            _stripe().v1.checkout.sessions.create,
            {
                "mode": "subscription",
                "customer": customer_id,
                "client_reference_id": str(user["user_id"]),
                "line_items": [{"price": await _price_id(PRO_PRICE_LOOKUP_KEY), "quantity": 1}],
                # Pro costs $0 up front; the card is needed for the savings fee.
                "payment_method_collection": "always",
                "subscription_data": {"metadata": {"user_id": str(user["user_id"])}},
                "integration_identifier": CHECKOUT_INTEGRATION_ID,
                "success_url": f"{APP_URL}/subscriptions?checkout=success",
                "cancel_url": f"{APP_URL}/subscriptions?checkout=cancelled",
            },
        )
        return {"url": session.url}

    @app.post("/billing/renew")
    async def renew(user=Depends(current_user)):
        row = await _billing_row(user["user_id"])
        if not row or not row["stripe_subscription_id"]:
            raise HTTPException(409, "No subscription to renew. Upgrade through checkout.")
        await renew_subscription(row["stripe_subscription_id"])
        return {"renewed": True}

    @app.post("/billing/portal")
    async def portal(user=Depends(current_user)):
        row = await _billing_row(user["user_id"])
        if not row or not row["stripe_customer_id"]:
            raise HTTPException(404, "No billing account yet")
        session = await _call(
            _stripe().v1.billing_portal.sessions.create,
            {"customer": row["stripe_customer_id"], "return_url": f"{APP_URL}/subscriptions"},
        )
        return {"url": session.url}

    @app.get("/billing/invoices")
    async def invoices(user=Depends(current_user)):
        row = await _billing_row(user["user_id"])
        if not row or not row["stripe_customer_id"] or _client is None:
            return []
        result = await _call(
            _client.v1.invoices.list, {"customer": row["stripe_customer_id"], "limit": 24}
        )
        return [
            {
                "id": inv.id,
                "number": inv.number,
                "created": _ts(inv.created).isoformat(),
                "description": inv.description or "ContextShrink Pro subscription",
                "total": inv.total / 100,
                "currency": inv.currency,
                "status": inv.status,
                "url": inv.hosted_invoice_url,
            }
            for inv in result.data
            if inv.status != "draft"
        ]

    @app.post("/stripe/webhook")
    async def webhook(request: Request):
        if _client is None or not WEBHOOK_SECRET:
            raise HTTPException(503, "Billing webhooks are not configured")
        payload = await request.body()
        try:
            event = _client.construct_event(
                payload, request.headers.get("stripe-signature"), WEBHOOK_SECRET
            )
        except (ValueError, stripe.SignatureVerificationError):
            raise HTTPException(400, "Invalid Stripe signature") from None
        done = await db._conn().fetchval(
            "SELECT 1 FROM billing.stripe_events WHERE event_id=$1", event.id
        )
        if not done:
            # Errors propagate as 500 so Stripe retries; handlers are idempotent.
            await handle_event(event)
            await db._conn().execute(
                "INSERT INTO billing.stripe_events(event_id, type) VALUES($1,$2) "
                "ON CONFLICT DO NOTHING",
                event.id,
                event.type,
            )
        return {"received": True}
