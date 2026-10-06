"""Billing logic against fakes: no Postgres, no network calls to Stripe.

Run from the repo root with the API's requirements installed:
    python -m pytest api/tests
"""

import asyncio
import hashlib
import hmac
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import stripe
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import billing  # noqa: E402

UID = "7b0c6c1e-9d6a-4c51-9a55-0d9f1f0a2b3c"
START = datetime(2026, 9, 1, tzinfo=timezone.utc)
END = datetime(2026, 10, 1, tzinfo=timezone.utc)


class FakeConn:
    """Just enough of the asyncpg pool for billing.py's queries."""

    def __init__(self):
        self.events: set[str] = set()
        self.fees: dict[tuple, dict] = {}
        self.customers = {"cus_1": UID}
        self.plan_updates: list[tuple] = []
        self.failures: list[str] = []
        self.overdue: list[str] = []
        self.ended: list[tuple] = []
        self.locks: set[str] = set()

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def _by_invoice(self, invoice_id):
        return next((r for r in self.fees.values() if r["stripe_invoice_id"] == invoice_id), None)

    async def fetch(self, sql, *args):
        if "billing.fee_overdue" in sql:
            return [{"user_id": UID, "stripe_subscription_id": s} for s in self.overdue]
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        if "pg_try_advisory_lock" in sql:
            if args[0] in self.locks:
                return False
            self.locks.add(args[0])
            return True
        if "billing.stripe_events" in sql:
            return 1 if args[0] in self.events else None
        if "payment_status='failed' LIMIT 1" in sql:
            return 1 if any(r["payment_status"] == "failed" for r in self.fees.values()) else None
        if "SELECT user_id FROM core.subscriptions WHERE stripe_customer_id" in sql:
            return self.customers.get(args[0])
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        if "FROM billing.savings_fees" in sql:
            return self.fees.get((args[0], args[1]))
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        if "pg_advisory_unlock" in sql:
            self.locks.discard(args[0])
        elif "INSERT INTO billing.stripe_events" in sql:
            self.events.add(args[0])
        elif "INSERT INTO billing.savings_fees" in sql:
            self.fees.setdefault(
                (args[0], args[1]),
                {
                    "savings_usd": args[3],
                    "period_end": args[2],
                    "fee_cents": args[4],
                    "stripe_invoice_id": None,
                    "payment_status": args[5],
                    "payment_failed_at": None,
                    "invoice_url": None,
                },
            )
        elif "SET stripe_invoice_id=" in sql:
            self.fees[(args[0], args[1])]["stripe_invoice_id"] = args[2]
        elif "SET invoice_url=" in sql:
            self._by_invoice(args[0])["invoice_url"] = args[1]
        elif "SET payment_status='failed'" in sql:
            row = self._by_invoice(args[0])
            if row and row["payment_status"] not in ("paid", "void"):
                row["payment_status"] = "failed"
                row["payment_failed_at"] = row["payment_failed_at"] or len(self.failures) + 1
                row["invoice_url"] = args[1] or row["invoice_url"]
            self.failures.append(args[0])
        elif "SET payment_status=$2" in sql:
            row = self._by_invoice(args[0])
            if row:
                row.update(payment_status=args[1], payment_failed_at=None)
        elif "UPDATE core.subscriptions SET plan='free'" in sql:
            self.ended.append(args)
        elif "UPDATE core.subscriptions SET plan=" in sql:
            self.plan_updates.append(args)
        else:
            raise AssertionError(sql)


