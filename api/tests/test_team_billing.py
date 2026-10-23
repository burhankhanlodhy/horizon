"""Team savings math and cycle snapshots, with no network or database access."""

import asyncio
import copy
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import billing  # noqa: E402
import savings_ledger  # noqa: E402
import team_billing  # noqa: E402


def test_team_savings_fee_uses_one_combined_threshold():
    assert team_billing.savings_fee(Decimal("20.00")) == Decimal("0.00")
    assert team_billing.savings_fee(Decimal("30.00")) == Decimal("1.50")
    assert team_billing.savings_fee(Decimal("600.00")) == Decimal("30.00")


def test_cycle_aggregation_includes_service_usage_and_keeps_member_slices():
    owner, member = "membership-owner", "membership-member"
    user_owner, user_member = "user-owner", "user-member"

    total, member_sums, service_savings = team_billing.aggregate_cycle_rows(
        [
            {"membership_id": owner, "user_id": user_owner, "savings_usd": Decimal("100")},
            {"membership_id": member, "user_id": user_member, "savings_usd": Decimal("200")},
            # Shared service keys are part of the combined fee, with no
            # per-member allocation.
            {"membership_id": None, "user_id": None, "savings_usd": Decimal("300")},
            # A separate key for one member merges into the stable membership.
            {"membership_id": member, "user_id": user_member, "savings_usd": Decimal("25")},
        ]
    )
    assert total == Decimal("625.00")
    assert int(team_billing.savings_fee(total) * 100) == 3125
    assert service_savings == Decimal("300.00")
    assert {key: value[2] for key, value in member_sums.items()} == {
        owner: Decimal("100"),
        member: Decimal("225"),
    }


def test_renewal_draft_snapshot_and_fee_attachment_are_replay_safe(monkeypatch):
    """A duplicate invoice.created event reuses its frozen fee item."""
    class Tx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    class Conn:
        def __init__(self):
            self.cycle = None
            self.lock_available = True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def fetchval(self, sql, *args):
            if "pg_try_advisory_lock" in sql:
                return self.lock_available
            return None

        async def execute(self, sql, *args):
            if "INSERT INTO billing.team_cycles" in sql and self.cycle is None:
                self.cycle = {
                    "savings_usd": Decimal("0"),
                    "fee_cents": 0,
                    "stripe_invoice_id": args[3],
                    "reconciliation_state": "snapshot_pending",
                }
            elif "SET savings_usd=$3" in sql:
                self.cycle.update(savings_usd=args[2], fee_cents=args[3], reconciliation_state="frozen")
            elif "SET stripe_invoice_id=$3,reconciliation_state='attached'" in sql:
                self.cycle.update(stripe_invoice_id=args[2], reconciliation_state="attached")

        async def fetchrow(self, sql, *args):
            return dict(self.cycle)

        async def fetch(self, sql, *args):
            if "metrics.proxy_events" in sql:
                return [
                    {"membership_id": "member-a", "user_id": "user-a", "savings_usd": Decimal("100")},
                    {"membership_id": "member-b", "user_id": "user-b", "savings_usd": Decimal("200")},
                    {"membership_id": None, "user_id": None, "savings_usd": Decimal("300")},
                ]
            return []

        def transaction(self):
            return Tx()

    class Pool:
        def __init__(self, conn):
            self.conn = conn

        async def fetchval(self, sql, *args):
            if "SELECT team_id FROM core.team_subscriptions" in sql:
                return "team-a"
            return None

        def acquire(self):
            return self.conn

    class Items:
        def __init__(self):
            self.data = []

        async def list(self, _params):
            return SimpleNamespace(data=list(self.data))

        async def create(self, params, _options):
            item = SimpleNamespace(metadata=params["metadata"], amount=params["amount"])
            self.data.append(item)
            return item

    conn = Conn()
    items = Items()
    monkeypatch.setattr(team_billing, "db", SimpleNamespace(_conn=lambda: Pool(conn)))
    monkeypatch.setattr(team_billing, "_stripe", lambda: SimpleNamespace(v1=SimpleNamespace(invoice_items=items)))

    async def inline_call(fn, *args, **kwargs):
        return await fn(*args, **kwargs)

    monkeypatch.setattr(team_billing, "_call", inline_call)
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    end = datetime(2026, 10, 1, tzinfo=timezone.utc)
    invoice = SimpleNamespace(
        id="in_team_renewal",
        customer="cus_team",
        period_start=int(start.timestamp()),
        period_end=int(end.timestamp()),
    )

    asyncio.run(team_billing._handle_cycle_invoice(invoice, "sub_team"))
    asyncio.run(team_billing._handle_cycle_invoice(invoice, "sub_team"))
    monkeypatch.setattr(billing, "_invoice_subscription_id", lambda _invoice: "sub_team")
    event = SimpleNamespace(
        type="invoice.created",
        data=SimpleNamespace(object=SimpleNamespace(**vars(invoice), billing_reason="subscription_cycle")),
    )
    assert asyncio.run(team_billing.handle_event(event)) is True

    assert conn.cycle["savings_usd"] == Decimal("600.00")
    assert conn.cycle["fee_cents"] == 3000
    assert conn.cycle["reconciliation_state"] == "attached"
    assert len(items.data) == 1
    assert items.data[0].amount == 3000
    conn.lock_available = False
    with pytest.raises(HTTPException) as error:
        asyncio.run(team_billing.handle_event(event))
    assert error.value.status_code == 503
    assert len(items.data) == 1


