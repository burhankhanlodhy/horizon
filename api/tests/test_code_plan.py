"""ContextShrink Code against fakes: no Postgres, no Stripe, no network.

The in-memory database answers code_plan's statements by their SQL text and
fails on anything it does not recognise, so an uncovered statement is visible.

Run from the repo root with the API's requirements installed:
    python -m pytest api/tests
"""

import asyncio
import base64
import hashlib
import itertools
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import asyncpg
import pytest
import stripe
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import billing  # noqa: E402
import code_plan  # noqa: E402
import savings_ledger  # noqa: E402

ALICE = UUID("7b0c6c1e-9d6a-4c51-9a55-0d9f1f0a2b3c")
BOB = UUID("0f3e5a7c-1b2d-4e6f-8a9b-c0d1e2f3a4b5")
T0 = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
PERIOD_START = T0 - timedelta(days=10)
PERIOD_END = T0 + timedelta(days=20)
HASH_A, HASH_B, HASH_C = "a" * 64, "b" * 64, "c" * 64
HOUR = "2026-10-22T11:00:00+00:00"
SESSIONS = {"sess-alice": ALICE, "sess-bob": BOB}
_client_ips = itertools.count(1)


def _blank_plan() -> dict:
    return {
        "stripe_subscription_id": None,
        "status": None,
        "current_period_end": None,
        "cancel_at_period_end": False,
        "updated_at": T0,
    }


def _month_of(moment: datetime) -> date:
    return moment.replace(day=1).date()


class FakeDB:
    """Stands in for the asyncpg pool used by code_plan and billing's helpers."""

    def __init__(self, clock):
        self.clock = clock
        self.requests: dict = {}
        self.devices: dict = {}
        self.plans: dict = {}
        self.weeks: list[dict] = []
        self.usage: dict = {}
        self.customers: dict = {"cus_code": ALICE}
        self.subscription_writes: list[str] = []
        # Runs just before a free-week insert, to play a concurrent request's commit.
        self.before_week_insert = None

    def add_week(self, user_id, device_hash, started_at):
        """A committed free-week row, as another request would have written it."""
        row = {
            "user_id": user_id,
            "device_hash": device_hash,
            "month": _month_of(started_at),
            "started_at": started_at,
            "ends_at": started_at + timedelta(days=7),
        }
        self.weeks.append(row)
        return row

    # asyncpg pool/connection shape: acquire(), transaction() as async contexts.
    def acquire(self):
        return self

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def _device_by_id(self, device_id):
        return self.devices.get(device_id)

    async def fetchrow(self, sql, *args):
        if "FROM core.code_cli_requests WHERE user_code_hash" in sql:
            row = next((r for r in self.requests.values() if r["user_code_hash"] == args[0]), None)
            return dict(row) if row else None
        if "FROM core.code_cli_requests WHERE id=$1 FOR UPDATE" in sql:
            row = self.requests.get(args[0])
            return dict(row) if row else None
        if "FROM core.code_plans WHERE user_id" in sql:
            return dict(self.plans[args[0]]) if args[0] in self.plans else None
        if "FROM core.code_devices WHERE key_hash" in sql:
            row = next((d for d in self.devices.values() if d["key_hash"] == args[0]), None)
            return dict(row) if row else None
        if (
            "UPDATE core.code_devices SET revoked_at=COALESCE(revoked_at, now()) WHERE id=$1 AND user_id=$2"
            in sql
        ):
            device = self._device_by_id(args[0])
            if device is None or device["user_id"] != args[1]:
                return None
            device["revoked_at"] = device["revoked_at"] or self.clock.now
            return {"id": device["id"]}
        if "FROM metrics.code_usage_hourly" in sql:
            rows = [
                r for r in self.usage.values() if r["user_id"] == args[0] and r["hour"] >= args[1]
            ]
            return {
                "requests": sum(r["requests"] for r in rows),
                "tokens_before": sum(r["tokens_before"] for r in rows),
                "tokens_after": sum(r["tokens_after"] for r in rows),
            }
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        if "FROM core.code_devices WHERE user_id=$1 AND revoked_at IS NULL" in sql:
            rows = [
                d
                for d in self.devices.values()
                if d["user_id"] == args[0] and d["revoked_at"] is None
            ]
            return [dict(d) for d in sorted(rows, key=lambda d: d["created_at"])]
        if "FROM core.code_free_weeks WHERE user_id=$1" in sql:
            return [
                {k: w[k] for k in ("device_hash", "month", "started_at", "ends_at")}
                for w in self.weeks
                if w["user_id"] == args[0]
            ]
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        if "SELECT id FROM core.users WHERE id=$1 FOR UPDATE" in sql:
            return args[0]
        if "SELECT user_id FROM core.code_free_weeks WHERE device_hash=$1 AND month=$2" in sql:
            row = next((w for w in self.weeks if (w["device_hash"], w["month"]) == args), None)
            return row["user_id"] if row else None
        if "INSERT INTO core.code_free_weeks" in sql:
            user_id, device_hash, month, started, ends = args
            if self.before_week_insert is not None:
                hook, self.before_week_insert = self.before_week_insert, None
                hook()
            # Both UNIQUE constraints; ON CONFLICT DO NOTHING returns no row.
            taken = any(
                (w["user_id"] == user_id or w["device_hash"] == device_hash) and w["month"] == month
                for w in self.weeks
            )
            if taken:
                return None
            self.add_week(user_id, device_hash, started)
            return started
        if "INSERT INTO core.code_devices (user_id" in sql:
            device_id = uuid4()
            self.devices[device_id] = {
                "id": device_id,
                "user_id": args[0],
                "name": args[1],
                "platform": args[2],
                "device_hash": args[3],
                "tools": [args[4]],
                "key_hash": None,
                "created_at": self.clock.now,
                "last_seen_at": self.clock.now,
                "revoked_at": None,
            }
            return device_id
        if "UPDATE core.code_devices SET key_hash=$2" in sql:
            device = self._device_by_id(args[0])
            if device is None or device["revoked_at"] is not None:
                return None
            device["key_hash"] = args[1]
            return device["id"]
        if "SELECT 1 FROM core.code_plans WHERE stripe_subscription_id=$1" in sql:
            return (
                1
                if any(p["stripe_subscription_id"] == args[0] for p in self.plans.values())
                else None
            )
        if "SELECT user_id FROM core.subscriptions WHERE stripe_customer_id" in sql:
            return self.customers.get(args[0])
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        if "DELETE FROM core.code_cli_requests WHERE expires_at" in sql:
            for key in [k for k, r in self.requests.items() if r["expires_at"] < args[0]]:
                del self.requests[key]
        elif "INSERT INTO core.code_cli_requests (id, user_code_hash" in sql:
            (
                request_id,
                code_hash,
                secret_hash,
                tool,
                device_hash,
                name,
                platform,
                created,
                expires,
            ) = args
            if any(r["user_code_hash"] == code_hash for r in self.requests.values()):
                raise asyncpg.UniqueViolationError("duplicate user_code_hash")
            self.requests[request_id] = {
                "id": request_id,
                "user_code_hash": code_hash,
                "poll_secret_hash": secret_hash,
                "tool": tool,
                "device_hash": device_hash,
                "device_name": name,
                "platform": platform,
                "created_at": created,
                "expires_at": expires,
                "approved_user_id": None,
                "device_id": None,
                "consumed_at": None,
                "limit_user_id": None,
            }
        elif "UPDATE core.code_cli_requests SET limit_user_id" in sql:
            self.requests[args[0]]["limit_user_id"] = args[1]
        elif "UPDATE core.code_devices SET name=$2" in sql:
            device = self._device_by_id(args[0])
            device.update(
                name=args[1],
                platform=args[2],
                key_hash=None,
                last_seen_at=self.clock.now,
                tools=sorted(set(device["tools"]) | {args[3]}),
            )
        elif "UPDATE core.code_cli_requests SET approved_user_id" in sql:
            self.requests[args[0]].update(approved_user_id=args[1], device_id=args[2])
        elif "UPDATE core.code_cli_requests SET consumed_at" in sql:
            self.requests[args[0]]["consumed_at"] = self.clock.now
        elif "UPDATE core.code_devices SET last_seen_at=now() WHERE id=$1" in sql:
            self._device_by_id(args[0])["last_seen_at"] = self.clock.now
        elif (
            "UPDATE core.code_devices SET revoked_at=COALESCE(revoked_at, now()) WHERE id=$1" in sql
        ):
            device = self._device_by_id(args[0])
            device["revoked_at"] = device["revoked_at"] or self.clock.now
        elif "INSERT INTO core.code_plans (user_id, stripe_subscription_id" in sql:
            user_id, sub_id, status, period_end, cancel_flag = args
            plan = self.plans.setdefault(user_id, _blank_plan())
            # Mirrors the ON CONFLICT ... WHERE guard: stale subscriptions do not overwrite.
            current = plan["stripe_subscription_id"]
            if current in (None, sub_id) or plan["status"] in ("canceled", "incomplete_expired"):
                plan.update(
                    stripe_subscription_id=sub_id,
                    status=status,
                    current_period_end=period_end,
                    cancel_at_period_end=cancel_flag,
                    updated_at=self.clock.now,
                )
        elif "UPDATE core.subscriptions" in sql:
            self.subscription_writes.append(sql)
        else:
            raise AssertionError(sql)

    async def executemany(self, sql, rows):
        assert "INSERT INTO metrics.code_usage_hourly" in sql
        for user_id, device_id, hour, tool, requests, before, after in rows:
            self.usage[(device_id, hour, tool)] = {
                "user_id": user_id,
                "hour": hour,
                "requests": requests,
                "tokens_before": before,
                "tokens_after": after,
            }