class FakeStripe:
    def __init__(self, conn=None):
        self.calls: list[tuple] = []
        self.linked_at_finalize = None
        self.invoices: dict[str, SimpleNamespace] = {}
        self.items: list[SimpleNamespace] = []
        record = self._record

        def create_invoice(params, options=None):
            invoice = SimpleNamespace(
                id="in_fee", status="draft", auto_advance=params["auto_advance"],
                metadata=SimpleNamespace(to_dict=lambda: params["metadata"]),
                hosted_invoice_url=None,
            )
            self.invoices[invoice.id] = invoice
            return record("invoice", params, options, invoice)

        def create_item(params, options=None):
            item = SimpleNamespace(**params)
            self.items.append(item)
            return record("item", params, options, item)

        def update_invoice(invoice_id, params, options=None):
            invoice = self.invoices[invoice_id]
            for name, value in params.items():
                setattr(invoice, name, value)
            return record("update", params, options, invoice)

        def finalize(invoice_id, params=None, options=None):
            # Payment is attempted on finalize; its webhook needs the link.
            if conn is not None:
                self.linked_at_finalize = conn._by_invoice(invoice_id) is not None
            invoice = self.invoices[invoice_id]
            invoice.status = "open"
            invoice.hosted_invoice_url = "https://pay.test/in_fee"
            return record("finalize", invoice_id, options, invoice)

        self.v1 = SimpleNamespace(
            invoices=SimpleNamespace(
                create=create_invoice,
                retrieve=lambda i: self.invoices[i],
                list=lambda p: SimpleNamespace(auto_paging_iter=lambda: iter(self.invoices.values())),
                update=update_invoice,
                finalize_invoice=finalize,
            ),
            invoice_items=SimpleNamespace(
                create=create_item,
                list=lambda p: SimpleNamespace(
                    auto_paging_iter=lambda: (item for item in self.items if item.invoice == p["invoice"])
                ),
            ),
            subscriptions=SimpleNamespace(
                retrieve=lambda i: SimpleNamespace(default_payment_method="pm_1")
            ),
        )

    def _record(self, name, params, options, result):
        self.calls.append((name, params, options))
        return result


@pytest.fixture
def env(monkeypatch):
    conn = FakeConn()
    fake = FakeStripe(conn)
    monkeypatch.setattr(billing.db, "_conn", lambda: conn)
    monkeypatch.setattr(billing, "_client", fake)
    return SimpleNamespace(conn=conn, stripe=fake)


# ── Fee rule ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "savings, fee",
    [("0", "0.00"), ("20.00", "0.00"), ("20.004", "0.00"), ("20.01", "1.00"), ("100", "5.00")],
)
def test_savings_fee_threshold_is_a_cliff(savings, fee):
    assert billing.savings_fee(Decimal(savings)) == Decimal(fee)


# ── Charging a period ─────────────────────────────────────────────────


def test_fee_invoiced_once_per_period(env, monkeypatch):
    async def savings(*_):
        return Decimal("100")

    monkeypatch.setattr(billing, "ledger_savings", savings)
    for _ in range(2):  # webhook redelivery
        asyncio.run(billing.charge_savings_fee(UID, "cus_1", START, END, "pm_1"))
    names = [c[0] for c in env.stripe.calls]
    assert names == ["invoice", "item", "finalize"]
    invoice, item = env.stripe.calls[0][1], env.stripe.calls[1][1]
    assert invoice["default_payment_method"] == "pm_1"
    assert invoice["pending_invoice_items_behavior"] == "exclude"
    assert item == {**item, "amount": 500, "currency": "usd", "invoice": "in_fee"}
    fee = env.conn.fees[(UID, START)]
    assert (fee["fee_cents"], fee["stripe_invoice_id"], fee["payment_status"]) == (500, "in_fee", "pending")
    assert fee["invoice_url"] == "https://pay.test/in_fee"
    # Linked before finalize, so the payment webhook can find the row.
    assert env.stripe.linked_at_finalize is True


def test_no_invoice_when_savings_at_or_below_threshold(env, monkeypatch):
    async def savings(*_):
        return Decimal("20")

    monkeypatch.setattr(billing, "ledger_savings", savings)
    asyncio.run(billing.charge_savings_fee(UID, "cus_1", START, END, None))
    assert env.stripe.calls == []
    assert env.conn.fees[(UID, START)]["fee_cents"] == 0
    assert env.conn.fees[(UID, START)]["payment_status"] == "none"