def test_cancellation_settles_final_fee_once_without_seat_invoice(monkeypatch):
    class Tx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    class Conn:
        def __init__(self):
            self.cycle = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def fetchval(self, sql, *args):
            return True if "pg_try_advisory_lock" in sql else None

        async def execute(self, sql, *args):
            if "INSERT INTO billing.team_cycles" in sql and self.cycle is None:
                self.cycle = {
                    "savings_usd": Decimal("0"), "fee_cents": 0,
                    "stripe_invoice_id": None, "reconciliation_state": "snapshot_pending",
                }
            elif "SET savings_usd=$3" in sql:
                self.cycle.update(savings_usd=args[2], fee_cents=args[3], reconciliation_state="frozen")
            elif "SET stripe_invoice_id=$3,updated_at=now()" in sql:
                self.cycle["stripe_invoice_id"] = args[2]
            elif "SET reconciliation_state='attached'" in sql:
                self.cycle["reconciliation_state"] = "attached"

        async def fetchrow(self, sql, *args):
            return dict(self.cycle)

        async def fetch(self, sql, *args):
            if "metrics.proxy_events" in sql:
                return [{"membership_id": "member-a", "user_id": "user-a", "savings_usd": Decimal("100")}]
            return []

        def transaction(self):
            return Tx()

    class Pool:
        def __init__(self, conn):
            self.conn = conn

        def acquire(self):
            return self.conn

    class StripeObjects:
        def __init__(self):
            self.invoices = []
            self.items = []
            self.finalized = []
            self.payment_keys = []

        async def create_invoice(self, params, _options):
            invoice = SimpleNamespace(id=f"invoice-{len(self.invoices) + 1}", params=params)
            self.invoices.append(invoice)
            return invoice

        async def list_items(self, params):
            return SimpleNamespace(data=[i for i in self.items if i.invoice == params["invoice"]])

        async def create_item(self, params, _options):
            item = SimpleNamespace(metadata=params["metadata"], amount=params["amount"], invoice=params["invoice"])
            self.items.append(item)
            return item

        async def finalize(self, invoice_id, _params, _options):
            self.finalized.append(invoice_id)
            return SimpleNamespace(id=invoice_id)

        async def pay(self, invoice_id, _params, options):
            self.payment_keys.append((invoice_id, options["idempotency_key"]))
            return SimpleNamespace(id=invoice_id, status="paid")

    conn = Conn()
    objects = StripeObjects()
    monkeypatch.setattr(team_billing, "db", SimpleNamespace(_conn=lambda: Pool(conn), FEE_GRACE_DAYS=7))
    monkeypatch.setattr(
        team_billing,
        "_stripe",
        lambda: SimpleNamespace(v1=SimpleNamespace(
            invoices=SimpleNamespace(create=objects.create_invoice, finalize_invoice=objects.finalize, pay=objects.pay),
            invoice_items=SimpleNamespace(list=objects.list_items, create=objects.create_item),
        )),
    )

    async def inline_call(fn, *args, **kwargs):
        return await fn(*args, **kwargs)

    monkeypatch.setattr(team_billing, "_call", inline_call)
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    end = datetime(2026, 9, 15, tzinfo=timezone.utc)
    subscription = SimpleNamespace(
        items=SimpleNamespace(data=[SimpleNamespace(
            price=SimpleNamespace(lookup_key=team_billing.TEAM_PRICE_LOOKUP_KEY),
            current_period_start=int(start.timestamp()),
        )]),
        ended_at=int(end.timestamp()),
    )
    asyncio.run(team_billing._settle_final_cycle("team-a", subscription, "cus-team"))
    asyncio.run(team_billing._settle_final_cycle("team-a", subscription, "cus-team"))

    assert conn.cycle["fee_cents"] == 500
    assert conn.cycle["reconciliation_state"] == "attached"
    assert len(objects.invoices) == 1
    assert len(objects.items) == 1
    assert objects.items[0].amount == 500
    assert objects.finalized == ["invoice-1", "invoice-1"]
    assert objects.payment_keys == [
        ("invoice-1", f"team-final-team-a-{int(start.timestamp())}-pay"),
        ("invoice-1", f"team-final-team-a-{int(start.timestamp())}-pay"),
    ]


