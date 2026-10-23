"""Team savings math and cycle snapshots, with no network or database access."""

import asyncio
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import billing  # noqa: E402
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