class FakeStripe:
    def __init__(self):
        self.subs: dict[str, dict] = {}
        self.invoices: list[dict] = []
        # False: list ignores the subscription filter, as a defence check on code_plan.
        self.honor_invoice_filter = True
        self.calls: list[tuple] = []
        self.v1 = SimpleNamespace(
            subscriptions=SimpleNamespace(retrieve=self._retrieve, update=self._update),
            invoices=SimpleNamespace(list=self._invoices),
            checkout=SimpleNamespace(sessions=SimpleNamespace(create=self._checkout)),
            billing_portal=SimpleNamespace(sessions=SimpleNamespace(create=self._portal)),
        )

    def _retrieve(self, sub_id):
        return stripe.Subscription.construct_from(self.subs[sub_id], "sk_test_x")

    def _update(self, sub_id, params, options=None):
        self.calls.append(("subscription.update", sub_id, params, options))
        self.subs[sub_id] = {**self.subs[sub_id], **params}
        return stripe.Subscription.construct_from(self.subs[sub_id], "sk_test_x")

    def _invoices(self, params, options=None):
        self.calls.append(("invoices.list", params))
        wanted = params.get("subscription")
        rows = [
            inv
            for inv in self.invoices
            if not self.honor_invoice_filter or _invoice_sub(inv) == wanted
        ]
        data = [stripe.Invoice.construct_from(inv, "sk_test_x") for inv in rows]
        return SimpleNamespace(data=data[: params.get("limit", 10)])

    def _checkout(self, params):
        self.calls.append(("checkout", params))
        return SimpleNamespace(url="https://checkout.test/cs_1")

    def _portal(self, params):
        self.calls.append(("portal", params))
        return SimpleNamespace(url="https://billing.test/portal")


@pytest.fixture
def env(monkeypatch):
    clock = SimpleNamespace(now=T0)
    db = FakeDB(clock)
    stripe_fake = FakeStripe()
    signer = Ed25519PrivateKey.generate()
    seed = signer.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    monkeypatch.setattr(code_plan, "_now", lambda: clock.now)
    monkeypatch.setattr(code_plan.db, "_conn", lambda: db)
    monkeypatch.setattr(code_plan, "_start_hits", {})
    monkeypatch.setattr(code_plan, "ENTITLEMENT_PRIVATE_KEY_B64", base64.b64encode(seed).decode())
    monkeypatch.setattr(billing.db, "_conn", lambda: db)
    monkeypatch.setattr(billing, "_client", stripe_fake)

    def boom(*_args, **_kwargs):
        raise AssertionError("savings fee path reached for a code event")

    monkeypatch.setattr(billing, "charge_savings_fee", boom)
    monkeypatch.setattr(billing, "record_fee_payment", boom)
    return SimpleNamespace(
        db=db,
        clock=clock,
        stripe=stripe_fake,
        public_key=signer.public_key(),
        monkeypatch=monkeypatch,
    )


async def fake_current_user(authorization: str | None = Header(default=None)):
    token = (authorization or "").removeprefix("Bearer ").strip()
    if token not in SESSIONS:
        raise HTTPException(401, "Invalid or expired session")
    user_id = SESSIONS[token]
    return {"user_id": user_id, "email": f"{token}@test.invalid", "name": "Test"}


@pytest.fixture
def client(env):
    app = FastAPI()
    code_plan.install(app, fake_current_user)
    with TestClient(app) as c:
        yield c


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def start(client, device_hash=HASH_A, name="Laptop", *, tool="claude", platform="windows", ip=None):
    n = next(_client_ips)
    return client.post(
        "/code/cli/start",
        json={"tool": tool, "device_hash": device_hash, "device_name": name, "platform": platform},
        headers={"cf-connecting-ip": ip or f"10.0.{n // 250}.{n % 250 + 1}"},
    )


def approve(client, user_code, session="sess-alice"):
    return client.post("/code/cli/approve", json={"user_code": user_code}, headers=auth(session))


def poll(client, started):
    return client.post(
        "/code/cli/poll",
        json={"request_id": started["request_id"], "poll_secret": started["poll_secret"]},
    )