def test_late_usage_recalculates_the_full_threshold_fee_and_replay_finds_existing_item(monkeypatch):
    class Tx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    class Conn:
        def __init__(self, row):
            self.row = row
            self.fail_adjustment_once = True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def fetchval(self, sql, *args):
            if "pg_try_advisory_lock" in sql:
                return True
            if "metrics.proxy_events" in sql:
                return Decimal("30.00")
            return None

        async def fetch(self, sql, *args):
            if "FROM billing.team_cycles" in sql:
                return [dict(self.row)]
            return []

        async def execute(self, sql, *args):
            if "SET fee_adjustment_cents=fee_adjustment_cents+$3" in sql:
                if self.fail_adjustment_once:
                    self.fail_adjustment_once = False
                    raise RuntimeError("simulate database failure after Stripe accepted the item")
                self.row["fee_adjustment_cents"] += args[2]
                self.row["late_savings_usd"] = args[3]
                self.row["reconciliation_state"] = "settled"

        def transaction(self):
            return Tx()

    class Pool:
        def __init__(self, conn):
            self.conn = conn

        def acquire(self):
            return self.conn

    class Items:
        def __init__(self):
            self.data = []
            self.create_calls = 0

        async def list(self, params):
            return SimpleNamespace(data=[i for i in self.data if i.invoice == params["invoice"]])

        async def create(self, params, _options):
            self.create_calls += 1
            item = SimpleNamespace(metadata=params["metadata"], amount=params["amount"], invoice=params["invoice"])
            self.data.append(item)
            return item

    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = datetime(2026, 9, 1, tzinfo=timezone.utc)
    row = {
        "period_start": start,
        "period_end": end,
        "savings_usd": Decimal("20.00"),
        "fee_cents": 0,
        "fee_adjustment_cents": 0,
    }
    conn = Conn(row)
    items = Items()
    monkeypatch.setattr(team_billing, "db", SimpleNamespace(_conn=lambda: Pool(conn)))
    monkeypatch.setattr(team_billing, "_stripe", lambda: SimpleNamespace(v1=SimpleNamespace(invoice_items=items)))

    async def inline_call(fn, *args, **kwargs):
        return await fn(*args, **kwargs)

    monkeypatch.setattr(team_billing, "_call", inline_call)
    def reconcile():
        return asyncio.run(
            team_billing._reconcile_late_cycles("team-a", "invoice-next", "cus-team", end)
        )
    with pytest.raises(RuntimeError, match="simulate database failure"):
        reconcile()
    reconcile()
    reconcile()

    # The recalculated Team fee is 5% of the full $30, not 5% of a $10 late
    # event, and the metadata lookup prevents a second invoice item on retry.
    assert items.create_calls == 1
    assert items.data[0].amount == 150
    assert conn.row["fee_adjustment_cents"] == 150
    assert conn.row["late_savings_usd"] == Decimal("30.00")