@pytest.mark.parametrize("stage", ["invoice", "item", "finalize", "invoice_link", "invoice_url"])
@pytest.mark.parametrize("after_success", [False, True])
def test_interrupted_fee_resumes_without_duplicate_stripe_objects(env, monkeypatch, stage, after_success):
    """No idempotency cache in these fakes: recovery must read durable state."""
    savings_reads = []

    async def savings(*args):
        savings_reads.append(args)
        return Decimal("100") if len(savings_reads) == 1 else Decimal("200")

    monkeypatch.setattr(billing, "ledger_savings", savings)
    interrupted = False

    if stage in ("invoice_link", "invoice_url"):
        original = env.conn.execute
        target = "SET stripe_invoice_id=" if stage == "invoice_link" else "SET invoice_url="

        async def execute(sql, *args):
            nonlocal interrupted
            if target in sql and not interrupted:
                interrupted = True
                if after_success:
                    await original(sql, *args)
                raise RuntimeError("Simulated database interruption")
            return await original(sql, *args)

        monkeypatch.setattr(env.conn, "execute", execute)
    else:
        service, method = {
            "invoice": (env.stripe.v1.invoices, "create"),
            "item": (env.stripe.v1.invoice_items, "create"),
            "finalize": (env.stripe.v1.invoices, "finalize_invoice"),
        }[stage]
        original = getattr(service, method)

        def stripe_call(*args):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                if after_success:
                    original(*args)
                raise RuntimeError("Simulated Stripe interruption")
            return original(*args)

        monkeypatch.setattr(service, method, stripe_call)

    with pytest.raises(RuntimeError, match="Simulated"):
        asyncio.run(billing.charge_savings_fee(UID, "cus_1", START, END, "pm_1"))
    assert not env.conn.locks
    if env.stripe.invoices and stage != "invoice_url" and not (stage == "finalize" and after_success):
        assert env.stripe.invoices["in_fee"].auto_advance is False

    # A different supplied end and new ledger entries must not alter the
    # period/fee persisted by the first attempt, or its Stripe parameters.
    asyncio.run(billing.charge_savings_fee(UID, "cus_1", START, END + timedelta(days=1), "pm_1"))
    asyncio.run(billing.charge_savings_fee(UID, "cus_1", START, END, "pm_1"))
    assert [c[0] for c in env.stripe.calls] == ["invoice", "item", "finalize"]
    assert len(env.stripe.invoices) == len(env.stripe.items) == len(savings_reads) == 1
    fee = env.conn.fees[(UID, START)]
    assert fee["savings_usd"] == Decimal("100")
    assert fee["fee_cents"] == env.stripe.items[0].amount == 500
    assert fee["period_end"] == END
    assert fee["invoice_url"] == "https://pay.test/in_fee"
    assert not env.conn.locks