def connect(client, device_hash=HASH_A, name="Laptop", *, session="sess-alice"):
    """Run the whole npx login for one computer; returns (device key, device id)."""
    started = start(client, device_hash, name).json()
    assert approve(client, started["user_code"], session).status_code == 200
    polled = poll(client, started).json()
    assert polled["status"] == "approved"
    return polled["device_key"], polled["device_id"]


def entitlement(client, key, device_hash=HASH_A):
    return client.post("/code/entitlement", json={"device_hash": device_hash}, headers=auth(key))


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def bucket(hour=HOUR, **extra):
    return {
        "hour": hour,
        "tool": "claude",
        "requests": 3,
        "tokens_before": 1000,
        "tokens_after": 400,
        **extra,
    }


# ------------------------------------------------------------ device limit


def test_two_computers_connect_and_a_third_is_refused_with_the_list(client):
    connect(client, HASH_A, "Laptop")
    connect(client, HASH_B, "Desktop")
    started = start(client, HASH_C, "Work PC").json()

    shown = client.get(
        "/code/cli/request", params={"code": started["user_code"]}, headers=auth("sess-alice")
    )
    assert shown.status_code == 200
    assert shown.json()["device_limit"]["reached"] is True
    assert [d["name"] for d in shown.json()["device_limit"]["devices"]] == ["Laptop", "Desktop"]

    refused = approve(client, started["user_code"])
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["code"] == "device_limit"
    assert [d["name"] for d in detail["devices"]] == ["Laptop", "Desktop"]
    assert set(detail["devices"][0]) == {"id", "name", "platform", "last_seen_at"}
    # The terminal learns why it is waiting, with the same devices.
    polled = poll(client, started).json()
    assert polled["status"] == "device_limit"
    assert [d["name"] for d in polled["devices"]] == ["Laptop", "Desktop"]
    assert set(polled["devices"][0]) == {"id", "name", "platform", "last_seen_at"}


def test_device_limit_poll_ends_when_the_browser_approves_after_a_disconnect(client):
    connect(client, HASH_A, "Laptop")
    connect(client, HASH_B, "Desktop")
    started = start(client, HASH_C, "Work PC").json()
    assert approve(client, started["user_code"]).status_code == 409
    assert poll(client, started).json()["status"] == "device_limit"

    account_devices = client.get("/code/account", headers=auth("sess-alice")).json()["devices"]
    laptop = next(d for d in account_devices if d["name"] == "Laptop")
    revoked = client.delete(f"/code/devices/{laptop['id']}", headers=auth("sess-alice"))
    assert revoked.status_code == 204
    assert approve(client, started["user_code"]).status_code == 200

    polled = poll(client, started).json()
    assert polled["status"] == "approved"
    assert polled["device_key"].startswith("cs_dev_")
    assert poll(client, started).json() == {"status": "expired"}  # the key is handed over once


def test_device_limit_poll_expires_with_the_request(client, env):
    connect(client, HASH_A, "Laptop")
    connect(client, HASH_B, "Desktop")
    started = start(client, HASH_C, "Work PC").json()
    assert approve(client, started["user_code"]).status_code == 409
    env.clock.now = T0 + timedelta(seconds=601)
    assert poll(client, started).json() == {"status": "expired"}


def test_a_request_nobody_refused_polls_as_pending(client):
    # Only a refused approval leaves the device_limit marker; a plain request stays pending.
    started = start(client, HASH_C, "Work PC").json()
    assert poll(client, started).json() == {"status": "pending"}


def test_same_computer_reuses_its_slot_and_its_old_key_stops_working(client):
    old_key, device_id = connect(client, HASH_A, "Laptop")
    connect(client, HASH_B, "Desktop")
    new_key, reused_id = connect(client, HASH_A, "Laptop")

    assert reused_id == device_id and new_key != old_key
    assert entitlement(client, old_key).status_code == 401
    assert entitlement(client, new_key).status_code == 200
    assert len(client.get("/code/account", headers=auth("sess-alice")).json()["devices"]) == 2


def test_uninstall_frees_the_slot_and_is_idempotent(client):
    key_a, _ = connect(client, HASH_A, "Laptop")
    connect(client, HASH_B, "Desktop")
    assert client.delete("/code/device", headers=auth(key_a)).status_code == 204
    assert client.delete("/code/device", headers=auth(key_a)).status_code == 204
    connect(client, HASH_C, "Work PC")  # fits now
    assert entitlement(client, key_a).status_code == 401


def test_account_page_revoke_frees_the_slot_and_hides_other_accounts_devices(client):
    _, device_a = connect(client, HASH_A, "Laptop")
    connect(client, HASH_B, "Desktop")
    assert client.delete(f"/code/devices/{device_a}", headers=auth("sess-bob")).status_code == 404
    assert client.delete(f"/code/devices/{device_a}", headers=auth("sess-alice")).status_code == 204
    connect(client, HASH_C, "Work PC")
    names = [
        d["name"] for d in client.get("/code/account", headers=auth("sess-alice")).json()["devices"]
    ]
    assert names == ["Desktop", "Work PC"]


# ------------------------------------------------------------------ trial


def _state(client, session="sess-alice") -> dict:
    return client.get("/code/account", headers=auth(session)).json()["plan"]


def test_approval_starts_no_week_and_the_account_is_free(client, env):
    connect(client, HASH_A)
    assert env.db.weeks == []
    account = client.get("/code/account", headers=auth("sess-alice")).json()
    assert account["device_limit"] == 2
    assert account["plan"] == {
        "state": "free",
        "free_week": {
            "status": "available",
            "started_at": None,
            "ends_at": None,
            "renews_at": T0.isoformat(),
        },
        "current_period_end": None,
        "cancel_at_period_end": False,
        "entitled": False,
        "price_usd": 5,
    }


def test_account_plan_object_has_the_documented_fields(client, env):
    connect(client, HASH_A)
    env.db.plans[ALICE] = {
        **_blank_plan(),
        "stripe_subscription_id": "sub_code_1",
        "status": "active",
        "current_period_end": PERIOD_END,
        "cancel_at_period_end": True,
    }
    assert _state(client) == {
        "state": "active",
        "free_week": {
            "status": "available",
            "started_at": None,
            "ends_at": None,
            "renews_at": T0.isoformat(),
        },
        "current_period_end": PERIOD_END.isoformat(),
        "cancel_at_period_end": True,
        "entitled": True,
        "price_usd": 5,
    }


# ------------------------------------------------------------- free week