def _checkout_handler(monkeypatch, row, session_store):
    from types import SimpleNamespace

    class Tx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    class Conn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def acquire(self):
            return self

        def transaction(self):
            return Tx()

        async def fetchrow(self, _sql, *_args):
            return row

        async def execute(self, sql, *args):
            if "SET stripe_customer_id=$2" in sql:
                row["stripe_customer_id"] = args[1]
            elif "SET stripe_checkout_session_id=NULL" in sql:
                row["stripe_checkout_session_id"] = None
                row["stripe_checkout_idempotency_key"] = args[1]
                row["checkout_seat_count"] = args[2]
            elif "SET stripe_checkout_session_id=$2" in sql:
                row["stripe_checkout_session_id"] = args[1]
                if "stripe_checkout_idempotency_key=$3" in sql:
                    row["stripe_checkout_idempotency_key"] = args[2]
                    row["checkout_seat_count"] = args[3]

    class Sessions:
        def __init__(self):
            self.created = []
            self.expired = []

        async def retrieve(self, session_id):
            return session_store[session_id]

        async def list(self, params):
            return SimpleNamespace(data=[s for s in session_store.values() if s.status == params["status"]])

        async def create(self, params, options):
            session = SimpleNamespace(
                id=f"checkout-{len(self.created) + 1}", status="open",
                url="https://checkout.test/session", metadata=params["metadata"],
            )
            self.created.append((params, options))
            session_store[session.id] = session
            return session

        async def expire(self, session_id):
            self.expired.append(session_id)
            session_store[session_id].status = "expired"

    class Customers:
        async def create(self, *_args):
            return SimpleNamespace(id="cus-team")

    sessions = Sessions()
    client = SimpleNamespace(v1=SimpleNamespace(checkout=SimpleNamespace(sessions=sessions), customers=Customers()))
    monkeypatch.setattr(team_billing, "ENABLED", True)
    monkeypatch.setattr(team_billing, "db", SimpleNamespace(_conn=lambda: Conn()))
    monkeypatch.setattr(team_billing, "_stripe", lambda: client)
    monkeypatch.setattr(team_billing, "_price_id", lambda: asyncio.sleep(0, result="price-team"))
    async def inline_call(fn, *args, **kwargs):
        return await fn(*args, **kwargs)
    monkeypatch.setattr(team_billing, "_call", inline_call)
    app = FastAPI()
    team_billing.install(app, lambda: None, lambda *_args, **_kwargs: None)
    return next(r.endpoint for r in app.routes if r.path == "/teams/{team_id}/billing/checkout"), sessions


def test_checkout_replay_reuses_open_session_and_stable_idempotency_key(monkeypatch):
    row = {
        "name": "Example", "status": "active", "stripe_customer_id": "cus-team", "stripe_subscription_id": None,
        "stripe_checkout_session_id": None, "stripe_checkout_idempotency_key": None,
        "checkout_seat_count": None,
    }
    sessions = {}
    handler, api = _checkout_handler(monkeypatch, row, sessions)
    body = team_billing.SeatChangeIn(seat_count=3)
    first = asyncio.run(handler("team-a", body, user={"user_id": "owner"}))
    key = api.created[0][1]["idempotency_key"]
    second = asyncio.run(handler("team-a", body, user={"user_id": "owner"}))
    assert first == second
    assert len(api.created) == 1
    assert row["stripe_checkout_session_id"] == "checkout-1"
    assert row["stripe_checkout_idempotency_key"] == key


def test_checkout_recovers_open_stripe_session_and_refuses_duplicate_subscription(monkeypatch):
    from types import SimpleNamespace

    row = {
        "name": "Example", "status": "active", "stripe_customer_id": "cus-team", "stripe_subscription_id": None,
        "stripe_checkout_session_id": None, "stripe_checkout_idempotency_key": None,
        "checkout_seat_count": None,
    }
    recovered = SimpleNamespace(
        id="orphaned", status="open", url="https://checkout.test/orphaned",
        metadata={"team_id": "team-a", "seat_count": "5", "checkout_idempotency_key": "persisted-key"},
    )
    handler, api = _checkout_handler(monkeypatch, row, {"orphaned": recovered})
    result = asyncio.run(handler("team-a", team_billing.SeatChangeIn(seat_count=5), user={"user_id": "owner"}))
    assert result["checkout_url"] == recovered.url
    assert row["stripe_checkout_session_id"] == "orphaned"
    assert api.created == []
    row["stripe_subscription_id"] = "sub-existing"
    with pytest.raises(HTTPException) as exc:
        asyncio.run(handler("team-a", team_billing.SeatChangeIn(seat_count=5), user={"user_id": "owner"}))
    assert exc.value.status_code == 409
    assert api.created == []