def test_concurrent_fee_delivery_requests_retry_then_reuses_completed_invoice(env, monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def savings(*_):
            entered.set()
            await release.wait()
            return Decimal("100")

        monkeypatch.setattr(billing, "ledger_savings", savings)
        first = asyncio.create_task(billing.charge_savings_fee(UID, "cus_1", START, END, "pm_1"))
        await entered.wait()
        try:
            with pytest.raises(billing.HTTPException) as exc:
                await billing.charge_savings_fee(UID, "cus_1", START, END, "pm_1")
            assert exc.value.status_code == 503
            assert env.conn.locks  # The competing delivery did not unlock the first.
        finally:
            release.set()
            await first
        await billing.charge_savings_fee(UID, "cus_1", START, END, "pm_1")

    asyncio.run(scenario())
    assert [c[0] for c in env.stripe.calls] == ["invoice", "item", "finalize"]
    assert not env.conn.locks


def _seed_partial_invoice(env, *, status="draft", auto_advance=False, items=(), linked=True):
    env.conn.fees[(UID, START)] = {
        "savings_usd": Decimal("100"), "fee_cents": 500, "period_end": END,
        "stripe_invoice_id": "in_fee" if linked else None,
        "payment_status": "pending", "payment_failed_at": None, "invoice_url": None,
    }
    env.stripe.invoices["in_fee"] = stripe.Invoice.construct_from(
        {
            "id": "in_fee", "object": "invoice", "status": status, "auto_advance": auto_advance,
            "metadata": {"user_id": UID, "period_start": START.isoformat()},
            "hosted_invoice_url": None if status == "draft" else "https://pay.test/in_fee",
        },
        "sk_test_x",
    )
    env.stripe.items = [
        stripe.InvoiceItem.construct_from(
            {"id": f"ii_{i}", "object": "invoiceitem", "invoice": "in_fee", **item}, "sk_test_x"
        )
        for i, item in enumerate(items)
    ]


@pytest.mark.parametrize("with_item", [False, True])
def test_legacy_draft_is_paused_and_completed(env, with_item):
    _seed_partial_invoice(
        env, auto_advance=True, items=[{"amount": 500, "currency": "usd"}] if with_item else []
    )
    asyncio.run(billing.charge_savings_fee(UID, "cus_1", START, END, "pm_1"))
    assert [c[0] for c in env.stripe.calls] == (["update", "finalize"] if with_item else ["update", "item", "finalize"])
    assert env.stripe.calls[0][1] == {"auto_advance": False}
    assert len(env.stripe.items) == 1
    assert env.conn.fees[(UID, START)]["invoice_url"] == "https://pay.test/in_fee"


@pytest.mark.parametrize("status", ["open", "paid", "void", "uncollectible"])
def test_finalized_invoice_recovers_url_without_new_charge(env, status):
    _seed_partial_invoice(env, status=status, items=[{"amount": 500, "currency": "usd"}])
    asyncio.run(billing.charge_savings_fee(UID, "cus_1", START, END, "pm_1"))
    assert env.stripe.calls == []
    assert env.conn.fees[(UID, START)]["invoice_url"] == "https://pay.test/in_fee"


@pytest.mark.parametrize(
    "status, items",
    [
        ("paid", []),
        ("draft", [{"amount": 0, "currency": "usd"}]),
        ("draft", [{"amount": 500, "currency": "eur"}]),
        ("draft", [{"amount": 500, "currency": "usd"}] * 2),
    ],
)
def test_inconsistent_invoice_requires_reconciliation_without_new_charge(env, status, items):
    _seed_partial_invoice(env, status=status, items=items)
    with pytest.raises(billing.HTTPException) as exc:
        asyncio.run(billing.charge_savings_fee(UID, "cus_1", START, END, "pm_1"))
    assert exc.value.status_code == 502
    assert env.stripe.calls == []
    assert not env.conn.locks


def test_multiple_matching_invoices_require_reconciliation(env):
    _seed_partial_invoice(env, linked=False)
    duplicate = stripe.Invoice.construct_from(env.stripe.invoices["in_fee"].to_dict(), "sk_test_x")
    duplicate.id = "in_duplicate"
    env.stripe.invoices[duplicate.id] = duplicate
    with pytest.raises(billing.HTTPException) as exc:
        asyncio.run(billing.charge_savings_fee(UID, "cus_1", START, END, "pm_1"))
    assert exc.value.status_code == 502
    assert env.conn.fees[(UID, START)]["stripe_invoice_id"] is None
    assert env.stripe.calls == []
    assert not env.conn.locks


# ── Fee payment tracking ──────────────────────────────────────────────


def _fee_invoice(url="https://pay.test/in_fee"):
    return stripe.Invoice.construct_from(
        {"id": "in_fee", "object": "invoice", "customer": "cus_1", "hosted_invoice_url": url},
        "sk_test_x",
    )


@pytest.fixture
def charged(env, monkeypatch):
    async def savings(*_):
        return Decimal("50")

    monkeypatch.setattr(billing, "ledger_savings", savings)
    asyncio.run(billing.charge_savings_fee(UID, "cus_1", START, END, None))
    return env.conn.fees[(UID, START)]


def test_failed_fee_keeps_first_failure_time_across_retries(env, charged):
    for _ in range(3):  # Stripe retries
        asyncio.run(billing.handle_event(_event("invoice.payment_failed", _fee_invoice())))
    assert charged["payment_status"] == "failed"
    assert charged["payment_failed_at"] == 1  # grace clock starts at the first failure


@pytest.mark.parametrize("kind, status", [("invoice.paid", "paid"), ("invoice.voided", "void")])
def test_settling_the_fee_clears_the_failure(env, charged, kind, status):
    asyncio.run(billing.handle_event(_event("invoice.payment_failed", _fee_invoice())))
    asyncio.run(billing.handle_event(_event(kind, _fee_invoice())))
    assert (charged["payment_status"], charged["payment_failed_at"]) == (status, None)


def test_late_failure_event_does_not_reopen_a_paid_fee(env, charged):
    asyncio.run(billing.handle_event(_event("invoice.paid", _fee_invoice())))
    asyncio.run(billing.handle_event(_event("invoice.payment_failed", _fee_invoice())))
    assert charged["payment_status"] == "paid"


# ── Event routing ─────────────────────────────────────────────────────


def _event(kind, obj):
    return stripe.Event.construct_from(
        {"id": "evt_1", "object": "event", "type": kind, "data": {"object": obj}}, "sk_test_x"
    )


def _invoice(reason):
    return {
        "object": "invoice",
        "id": "in_renewal",
        "customer": "cus_1",
        "billing_reason": reason,
        "period_start": int(START.timestamp()),
        "period_end": int(END.timestamp()),
        "parent": {"subscription_details": {"subscription": "sub_1"}},
    }


def test_renewal_invoice_bills_the_period_that_ended(env, monkeypatch):
    charged = []

    async def charge(*args):
        charged.append(args)

    monkeypatch.setattr(billing, "charge_savings_fee", charge)
    asyncio.run(billing.handle_event(_event("invoice.created", _invoice("subscription_cycle"))))
    assert charged == [(UID, "cus_1", START, END, "pm_1")]


def test_first_invoice_is_not_a_fee_period(env, monkeypatch):
    charged = []

    async def charge(*args):
        charged.append(args)

    monkeypatch.setattr(billing, "charge_savings_fee", charge)
    asyncio.run(billing.handle_event(_event("invoice.created", _invoice("subscription_create"))))
    assert charged == []


# ── Plan sync from real Stripe object types ───────────────────────────


def _subscription(status, metadata, *, cancel_at=None, cancel_at_period_end=False):
    # construct_from yields the same StripeObject types the API returns.
    return stripe.Subscription.construct_from(
        {
            "id": "sub_1",
            "object": "subscription",
            "customer": "cus_1",
            "status": status,
            "cancel_at_period_end": cancel_at_period_end,
            "cancel_at": cancel_at,
            "metadata": metadata,
            "items": {
                "object": "list",
                "data": [
                    {
                        "object": "subscription_item",
                        "price": {"object": "price", "id": "price_pro"},
                        "current_period_start": int(START.timestamp()),
                        "current_period_end": int(END.timestamp()),
                    }
                ],
            },
        },
        "sk_test_x",
    )


@pytest.mark.parametrize("metadata", [{}, {"user_id": UID}])
@pytest.mark.parametrize("status, stored", [("active", "active"), ("trialing", "active"), ("past_due", "past_due")])
def test_sync_grants_pro_from_stripe_state(env, monkeypatch, metadata, status, stored):
    async def price_id(_):
        return "price_pro"

    monkeypatch.setattr(billing, "_price_id", price_id)
    env.stripe.v1.subscriptions.retrieve = lambda _id: _subscription(status, metadata)
    asyncio.run(billing.sync_subscription("sub_1"))
    assert env.conn.plan_updates == [(UID, "pro", stored, "sub_1", START, END, False, None)]


@pytest.mark.parametrize(
    "fields",
    [
        {"cancel_at": int(END.timestamp())},  # Customer Portal on newer API versions
        {"cancel_at_period_end": True},  # older style flag
    ],
)
def test_sync_records_scheduled_cancellation(env, monkeypatch, fields):
    async def price_id(_):
        return "price_pro"

    monkeypatch.setattr(billing, "_price_id", price_id)
    env.stripe.v1.subscriptions.retrieve = lambda _id: _subscription("active", {}, **fields)
    asyncio.run(billing.sync_subscription("sub_1"))
    # Still Pro until the cancellation date, which is recorded.
    assert env.conn.plan_updates == [(UID, "pro", "active", "sub_1", START, END, True, END)]


@pytest.mark.parametrize(
    "fields, update",
    [
        ({"cancel_at": int(END.timestamp())}, {"cancel_at": ""}),
        ({"cancel_at_period_end": True}, {"cancel_at_period_end": False}),
    ],
)
def test_renew_clears_the_scheduled_cancellation(env, monkeypatch, fields, update):
    async def price_id(_):
        return "price_pro"

    updates = []
    state = {"sub": _subscription("active", {}, **fields)}

    def apply(sub_id, params):
        updates.append(params)
        state["sub"] = _subscription("active", {})

    monkeypatch.setattr(billing, "_price_id", price_id)
    env.stripe.v1.subscriptions.retrieve = lambda _id: state["sub"]
    env.stripe.v1.subscriptions.update = apply
    asyncio.run(billing.renew_subscription("sub_1"))
    assert updates == [update]
    assert env.conn.plan_updates[-1] == (UID, "pro", "active", "sub_1", START, END, False, None)


def test_renew_refuses_an_ended_subscription(env):
    env.stripe.v1.subscriptions.retrieve = lambda _id: _subscription("canceled", {})
    with pytest.raises(billing.HTTPException) as exc:
        asyncio.run(billing.renew_subscription("sub_1"))
    assert exc.value.status_code == 409


# ── Unpaid fee past grace: downgrade to Free ──────────────────────────


def test_overdue_plan_is_tagged_cancelled_and_downgraded(env, monkeypatch):
    calls = []
    state = {"sub": _subscription("active", {})}

    def update(sub_id, params):
        calls.append(("update", params))

    def cancel(sub_id, params):
        calls.append(("cancel", params))
        state["sub"] = _subscription("canceled", {})

    env.conn.overdue = ["sub_1"]
    env.stripe.v1.subscriptions.retrieve = lambda _id: state["sub"]
    env.stripe.v1.subscriptions.update = update
    env.stripe.v1.subscriptions.cancel = cancel
    assert asyncio.run(billing.enforce_unpaid_fees()) == 1
    # Tagged before cancelling, so the deletion webhook skips the final fee.
    assert calls[0] == ("update", {"metadata": {"cancel_reason": "unpaid_savings_fee"}})
    assert calls[1][0] == "cancel" and calls[1][1]["prorate"] is False
    assert env.conn.ended == [(UID, "sub_1")]  # plan='free' locally right away


def _deleted(reason=None):
    sub = _subscription("canceled", {"cancel_reason": reason} if reason else {}).to_dict()
    sub.update(ended_at=int(END.timestamp()), canceled_at=int(END.timestamp()), default_payment_method=None)
    return sub


@pytest.mark.parametrize("reason, billed", [(None, True), ("unpaid_savings_fee", False)])
def test_final_fee_skipped_only_for_unpaid_fee_cancellations(env, monkeypatch, reason, billed):
    charged = []

    async def charge(*args):
        charged.append(args)

    monkeypatch.setattr(billing, "charge_savings_fee", charge)
    env.stripe.v1.subscriptions.retrieve = lambda _id: _subscription("canceled", {})
    asyncio.run(billing.handle_event(_event("customer.subscription.deleted", _deleted(reason))))
    assert bool(charged) is billed


def test_upgrade_blocked_until_the_unpaid_fee_is_paid(env, charged):
    asyncio.run(billing.handle_event(_event("invoice.payment_failed", _fee_invoice())))
    assert asyncio.run(billing.has_unpaid_fee(UID)) is True
    asyncio.run(billing.handle_event(_event("invoice.paid", _fee_invoice())))
    assert asyncio.run(billing.has_unpaid_fee(UID)) is False


# ── Webhook endpoint ──────────────────────────────────────────────────


def _signed(payload: bytes, secret: str) -> str:
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def test_webhook_verifies_signature_and_ignores_redelivery(monkeypatch):
    conn = FakeConn()
    handled = []

    async def handle(event):
        handled.append(event.id)

    monkeypatch.setattr(billing.db, "_conn", lambda: conn)
    monkeypatch.setattr(billing, "_client", stripe.StripeClient("sk_test_dummy"))
    monkeypatch.setattr(billing, "WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(billing, "handle_event", handle)
    app = FastAPI()
    billing.install(app, lambda: None)
    client = TestClient(app)
    payload = json.dumps(
        {"id": "evt_9", "object": "event", "type": "invoice.paid", "data": {"object": {}}}
    ).encode()

    bad = client.post("/stripe/webhook", content=payload, headers={"stripe-signature": "t=1,v1=00"})
    assert bad.status_code == 400
    for _ in range(2):
        ok = client.post(
            "/stripe/webhook",
            content=payload,
            headers={"stripe-signature": _signed(payload, "whsec_test")},
        )
        assert ok.status_code == 200
    assert handled == ["evt_9"]