WEEK = timedelta(days=7)
NOV_1 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def test_first_use_starts_the_week_and_the_note_runs_to_its_end(client, env):
    key, _ = connect(client, HASH_A)
    response = entitlement(client, key)
    assert response.status_code == 200
    body = response.json()
    ends = int((T0 + WEEK).timestamp())
    assert body["plan"] == "trial"
    assert body["until"] == ends and body["free_week_ends_at"] == ends
    _, _, fields = _note(body["token"])
    assert fields["plan"] == "trial" and fields["until"] == ends
    assert env.db.weeks == [
        {
            "user_id": ALICE,
            "device_hash": HASH_A,
            "month": date(2026, 10, 1),
            "started_at": T0,
            "ends_at": T0 + WEEK,
        }
    ]
    plan = _state(client)
    assert plan["state"] == "free" and plan["entitled"] is True
    assert plan["free_week"] == {
        "status": "active",
        "started_at": T0.isoformat(),
        "ends_at": (T0 + WEEK).isoformat(),
        "renews_at": NOV_1.isoformat(),
    }


def test_a_second_device_of_the_account_shares_the_week(client, env):
    key_a, _ = connect(client, HASH_A, "Laptop")
    key_b, _ = connect(client, HASH_B, "Desktop")
    first = entitlement(client, key_a).json()
    env.clock.now += timedelta(days=2)
    second = entitlement(client, key_b, HASH_B)
    assert second.status_code == 200
    assert second.json()["until"] == first["until"] == int((T0 + WEEK).timestamp())
    assert len(env.db.weeks) == 1


def test_week_ends_after_seven_days_and_the_next_one_renews_on_the_first(client, env):
    key, _ = connect(client, HASH_A)
    assert entitlement(client, key).status_code == 200
    env.clock.now = T0 + WEEK  # the week ends exactly now
    response = entitlement(client, key)
    assert response.status_code == 402
    assert response.json()["detail"] == {
        "code": "plan_required",
        "free_week_renews_at": NOV_1.isoformat(),
        "free_week_reason": "used",
    }
    assert _state(client)["free_week"]["renews_at"] == NOV_1.isoformat()


def test_next_month_starts_a_new_week_at_its_first_use(client, env):
    key, _ = connect(client, HASH_A)
    entitlement(client, key)
    env.clock.now = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
    response = entitlement(client, key)
    assert response.status_code == 200
    assert response.json()["until"] == int((env.clock.now + WEEK).timestamp())
    assert [w["month"] for w in env.db.weeks] == [date(2026, 10, 1), date(2026, 11, 1)]


def test_a_week_started_on_the_28th_runs_into_next_month_and_that_month_waits_for_it(client, env):
    key_a, _ = connect(client, HASH_A, "Laptop")
    key_b, _ = connect(client, HASH_B, "Desktop")
    spill_end = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
    env.clock.now = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
    assert entitlement(client, key_a).json()["until"] == int(spill_end.timestamp())

    env.clock.now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    # Still inside the week: another device shares it, and no November week starts.
    assert entitlement(client, key_b, HASH_B).json()["until"] == int(spill_end.timestamp())
    assert len(env.db.weeks) == 1
    plan = _state(client)
    assert plan["free_week"]["status"] == "active"
    assert plan["free_week"]["renews_at"] == spill_end.isoformat()

    env.clock.now = spill_end - timedelta(minutes=1)
    assert entitlement(client, key_b, HASH_B).status_code == 200
    # The moment it ends, November's week may start.
    env.clock.now = spill_end
    response = entitlement(client, key_b, HASH_B)
    assert response.status_code == 200
    assert response.json()["until"] == int((spill_end + WEEK).timestamp())
    assert [w["month"] for w in env.db.weeks] == [date(2026, 10, 1), date(2026, 11, 1)]


def test_a_computer_gets_one_free_week_per_month_across_accounts(client, env):
    key_alice, _ = connect(client, HASH_A, session="sess-alice")
    assert entitlement(client, key_alice).status_code == 200
    key_bob, _ = connect(client, HASH_A, "Laptop", session="sess-bob")
    env.clock.now += timedelta(days=8)  # still October, and Alice's week is over
    response = entitlement(client, key_bob)
    assert response.status_code == 402
    assert response.json()["detail"] == {
        "code": "plan_required",
        "free_week_renews_at": NOV_1.isoformat(),
        "free_week_reason": "device_used",
    }
    assert len(env.db.weeks) == 1

    env.clock.now = NOV_1 + timedelta(hours=9)
    assert entitlement(client, key_bob).status_code == 200  # next month: Bob's own week
    assert env.db.weeks[-1]["user_id"] == BOB


def test_a_running_week_does_not_carry_a_computer_another_account_used_this_month(client, env):
    key_bob, _ = connect(client, HASH_B, session="sess-bob")
    assert entitlement(client, key_bob, HASH_B).status_code == 200
    key_alice, _ = connect(client, HASH_A, session="sess-alice")
    env.clock.now += timedelta(hours=1)
    assert entitlement(client, key_alice, HASH_A).status_code == 200
    key_alice_desktop, _ = connect(client, HASH_B, "Desktop", session="sess-alice")
    env.clock.now += timedelta(hours=1)
    response = entitlement(client, key_alice_desktop, HASH_B)
    assert response.status_code == 402
    assert response.json()["detail"]["free_week_reason"] == "device_used"


def test_reconnecting_a_computer_does_not_restart_the_months_week(client, env):
    key, device_id = connect(client, HASH_A)
    assert entitlement(client, key).status_code == 200
    env.clock.now += timedelta(days=8)  # still October, and the week is over
    client.delete(f"/code/devices/{device_id}", headers=auth("sess-alice"))
    new_key, _ = connect(client, HASH_A)  # same computer, new device row
    response = entitlement(client, new_key)
    assert response.status_code == 402
    assert response.json()["detail"]["free_week_reason"] == "used"
    assert entitlement(client, key).status_code == 401
    assert len(env.db.weeks) == 1


def test_a_paid_account_never_starts_or_uses_a_free_week(client, env):
    key_bob, _ = connect(client, HASH_B, session="sess-bob")
    assert entitlement(client, key_bob, HASH_B).status_code == 200  # Bob used the computer's week
    key, _ = connect(client, HASH_B, "Desktop")
    env.db.plans[ALICE] = {
        **_blank_plan(),
        "stripe_subscription_id": "sub_code_1",
        "status": "active",
        "current_period_end": PERIOD_END,
    }
    body = entitlement(client, key, HASH_B).json()
    assert body["plan"] == "active"
    assert body["until"] == int((PERIOD_END + code_plan.GRACE).timestamp())
    assert body["free_week_ends_at"] is None
    assert [w["user_id"] for w in env.db.weeks] == [BOB]
    assert _state(client)["free_week"]["status"] == "available"