def test_checkout_expires_open_session_and_uses_new_key_when_seat_count_changes(monkeypatch):
    from types import SimpleNamespace

    previous = SimpleNamespace(
        id="old-session", status="open", url="https://checkout.test/old",
        metadata={"team_id": "team-a", "seat_count": "3", "checkout_idempotency_key": "old-key"},
    )
    row = {
        "name": "Example", "status": "active", "stripe_customer_id": "cus-team", "stripe_subscription_id": None,
        "stripe_checkout_session_id": "old-session", "stripe_checkout_idempotency_key": "old-key",
        "checkout_seat_count": 3,
    }
    handler, api = _checkout_handler(monkeypatch, row, {"old-session": previous})
    result = asyncio.run(handler("team-a", team_billing.SeatChangeIn(seat_count=5), user={"user_id": "owner"}))
    assert result["checkout_url"] == "https://checkout.test/session"
    assert api.expired == ["old-session"]
    assert api.created[0][0]["line_items"][0]["quantity"] == 5
    assert api.created[0][1]["idempotency_key"] != "old-key"

def test_checkout_can_restart_after_cancellation_but_waits_for_completed_checkout_webhook(monkeypatch):
    row = {
        "name": "Example", "status": "canceled", "stripe_customer_id": "cus-team", "stripe_subscription_id": "sub-old",
        "stripe_checkout_session_id": None, "stripe_checkout_idempotency_key": None,
        "checkout_seat_count": None,
    }
    handler, api = _checkout_handler(monkeypatch, row, {})
    result = asyncio.run(handler("team-a", team_billing.SeatChangeIn(seat_count=3), user={"user_id": "owner"}))
    assert result["checkout_url"] == "https://checkout.test/session"
    assert len(api.created) == 1

    completed = SimpleNamespace(
        id="done", status="complete", url=None,
        metadata={"team_id": "team-a", "seat_count": "3"},
    )
    row.update(
        status="active", stripe_subscription_id=None, stripe_checkout_session_id="done",
        stripe_checkout_idempotency_key="same-key", checkout_seat_count=3,
    )
    handler, api = _checkout_handler(monkeypatch, row, {"done": completed})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(handler("team-a", team_billing.SeatChangeIn(seat_count=3), user={"user_id": "owner"}))
    assert exc.value.status_code == 409
    assert api.created == []


# ------------------------------------------- fee eligibility, access and late fees
#
# The fakes below model only the statements these paths run. Writes made inside
# conn.transaction() roll back when the block raises, as Postgres does.

PERIOD_START = datetime(2026, 8, 1, tzinfo=timezone.utc)
PERIOD_END = datetime(2026, 9, 1, tzinfo=timezone.utc)
RECONCILE_BEFORE = datetime(2026, 10, 1, tzinfo=timezone.utc)
FEE_SQL = savings_ledger.FEE_ELIGIBLE_SAVINGS_USD


def _cycle(fee_cents=0, fee_adjustment_cents=0):
    return {
        "team_id": "team-a",
        "period_start": PERIOD_START,
        "period_end": PERIOD_END,
        "savings_usd": Decimal("0"),
        "fee_cents": fee_cents,
        "fee_adjustment_cents": fee_adjustment_cents,
        "late_savings_usd": Decimal("0"),
        "payment_status": "paid",
        "reconciliation_state": "attached",
    }


class _Tx:
    def __init__(self, ledger):
        self.ledger = ledger

    async def __aenter__(self):
        self.saved = (copy.deepcopy(self.ledger.cycle), copy.deepcopy(self.ledger.adjustments))
        return self

    async def __aexit__(self, exc_type, *_):
        if exc_type is not None:
            self.ledger.cycle, self.ledger.adjustments = self.saved
        return False


class _Conn:
    def __init__(self, ledger):
        self.ledger = ledger

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def transaction(self):
        return _Tx(self.ledger)

    async def fetchval(self, sql, *args):
        self.ledger.sql.append(sql)
        if "pg_try_advisory_lock" in sql:
            return True
        if "metrics.proxy_events" in sql:
            return self.ledger.savings
        if "count(*)" in sql:
            return sum(1 for a in self.ledger.adjustments if a["period_start"] == args[1])
        return None

    async def fetch(self, sql, *args):
        self.ledger.sql.append(sql)
        if "FROM billing.team_cycles" in sql:
            return [dict(self.ledger.cycle)]
        if "FROM billing.team_fee_adjustments" in sql:
            return [
                dict(a)
                for a in self.ledger.adjustments
                if a["status"] == "pending" and a["period_start"] == args[1]
            ]
        return []

    async def execute(self, sql, *args):
        ledger = self.ledger
        ledger.sql.append(sql)
        if sql.lstrip().startswith("INSERT INTO billing.team_fee_adjustments"):
            key, team_id, start, cents, invoice_id = args
            ledger.adjustments.append(
                {
                    "adjustment_key": key,
                    "team_id": team_id,
                    "period_start": start,
                    "cents": cents,
                    "stripe_invoice_id": invoice_id,
                    "status": "pending",
                }
            )
        elif "SET status='attached'" in sql:
            for a in ledger.adjustments:
                if a["adjustment_key"] == args[0]:
                    a["status"] = "attached"
        elif "SET stripe_invoice_id=$2" in sql:
            for a in ledger.adjustments:
                if a["adjustment_key"] == args[0]:
                    a["stripe_invoice_id"] = args[1]
        elif "SET fee_adjustment_cents=fee_adjustment_cents+$3" in sql:
            if ledger.fail_next_cycle_update:
                ledger.fail_next_cycle_update = False
                raise RuntimeError("simulate database failure after Stripe accepted the item")
            ledger.cycle["fee_adjustment_cents"] += args[2]
            ledger.cycle["late_savings_usd"] = args[3]
            ledger.cycle["reconciliation_state"] = "settled"
        elif "SET reconciliation_state='settled',late_savings_usd=$3" in sql:
            ledger.cycle["reconciliation_state"] = "settled"
            ledger.cycle["late_savings_usd"] = args[2]
        return None


class _Pool:
    """db._conn(): direct statements and acquire() both reach the same connection."""

    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return self.conn

    def __getattr__(self, name):
        return getattr(self.conn, name)


class _Ledger:
    def __init__(self, cycle, savings):
        self.cycle = cycle
        self.savings = Decimal(savings)
        self.adjustments = []
        self.sql = []
        self.fail_next_cycle_update = False
        self.conn = _Conn(self)
        self.pool = _Pool(self.conn)


class _Items:
    """Stripe invoice items. A repeated idempotency key returns the original item."""

    def __init__(self):
        self.items = []
        self.create_calls = 0
        self._by_key = {}

    def list(self, params):
        return SimpleNamespace(data=[i for i in self.items if i.invoice == params["invoice"]])

    def create(self, params, options=None):
        key = (options or {}).get("idempotency_key")
        if key and key in self._by_key:
            return self._by_key[key]
        self.create_calls += 1
        item = SimpleNamespace(invoice=params["invoice"], amount=params["amount"], metadata=params["metadata"])
        self.items.append(item)
        if key:
            self._by_key[key] = item
        return item

    def charged_cents(self):
        return sum(item.amount for item in self.items)


def _patch_late_fee(monkeypatch, ledger, items):
    monkeypatch.setattr(team_billing, "db", SimpleNamespace(_conn=lambda: ledger.pool, FEE_GRACE_DAYS=7))
    monkeypatch.setattr(team_billing, "_stripe", lambda: SimpleNamespace(v1=SimpleNamespace(invoice_items=items)))

    async def inline_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(team_billing, "_call", inline_call)


def _reconcile(invoice_id):
    asyncio.run(team_billing._reconcile_late_cycles("team-a", invoice_id, "cus-team", RECONCILE_BEFORE))


def test_late_fee_commit_failure_after_stripe_is_never_charged_twice(monkeypatch):
    ledger = _Ledger(_cycle(), savings="30.00")
    items = _Items()
    _patch_late_fee(monkeypatch, ledger, items)
    ledger.fail_next_cycle_update = True

    # Stripe accepts the item on inv-1, then the database write fails.
    with pytest.raises(RuntimeError, match="simulate database failure"):
        _reconcile("inv-1")
    assert items.charged_cents() == 150
    assert ledger.cycle["fee_adjustment_cents"] == 0
    assert [a["status"] for a in ledger.adjustments] == ["pending"]

    # The next renewal is a new invoice. The pending intent is found on inv-1, so
    # the delta is recorded as applied and not charged again.
    _reconcile("inv-2")
    assert items.create_calls == 1
    assert items.charged_cents() == 150
    assert all(item.invoice == "inv-1" for item in items.items)
    assert ledger.cycle["fee_adjustment_cents"] == 150
    assert ledger.adjustments[0]["status"] == "attached"

    _reconcile("inv-3")
    assert items.create_calls == 1
    assert items.charged_cents() == 150