def test_schema_migration_copies_this_months_trials_into_free_weeks():
    # No Postgres runs here, so this checks the migration statement itself.
    schema = (Path(__file__).resolve().parents[1] / "schema.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS core.code_free_weeks" in schema
    migration = " ".join(schema[schema.index("INSERT INTO core.code_free_weeks") :].split())
    assert (
        "FROM core.code_plans p JOIN core.code_trial_devices t ON t.user_id = p.user_id"
        in migration
    )
    assert "WHERE p.trial_started_at IS NOT NULL AND p.trial_ends_at IS NOT NULL" in migration
    assert (
        "date_trunc('month', p.trial_started_at AT TIME ZONE 'UTC') = "
        "date_trunc('month', now() AT TIME ZONE 'UTC')" in migration
    )
    assert migration.endswith("ON CONFLICT DO NOTHING;")


def test_concurrent_first_use_from_two_devices_of_one_account_starts_one_week(client, env):
    key_a, _ = connect(client, HASH_A, "Laptop")
    key_b, _ = connect(client, HASH_B, "Desktop")
    # Device A's request commits its week after device B has read the account's weeks.
    env.db.before_week_insert = lambda: env.db.add_week(ALICE, HASH_A, T0)
    response = entitlement(client, key_b, HASH_B)
    assert response.status_code == 200
    assert response.json()["until"] == int((T0 + WEEK).timestamp())
    assert len(env.db.weeks) == 1
    assert entitlement(client, key_a, HASH_A).json()["until"] == int((T0 + WEEK).timestamp())


def test_concurrent_first_use_of_one_computer_by_two_accounts_starts_one_week(client, env):
    key_alice, _ = connect(client, HASH_A, session="sess-alice")
    env.db.before_week_insert = lambda: env.db.add_week(BOB, HASH_A, T0)
    response = entitlement(client, key_alice, HASH_A)
    assert response.status_code == 402
    assert response.json()["detail"]["free_week_reason"] == "device_used"
    assert [w["user_id"] for w in env.db.weeks] == [BOB]


# ------------------------------------------------------------ entitlement


def _note(token: str) -> tuple[bytes, bytes, dict]:
    payload_b64, sig_b64 = token.split(".")
    payload = b64url_decode(payload_b64)
    return payload, b64url_decode(sig_b64), json.loads(payload)


def test_entitled_device_gets_a_note_that_verifies_with_the_public_key(client, env):
    key, device_id = connect(client, HASH_A)
    response = entitlement(client, key)
    assert response.status_code == 200
    body = response.json()
    assert body["plan"] == "trial"
    payload, signature, fields = _note(body["token"])
    env.public_key.verify(signature, payload)  # raises InvalidSignature when wrong
    iat = int(T0.timestamp())
    assert fields == {
        "v": 1,
        "iss": "contextshrink-code",
        "sub": str(ALICE),
        "dev": str(device_id),
        "dh": HASH_A,
        "plan": "trial",
        "iat": iat,
        "exp": iat + 72 * 3600,
        "until": int((T0 + timedelta(days=7)).timestamp()),
    }
    assert body["until"] == fields["until"]


def test_note_expiry_is_capped_by_the_week_end(client, env):
    key, _ = connect(client, HASH_A)
    entitlement(client, key)  # starts the week
    env.clock.now = T0 + WEEK - timedelta(hours=1)
    _, _, fields = _note(entitlement(client, key).json()["token"])
    assert fields["until"] == int((T0 + WEEK).timestamp())
    assert fields["exp"] == fields["until"] < fields["iat"] + 72 * 3600


def test_paid_note_runs_to_period_end_plus_three_days(client, env):
    key, _ = connect(client, HASH_A)
    env.db.plans[ALICE] = {
        **_blank_plan(),
        "stripe_subscription_id": "sub_code_1",
        "status": "active",
        "current_period_end": PERIOD_END,
    }
    body = entitlement(client, key).json()
    _, _, fields = _note(body["token"])
    assert body["plan"] == fields["plan"] == "active"
    assert fields["until"] == int((PERIOD_END + timedelta(days=3)).timestamp())
    assert fields["exp"] == fields["iat"] + 72 * 3600


def test_past_due_keeps_the_paid_note_only_inside_the_grace_period(client, env):
    key, _ = connect(client, HASH_A)
    env.db.plans[ALICE] = {
        **_blank_plan(),
        "stripe_subscription_id": "sub_code_1",
        "status": "past_due",
        "current_period_end": T0 - timedelta(days=1),
    }
    assert entitlement(client, key).json()["plan"] == "active"
    env.clock.now = T0 + timedelta(days=3)  # grace is over, so the free week applies
    later = entitlement(client, key).json()
    assert later["plan"] == "trial"
    assert later["until"] == int((T0 + timedelta(days=10)).timestamp())


def test_a_used_week_is_refused_with_the_renewal_and_no_trial_fields(client, env):
    key, _ = connect(client, HASH_A)
    env.db.add_week(ALICE, HASH_A, T0 - timedelta(days=21))  # this month's week, already over
    response = entitlement(client, key)
    assert response.status_code == 402
    assert response.json()["detail"] == {
        "code": "plan_required",
        "free_week_renews_at": NOV_1.isoformat(),
        "free_week_reason": "used",
    }


def test_device_hash_must_match_the_key(client):
    key, _ = connect(client, HASH_A)
    assert entitlement(client, key, device_hash=HASH_B).status_code == 403


def test_revoked_device_is_401_on_entitlement_and_usage(client):
    key, device_id = connect(client, HASH_A)
    client.delete(f"/code/devices/{device_id}", headers=auth("sess-alice"))
    assert entitlement(client, key).status_code == 401
    assert client.post("/code/usage", json={"buckets": []}, headers=auth(key)).status_code == 401


def test_missing_or_bad_signing_key_is_503(client, env):
    key, _ = connect(client, HASH_A)
    env.monkeypatch.setattr(code_plan, "ENTITLEMENT_PRIVATE_KEY_B64", "")
    assert entitlement(client, key).status_code == 503
    env.monkeypatch.setattr(code_plan, "ENTITLEMENT_PRIVATE_KEY_B64", "not-a-32-byte-seed")
    assert entitlement(client, key).status_code == 503


@pytest.mark.parametrize(
    "plan, expected_state, entitled",
    [
        ({}, "free", False),
        ({"status": "active", "stripe_subscription_id": "s"}, "active", True),
        # Inside the three-day grace after the period ended, then not.
        ({"status": "past_due", "current_period_end": T0 - timedelta(days=1)}, "past_due", True),
        ({"status": "past_due", "current_period_end": T0 - timedelta(days=4)}, "free", False),
        # A cancelled subscription is no live paid subscription: the account is free.
        ({"status": "canceled", "stripe_subscription_id": "s"}, "free", False),
    ],
)
def test_account_plan_state(client, env, plan, expected_state, entitled):
    env.db.plans[ALICE] = {**_blank_plan(), **plan}
    body = client.get("/code/account", headers=auth("sess-alice")).json()
    assert body["plan"]["state"] == expected_state
    assert body["plan"]["entitled"] is entitled


# -------------------------------------------------------------- CLI flow


def test_connect_flow_hands_the_key_over_once_and_stores_only_hashes(client, env):
    started = start(client, HASH_A, "Laptop").json()
    code = started["user_code"]
    assert re.fullmatch(
        r"[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{4}-[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{4}", code
    )
    assert started["expires_in"] == 600 and started["interval"] == 3
    assert started["verify_url"] == f"https://code.contextshrink.com/connect?code={code}"
    assert poll(client, started).json() == {"status": "pending"}

    shown = client.get(
        "/code/cli/request", params={"code": code.lower()}, headers=auth("sess-alice")
    ).json()
    assert shown["device_name"] == "Laptop" and shown["tool"] == "claude"
    assert shown["device_limit"]["reached"] is False

    assert approve(client, code).json() == {"status": "approved"}
    polled = poll(client, started).json()
    assert polled["status"] == "approved"
    assert polled["device_key"].startswith("cs_dev_")
    assert polled["api_url"] == "https://api.contextshrink.com"
    assert poll(client, started).json() == {"status": "expired"}  # key is not handed out twice
    assert "device_key" not in poll(client, started).json()

    stored = repr(env.db.devices) + repr(env.db.requests)
    assert polled["device_key"] not in stored
    assert started["poll_secret"] not in stored and code not in stored
    device = next(iter(env.db.devices.values()))
    assert device["key_hash"] == hashlib.sha256(polled["device_key"].encode()).hexdigest()


def test_pending_request_expires_after_ten_minutes(client, env):
    started = start(client, HASH_A).json()
    env.clock.now = T0 + timedelta(seconds=601)
    assert poll(client, started).json() == {"status": "expired"}
    assert approve(client, started["user_code"]).status_code == 404
    shown = client.get(
        "/code/cli/request", params={"code": started["user_code"]}, headers=auth("sess-alice")
    )
    assert shown.status_code == 404


def test_unknown_malformed_and_foreign_codes_are_404(client):
    started = start(client, HASH_A).json()
    assert approve(client, "ABCD-EFGH").status_code == 404
    assert approve(client, "0000-0000").status_code == 404  # 0 is not in the alphabet
    assert approve(client, started["user_code"], session="sess-bob").status_code == 200
    assert approve(client, started["user_code"], session="sess-alice").status_code == 404


def test_double_click_approval_is_harmless(client):
    started = start(client, HASH_A).json()
    assert approve(client, started["user_code"]).status_code == 200
    assert approve(client, started["user_code"]).status_code == 200
    assert poll(client, started).json()["status"] == "approved"


def test_wrong_poll_secret_is_404(client):
    started = start(client, HASH_A).json()
    response = client.post(
        "/code/cli/poll", json={"request_id": started["request_id"], "poll_secret": "x" * 30}
    )
    assert response.status_code == 404


def test_user_code_collision_draws_another_code(client, env, monkeypatch):
    first = start(client, HASH_A).json()["user_code"]
    codes = iter([first, "ABCD-EFGH"])
    monkeypatch.setattr(code_plan, "_new_user_code", lambda: next(codes))
    assert start(client, HASH_B, "Desktop").json()["user_code"] == "ABCD-EFGH"


def test_connect_start_is_rate_limited_per_client(client):
    for _ in range(10):
        assert start(client, ip="203.0.113.7").status_code == 200
    assert start(client, ip="203.0.113.7").status_code == 429
    assert start(client, ip="203.0.113.8").status_code == 200


# ----------------------------------------------------------------- usage


def _usage(client, key, buckets):
    return client.post("/code/usage", json={"buckets": buckets}, headers=auth(key))


def test_usage_upsert_is_idempotent_and_replaces_a_bucket(client, env):
    key, _ = connect(client, HASH_A)
    assert _usage(client, key, [bucket()]).json() == {"accepted": 1}
    assert _usage(client, key, [bucket()]).json() == {"accepted": 1}
    today = client.get("/code/usage/summary", headers=auth(key)).json()["today"]
    assert today == {
        "requests": 3,
        "tokens_before": 1000,
        "tokens_after": 400,
        "tokens_saved": 600,
        "pct": 60.0,
    }
    assert len(env.db.usage) == 1

    _usage(client, key, [bucket(requests=5, tokens_before=2000, tokens_after=500)])
    today = client.get("/code/usage/summary", headers=auth(key)).json()["today"]
    assert (today["requests"], today["tokens_saved"], today["pct"]) == (5, 1500, 75.0)


def test_usage_summary_accepts_device_key_or_session_and_is_per_account(client):
    key, _ = connect(client, HASH_A)
    _usage(client, key, [bucket()])
    by_device = client.get("/code/usage/summary", headers=auth(key)).json()
    by_session = client.get("/code/usage/summary", headers=auth("sess-alice")).json()
    assert by_device == by_session
    assert (
        client.get("/code/usage/summary", headers=auth("sess-bob")).json()["last_30d"]["requests"]
        == 0
    )


def test_usage_summary_windows_and_zero_data(client):
    key, _ = connect(client, HASH_A)
    assert client.get("/code/usage/summary", headers=auth(key)).json()["today"]["pct"] == 0
    _usage(client, key, [bucket("2026-10-14T11:00:00+00:00")])  # eight days ago
    summary = client.get("/code/usage/summary", headers=auth(key)).json()
    assert summary["today"]["requests"] == 0 and summary["last_7d"]["requests"] == 0
    assert summary["last_30d"]["requests"] == 3


def test_offset_hours_are_normalised_to_utc(client):
    key, _ = connect(client, HASH_A)
    assert _usage(client, key, [bucket("2026-10-22T13:00:00+02:00")]).status_code == 200
    assert client.get("/code/usage/summary", headers=auth(key)).json()["today"]["requests"] == 3


@pytest.mark.parametrize(
    "buckets",
    [
        [bucket("2026-10-22T11:00:00")],  # no offset
        [bucket("2026-10-22T11:30:00+00:00")],  # not a whole hour
        [bucket("2026-10-22T13:00:00+00:00")],  # in the future
        [bucket("2026-09-15T11:00:00+00:00")],  # older than 35 days
        [bucket("yesterday")],
        [bucket(tool="gpt")],
        [bucket(requests=-1)],
        [bucket(tokens_after=-5)],
        [bucket(model="secret-model")],  # no extra fields
        [bucket()] * 501,
        [bucket(), bucket("2026-10-22T11:30:00+00:00")],  # one bad bucket rejects the batch
    ],
)
def test_bad_buckets_are_rejected_and_nothing_is_written(client, env, buckets):
    key, _ = connect(client, HASH_A)
    assert _usage(client, key, buckets).status_code == 422
    assert env.db.usage == {}


# -------------------------------------------------------------- Stripe


def _sub(
    status="active",
    *,
    metadata=None,
    lookup_key="contextshrink_code_monthly",
    sub_id="sub_code_1",
    cancel_at_period_end=False,
):
    return {
        "id": sub_id,
        "object": "subscription",
        "customer": "cus_code",
        "status": status,
        "cancel_at": None,
        "cancel_at_period_end": cancel_at_period_end,
        "metadata": metadata
        if metadata is not None
        else {"product": "code", "user_id": str(ALICE)},
        "items": {
            "object": "list",
            "data": [
                {
                    "object": "subscription_item",
                    "price": {"object": "price", "id": "price_code", "lookup_key": lookup_key},
                    "current_period_start": int(PERIOD_START.timestamp()),
                    "current_period_end": int(PERIOD_END.timestamp()),
                }
            ],
        },
    }


def _event(kind, obj):
    return stripe.Event.construct_from(
        {"id": "evt_1", "object": "event", "type": kind, "data": {"object": obj}}, "sk_test_x"
    )


def _invoice(reason="subscription_cycle", *, metadata=None, sub_id="sub_code_1"):
    return {
        "object": "invoice",
        "id": "in_code_1",
        "customer": "cus_code",
        "status": "paid",
        "billing_reason": reason,
        "period_start": int(PERIOD_START.timestamp()),
        "period_end": int(PERIOD_END.timestamp()),
        "parent": {
            "type": "subscription_details",
            "subscription_details": {"subscription": sub_id, "metadata": metadata or {}},
        },
    }


def test_code_subscription_events_sync_the_plan_and_never_reach_the_fee(client, env):
    env.stripe.subs["sub_code_1"] = _sub("active")
    asyncio.run(billing.handle_event(_event("customer.subscription.created", _sub("active"))))
    assert env.db.plans[ALICE]["status"] == "active"
    assert env.db.plans[ALICE]["stripe_subscription_id"] == "sub_code_1"

    env.stripe.subs["sub_code_1"] = _sub("canceled")
    asyncio.run(billing.handle_event(_event("customer.subscription.deleted", _sub("canceled"))))
    assert env.db.plans[ALICE]["status"] == "canceled"
    assert env.db.subscription_writes == []  # core.subscriptions.plan untouched


@pytest.mark.parametrize(
    "kind",
    ["invoice.created", "invoice.paid", "invoice.payment_failed", "invoice.voided"],
)
def test_code_invoices_are_not_savings_fee_invoices(client, env, kind):
    env.stripe.subs["sub_code_1"] = _sub("active")
    # Metadata on the invoice's subscription details marks it as a code invoice.
    asyncio.run(billing.handle_event(_event(kind, _invoice(metadata={"product": "code"}))))
    assert env.db.subscription_writes == []


def test_code_invoice_is_recognised_from_its_stored_subscription_without_metadata(client, env):
    env.stripe.subs["sub_code_1"] = _sub("active")
    env.db.plans[ALICE] = {
        **_blank_plan(),
        "stripe_subscription_id": "sub_code_1",
        "status": "active",
    }
    asyncio.run(billing.handle_event(_event("invoice.created", _invoice(metadata={}))))
    assert env.db.plans[ALICE]["status"] == "active"  # no fee, no error


def test_checkout_completion_for_a_code_session_syncs_the_code_plan(client, env):
    env.stripe.subs["sub_code_1"] = _sub("active")
    session = {
        "object": "checkout.session",
        "id": "cs_1",
        "mode": "subscription",
        "subscription": "sub_code_1",
        "metadata": {"product": "code", "user_id": str(ALICE)},
    }
    asyncio.run(billing.handle_event(_event("checkout.session.completed", session)))
    assert env.db.plans[ALICE]["stripe_subscription_id"] == "sub_code_1"


def test_pro_events_are_left_to_billing(client, env):
    pro = _sub("active", metadata={}, lookup_key="contextshrink_pro_monthly", sub_id="sub_pro_1")
    assert (
        asyncio.run(code_plan.handle_stripe_event(_event("customer.subscription.updated", pro)))
        is False
    )
    pro_invoice = _invoice(metadata={}, sub_id="sub_pro_1")
    assert (
        asyncio.run(code_plan.handle_stripe_event(_event("invoice.created", pro_invoice))) is False
    )
    unrelated = _event("invoice.paid", {"object": "invoice", "id": "in_x", "customer": "cus_code"})
    assert asyncio.run(code_plan.handle_stripe_event(unrelated)) is False
    assert env.db.plans == {}


def test_a_stale_subscription_event_does_not_overwrite_the_current_one(client, env):
    env.db.plans[ALICE] = {
        **_blank_plan(),
        "stripe_subscription_id": "sub_code_2",
        "status": "active",
    }
    env.stripe.subs["sub_code_1"] = _sub("canceled", sub_id="sub_code_1")
    asyncio.run(
        billing.handle_event(
            _event("customer.subscription.deleted", _sub("canceled", sub_id="sub_code_1"))
        )
    )
    assert env.db.plans[ALICE]["stripe_subscription_id"] == "sub_code_2"
    assert env.db.plans[ALICE]["status"] == "active"


def test_checkout_starts_a_code_subscription_with_its_metadata(client, env, monkeypatch):
    async def customer(_user):
        return "cus_code"

    async def price(lookup_key):
        assert lookup_key == "contextshrink_code_monthly"
        return "price_code"

    monkeypatch.setattr(billing, "_ensure_customer", customer)
    monkeypatch.setattr(billing, "_price_id", price)
    response = client.post("/code/checkout", headers=auth("sess-alice"))
    assert response.json() == {"url": "https://checkout.test/cs_1"}
    [(name, params)] = env.stripe.calls
    assert name == "checkout"
    assert params["mode"] == "subscription" and params["customer"] == "cus_code"
    assert params["line_items"] == [{"price": "price_code", "quantity": 1}]
    assert params["subscription_data"]["metadata"] == {"product": "code", "user_id": str(ALICE)}
    assert params["success_url"] == "https://code.contextshrink.com/account?checkout=success"
    assert params["cancel_url"] == "https://code.contextshrink.com/account?checkout=cancelled"


def test_checkout_refused_while_a_code_subscription_is_open(client, env, monkeypatch):
    env.db.plans[ALICE] = {
        **_blank_plan(),
        "stripe_subscription_id": "sub_code_1",
        "status": "past_due",
    }
    response = client.post("/code/checkout", headers=auth("sess-alice"))
    assert response.status_code == 409


def test_portal_needs_a_billing_account_and_returns_to_the_account_page(client, env, monkeypatch):
    async def no_account(_user_id):
        return None

    monkeypatch.setattr(billing, "_billing_row", no_account)
    assert client.post("/code/portal", headers=auth("sess-alice")).status_code == 404

    async def account(_user_id):
        return {"stripe_customer_id": "cus_code"}

    monkeypatch.setattr(billing, "_billing_row", account)
    assert client.post("/code/portal", headers=auth("sess-alice")).json() == {
        "url": "https://billing.test/portal"
    }
    [(name, params)] = env.stripe.calls
    assert name == "portal" and params["return_url"] == "https://code.contextshrink.com/account"


def _subscribe(env, *, status="active", cancel=False):
    env.stripe.subs["sub_code_1"] = _sub(status, cancel_at_period_end=cancel)
    env.db.plans[ALICE] = {
        **_blank_plan(),
        "stripe_subscription_id": "sub_code_1",
        "status": status,
        "current_period_end": PERIOD_END,
        "cancel_at_period_end": cancel,
    }


def _stripe_updates(env):
    return [(c[1], c[2], c[3]) for c in env.stripe.calls if c[0] == "subscription.update"]


def test_cancel_sets_cancel_at_period_end_in_stripe_and_returns_the_plan(client, env):
    _subscribe(env)
    response = client.post("/code/subscription/cancel", headers=auth("sess-alice"))
    assert response.status_code == 200
    [(sub_id, params, options)] = _stripe_updates(env)
    assert sub_id == "sub_code_1" and params == {"cancel_at_period_end": True}
    assert options["idempotency_key"].startswith("code-cancel-sub_code_1-")
    assert response.json() == {
        "state": "active",
        "free_week": {
            "status": "available",
            "started_at": None,
            "ends_at": None,
            "renews_at": T0.isoformat(),
        },
        "current_period_end": PERIOD_END.isoformat(),
        "cancel_at_period_end": True,
        "entitled": True,
        "price_usd": 5,
    }
    assert env.db.plans[ALICE]["cancel_at_period_end"] is True


def test_resume_undoes_a_pending_cancel_and_each_action_has_its_own_key(client, env):
    _subscribe(env)
    client.post("/code/subscription/cancel", headers=auth("sess-alice"))
    env.clock.now += timedelta(minutes=1)
    response = client.post("/code/subscription/resume", headers=auth("sess-alice"))
    assert response.status_code == 200
    assert response.json()["cancel_at_period_end"] is False
    env.clock.now += timedelta(minutes=1)
    client.post("/code/subscription/cancel", headers=auth("sess-alice"))

    updates = _stripe_updates(env)
    assert [params["cancel_at_period_end"] for _, params, _ in updates] == [True, False, True]
    keys = [options["idempotency_key"] for _, _, options in updates]
    assert len(set(keys)) == 3
    assert keys[1].startswith("code-resume-sub_code_1-")


def test_cancel_or_resume_without_a_live_subscription_is_409_and_calls_nothing(client, env):
    connect(client, HASH_A)
    for path in ("/code/subscription/cancel", "/code/subscription/resume"):
        assert client.post(path, headers=auth("sess-alice")).status_code == 409
    env.db.plans[ALICE] = {
        **_blank_plan(),
        "stripe_subscription_id": "sub_code_1",
        "status": "canceled",
    }
    assert client.post("/code/subscription/cancel", headers=auth("sess-alice")).status_code == 409
    assert _stripe_updates(env) == []


def _stripe_invoice(inv_id, sub_id, *, number, status="paid"):
    return {
        "object": "invoice",
        "id": inv_id,
        "number": number,
        "created": int(T0.timestamp()),
        "total": 500,
        "currency": "usd",
        "status": status,
        "hosted_invoice_url": f"https://invoice.test/{inv_id}",
        "parent": {
            "type": "subscription_details",
            "subscription_details": {"subscription": sub_id, "metadata": {}},
        },
    }


def _invoice_sub(inv: dict) -> str:
    return inv["parent"]["subscription_details"]["subscription"]


CODE_INVOICES = [
    {
        "id": "in_code_1",
        "number": "CS-1",
        "created": T0.isoformat(),
        "total": 5.0,
        "currency": "usd",
        "status": "paid",
        "url": "https://invoice.test/in_code_1",
    }
]


def test_invoices_list_only_the_code_subscriptions_invoices(client, env):
    _subscribe(env)
    env.stripe.invoices = [
        _stripe_invoice("in_code_1", "sub_code_1", number="CS-1"),
        _stripe_invoice("in_pro_1", "sub_pro_1", number="PRO-1"),
        _stripe_invoice("in_draft", "sub_code_1", number="CS-2", status="draft"),
    ]
    response = client.get("/code/invoices", headers=auth("sess-alice"))
    assert response.status_code == 200
    assert response.json() == CODE_INVOICES
    assert [c for c in env.stripe.calls if c[0] == "invoices.list"] == [
        ("invoices.list", {"subscription": "sub_code_1", "limit": 24})
    ]


def test_invoices_drop_other_subscriptions_even_if_stripe_returns_them(client, env):
    _subscribe(env)
    env.stripe.honor_invoice_filter = False
    env.stripe.invoices = [
        _stripe_invoice("in_code_1", "sub_code_1", number="CS-1"),
        _stripe_invoice("in_pro_1", "sub_pro_1", number="PRO-1"),
    ]
    assert client.get("/code/invoices", headers=auth("sess-alice")).json() == CODE_INVOICES


def test_no_code_subscription_means_no_invoices_and_no_stripe_call(client, env):
    connect(client, HASH_A)
    assert client.get("/code/invoices", headers=auth("sess-alice")).json() == []
    assert [c for c in env.stripe.calls if c[0] == "invoices.list"] == []


# ------------------------------------------------------- fee fairness


def test_fee_eligible_savings_skip_subscription_and_oauth_rows():
    fee_sql = savings_ledger.FEE_ELIGIBLE_SAVINGS_USD
    assert "COALESCE(data->>'auth_mode','payg') = 'payg'" in fee_sql
    assert "auth_mode" not in savings_ledger.SAVINGS_USD  # analytics totals are unchanged
    assert savings_ledger.SAVINGS_ELIGIBLE in fee_sql


def test_ledger_savings_for_the_fee_uses_the_fee_eligible_total():
    seen = []

    class Conn:
        async def fetchval(self, sql, *args):
            seen.append(sql)
            return Decimal("12.50")

    value = asyncio.run(billing.ledger_savings(ALICE, PERIOD_START, PERIOD_END, Conn()))
    assert value == Decimal("12.50")
    assert savings_ledger.FEE_ELIGIBLE_SAVINGS_USD in seen[0]


# ------------------------------------------------------------- gateway


def test_gateway_allows_the_code_routes_and_nothing_private():
    caddy = (Path(__file__).resolve().parents[2] / "deploy/pi5-webtier/Caddyfile").read_text()
    pattern = next(
        line.split("path_regexp api ", 1)[1]
        for line in caddy.splitlines()
        if "@public_api path_regexp api " in line
    )
    for allowed in (
        "/code/cli/start",
        "/code/cli/request",
        "/code/cli/approve",
        "/code/cli/poll",
        "/code/account",
        "/code/checkout",
        "/code/portal",
        "/code/device",
        f"/code/devices/{uuid4()}",
        "/code/entitlement",
        "/code/usage",
        "/code/usage/summary",
        "/code/subscription/cancel",
        "/code/subscription/resume",
        "/code/invoices",
    ):
        assert re.fullmatch(pattern, allowed), allowed
    for private in (
        "/code",
        "/code/cli",
        "/code/cli/poll/extra",
        "/code/devices",
        "/code/devices/not-a-uuid",
        "/code/device/extra",
        "/code/usage/summary/extra",
        "/code/internal/usage",
        "/code/subscription",
        "/code/subscription/cancel/extra",
        "/code/invoices/extra",
    ):
        assert not re.fullmatch(pattern, private), private