def test_pending_intent_without_a_stripe_item_is_attached_to_the_current_invoice(monkeypatch):
    """A crash before the Stripe call leaves an intent with no item behind it."""
    ledger = _Ledger(_cycle(), savings="30.00")
    key = f"team-fee-adj-team-a-{int(PERIOD_START.timestamp())}-150-0"
    ledger.adjustments.append(
        {
            "adjustment_key": key,
            "team_id": "team-a",
            "period_start": PERIOD_START,
            "cents": 150,
            "stripe_invoice_id": "inv-1",
            "status": "pending",
        }
    )
    items = _Items()
    _patch_late_fee(monkeypatch, ledger, items)

    _reconcile("inv-2")

    assert items.create_calls == 1
    assert items.items[0].invoice == "inv-2"
    assert items.items[0].amount == 150
    assert items.items[0].metadata["team_cycle_key"] == key
    assert ledger.adjustments[0]["stripe_invoice_id"] == "inv-2"
    assert ledger.adjustments[0]["status"] == "attached"
    assert ledger.cycle["fee_adjustment_cents"] == 150


def test_normal_late_fee_is_one_item_on_the_current_invoice(monkeypatch):
    ledger = _Ledger(_cycle(), savings="30.00")
    items = _Items()
    _patch_late_fee(monkeypatch, ledger, items)

    _reconcile("inv-1")

    assert items.create_calls == 1
    item = items.items[0]
    assert item.invoice == "inv-1"
    assert item.amount == 150
    assert item.metadata["team_cycle_key"].startswith("team-fee-adj-team-a-")
    assert [a["status"] for a in ledger.adjustments] == ["attached"]
    assert ledger.cycle["fee_adjustment_cents"] == 150
    assert ledger.cycle["late_savings_usd"] == Decimal("30.00")
    assert ledger.cycle["reconciliation_state"] == "settled"

    _reconcile("inv-2")
    assert items.create_calls == 1


def test_late_fee_reduction_credits_back_and_a_later_increase_is_charged_again(monkeypatch):
    ledger = _Ledger(_cycle(fee_cents=150), savings="20.00")
    items = _Items()
    _patch_late_fee(monkeypatch, ledger, items)

    _reconcile("inv-1")
    assert [item.amount for item in items.items] == [-150]
    assert ledger.cycle["fee_adjustment_cents"] == -150

    ledger.savings = Decimal("30.00")
    _reconcile("inv-2")
    assert [item.amount for item in items.items] == [-150, 150]
    assert ledger.cycle["fee_adjustment_cents"] == 0
    # Each total gets its own key, so a value seen before is not mistaken for a replay.
    assert len({a["adjustment_key"] for a in ledger.adjustments}) == 2
    # The cycle's fee is now the 5% of $30 that the team owes.
    assert ledger.cycle["fee_cents"] + ledger.cycle["fee_adjustment_cents"] == 150


def test_team_fee_excludes_subscription_and_oauth_rows():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE proxy_events (team_id TEXT, occurred_at TEXT, data TEXT)")
    rows = [
        {"savings_usd": 10},
        {"savings_usd": 5, "auth_mode": "payg"},
        {"savings_usd": 100, "auth_mode": "oauth"},
        {"savings_usd": 200, "auth_mode": "subscription"},
        {"savings_usd": 300, "kind": "graph_estimate"},
    ]
    for data in rows:
        connection.execute("INSERT INTO proxy_events VALUES (?,?,?)", ("team-a", "2026-08-10", json.dumps(data)))
    connection.execute(
        "INSERT INTO proxy_events VALUES (?,?,?)", ("team-b", "2026-08-10", json.dumps({"savings_usd": 900}))
    )
    window = ("team-a", "2026-08-01", "2026-09-01")

    def total(expression):
        value = connection.execute(
            f"SELECT {expression} FROM proxy_events WHERE team_id=? AND occurred_at>=? AND occurred_at<?",
            window,
        ).fetchone()[0]
        return Decimal(str(value))

    # The old all-traffic total would charge 5% of $315; the fee total is $15.
    assert total(savings_ledger.SAVINGS_USD) == Decimal("315")
    assert total(FEE_SQL) == Decimal("15")
    assert team_billing.savings_fee(total(FEE_SQL)) == Decimal("0.00")


def test_team_cycle_freeze_and_late_reconciliation_use_the_fee_total(monkeypatch):
    ledger = _Ledger(_cycle(), savings="30.00")
    _patch_late_fee(monkeypatch, ledger, _Items())

    asyncio.run(team_billing._freeze_cycle(ledger.conn, "team-a", PERIOD_START, PERIOD_END))
    _reconcile("inv-1")

    freeze_sql = [s for s in ledger.sql if "metrics.proxy_events" in s and "GROUP BY" in s]
    proxy_sql = [s for s in ledger.sql if "metrics.proxy_events" in s]
    assert freeze_sql and all(FEE_SQL in s for s in freeze_sql)
    assert proxy_sql and all(FEE_SQL in s for s in proxy_sql)


def test_team_estimate_uses_the_fee_total(monkeypatch):
    ledger = _Ledger(_cycle(), savings="15.00")
    _patch_late_fee(monkeypatch, ledger, _Items())

    async def team_row(_team_id):
        return {
            "team_id": "team-a",
            "seat_count": 3,
            "current_period_start": PERIOD_START,
            "current_period_end": PERIOD_END,
        }

    monkeypatch.setattr(team_billing, "_team_row", team_row)
    result = asyncio.run(team_billing.estimate("team-a", {"membership_id": "m1", "team_role": "owner"}))

    assert result["savings_usd"] == 15.0
    assert result["savings_fee_usd"] == 0.0
    assert any(FEE_SQL in s for s in ledger.sql if "metrics.proxy_events" in s)


def test_personal_ledger_savings_skip_team_rows_and_subscription_logins():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE metrics_proxy_events (user_id TEXT, team_id TEXT, occurred_at TEXT, data TEXT)")
    rows = [
        ("account", None, {"savings_usd": 10}),
        ("account", "team-a", {"savings_usd": 900}),
        ("account", None, {"savings_usd": 500, "auth_mode": "oauth"}),
    ]
    for user_id, team_id, data in rows:
        connection.execute(
            "INSERT INTO metrics_proxy_events VALUES (?,?,?,?)",
            (user_id, team_id, "2026-08-10", json.dumps(data)),
        )

    class LedgerConnection:
        async def fetchval(self, sql, *args):
            # Translate the asyncpg placeholders in order for SQLite.
            return connection.execute(
                re.sub(r"\$\d", "?", sql).replace("metrics.proxy_events", "metrics_proxy_events"),
                args,
            ).fetchone()[0]

    total = asyncio.run(
        billing.ledger_savings("account", PERIOD_START, PERIOD_END, LedgerConnection())
    )
    assert total == Decimal("10")


@pytest.mark.parametrize(
    "row, expected",
    [
        ({"status": "active"}, True),
        ({"status": "trialing"}, True),
        ({"status": "past_due", "current_period_end": "now-1d"}, True),
        ({"status": "past_due", "current_period_end": "now-4d"}, False),
        ({"status": "past_due", "current_period_end": None}, False),
        ({"status": "unpaid", "current_period_end": "now"}, False),
        ({"status": "canceled"}, False),
        ({"status": "active", "overdue": True}, False),
        ({"status": "past_due", "current_period_end": "now-1d", "overdue": True}, False),
        (None, False),
    ],
)
def test_team_entitlement_matches_code_plan_access_rules(monkeypatch, row, expected):
    now = datetime.now(timezone.utc)
    if row is not None:
        row = dict({"current_period_end": None, "overdue": False}, **row)
        offsets = {"now": timedelta(0), "now-1d": timedelta(days=-1), "now-4d": timedelta(days=-4)}
        if isinstance(row["current_period_end"], str):
            row["current_period_end"] = now + offsets[row["current_period_end"]]

    class Conn:
        async def fetchrow(self, sql, *args):
            return row

    monkeypatch.setattr(team_billing, "db", SimpleNamespace(_conn=lambda: Conn(), FEE_GRACE_DAYS=7))
    assert asyncio.run(team_billing.entitlement("team-a")) is expected
