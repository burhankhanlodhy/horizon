"""Workspace boundaries and immutable proxy-event attribution, with no external services."""
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analytics
import main


@pytest.mark.parametrize("role,column", [("owner", "team_id"), ("admin", "team_id"), ("member", "membership_id")])
def test_team_visibility_is_derived_from_verified_membership(role, column):
    user = {"user_id": uuid4(), "team_id": uuid4(), "membership_id": uuid4(), "team_role": role}
    where, identity = analytics.workspace_filter(user)
    assert where == column + "=$1"
    assert identity == user[column]
    where, args = analytics.scope_filter(user, "rolling", 30)
    assert where.startswith(column + "=$1 AND") and args[0] == identity


def test_personal_queries_explicitly_exclude_team_history():
    uid = uuid4()
    where, identity = analytics.workspace_filter({"user_id": uid}, "k")
    assert where == "k.user_id=$1 AND k.team_id IS NULL" and identity == uid


def test_workspace_header_cannot_claim_an_unverified_membership(monkeypatch):
    uid, team_id, mid = uuid4(), uuid4(), uuid4()
    async def session(_):
        return {"user_id": uid, "plan": "pro", "subscription_status": "active"}
    calls = []
    async def context(user, selected):
        calls.append((user["user_id"], selected))
        return {"team_id": team_id, "membership_id": mid, "role": "member"}
    async def entitlement(_):
        return False
    monkeypatch.setattr(main.db, "resolve_session", session)
    monkeypatch.setattr(main.teams, "context", context)
    monkeypatch.setattr(main.team_billing, "entitlement", entitlement)
    result = asyncio.run(main.current_user("Bearer session", str(team_id)))
    assert calls == [(uid, team_id)]
    assert result["subscription_status"] == "inactive"  # Personal Pro cannot unlock an unpaid Team.
    assert result["personal_plan"] == "pro" and result["membership_id"] == mid
    assert asyncio.run(main.current_user("Bearer session", None))["plan"] == "pro"


class Ledger:
    def __init__(self, team, membership):
        self.team, self.membership = team, membership
        self.rows, self.queries = {}, []
    def acquire(self):
        return self
    def transaction(self):
        return self
    async def __aenter__(self):
        return self
    async def __aexit__(self, *_):
        pass
    async def fetchrow(self, sql, *args):
        self.queries.append(sql)
        return {"team_id": self.team, "membership_id": self.membership}
    async def execute(self, sql, *args):
        assert "ON CONFLICT(event_id) DO NOTHING" in sql
        self.rows.setdefault(args[0], args)


@pytest.fixture
def ingest(monkeypatch):
    team, membership = uuid4(), uuid4()
    ledger = Ledger(team, membership)
    monkeypatch.setattr(analytics.db, "_conn", lambda: ledger)
    monkeypatch.setenv("CONTEXTSHRINK_SERVICE_TOKEN", "test-service")
    app = FastAPI()
    async def user():
        raise AssertionError("Service ingress must not depend on selected browser workspace")
    analytics.install(app, user)
    with TestClient(app) as client:
        yield client, ledger


def payload():
    return {"event_id": str(uuid4()), "user_id": str(uuid4()), "key_id": str(uuid4()),
            "runtime_id": str(uuid4()), "occurred_at": datetime.now(timezone.utc).isoformat(),
            "request_id": "team-test", "status": 200, "savings_usd": 1.25}


def post(client, event):
    return client.post("/internal/analytics/events", headers={"X-ContextShrink-Service-Token": "test-service"},
                       json={"runtime_id": event["runtime_id"], "events": [event]})


def test_old_proxy_events_get_authoritative_attribution_and_deduplicate(ingest):
    client, ledger = ingest
    event = payload()
    response = post(client, event)
    assert response.status_code == 200, response.text
    assert post(client, event).status_code == 200
    assert len(ledger.rows) == 1
    row = next(iter(ledger.rows.values()))
    assert row[-2:] == (ledger.team, ledger.membership)
    assert json.loads(row[10])["team_id"] == str(ledger.team)
    query = ledger.queries[0]
    # Delayed events require historical validity, not current active membership.
    for check in ("k.created_at <= $3", "$3 <= k.revoked_at", "m.joined_at <= $3", "$3 <= m.left_at"):
        assert check in query


@pytest.mark.parametrize("field", ["team_id", "membership_id"])
def test_spoofed_team_attribution_is_rejected(ingest, field):
    client, ledger = ingest
    event = payload()
    event[field] = str(uuid4())
    assert post(client, event).status_code == 422
    assert not ledger.rows


def test_removed_or_foreign_key_events_are_rejected(ingest, monkeypatch):
    client, ledger = ingest
    async def missing(*_):
        return None
    monkeypatch.setattr(ledger, "fetchrow", missing)
    assert post(client, payload()).status_code == 422
    assert not ledger.rows


def test_team_entitlement_fails_closed_when_feature_disabled(monkeypatch):
    monkeypatch.delenv("CONTEXTSHRINK_TEAMS_ENABLED", raising=False)
    result = asyncio.run(analytics.compression_entitlement(uuid4(), uuid4()))
    assert result["compression_allowed"] is False


@pytest.mark.parametrize("paid,expected", [(True, 200), (False, 403)])
def test_proxy_authorization_uses_the_keys_team_not_supplied_headers(monkeypatch, paid, expected):
    key = {"id": uuid4(), "user_id": uuid4(), "team_id": uuid4(), "membership_id": uuid4(),
           "scopes": ["proxy:responses"]}
    async def resolve(_):
        return key
    async def entitlement(team_id):
        assert team_id == key["team_id"]
        return paid
    monkeypatch.setattr(analytics.db, "resolve_api_key", resolve)
    monkeypatch.setattr(analytics.team_billing, "entitlement", entitlement)
    monkeypatch.setenv("CONTEXTSHRINK_TEAMS_ENABLED", "true")
    monkeypatch.setenv("CONTEXTSHRINK_SERVICE_TOKEN", "test-service")
    app = FastAPI()
    async def no_session():
        raise AssertionError("Proxy authorization must use the key")
    analytics.install(app, no_session)
    with TestClient(app) as client:
        response = client.post("/internal/proxy/authorize",
            headers={"X-ContextShrink-Service-Token": "test-service", "X-ContextShrink-Team": str(uuid4())},
            json={"key": "cs_live_test", "scope": "proxy:responses"})
    assert response.status_code == expected
    if paid:
        assert response.json()["team_id"] == str(key["team_id"])
        assert response.json()["membership_id"] == str(key["membership_id"])
        assert response.json()["plan"] == "team"


def test_signed_member_savings_are_not_clamped_before_the_team_threshold():
    from decimal import Decimal
    rows = [{"membership_id": uuid4(), "user_id": uuid4(), "savings_usd": Decimal(value)}
            for value in ("30", "-11")]
    total, _, _ = main.team_billing.aggregate_cycle_rows(rows)
    assert total == Decimal("19.00")
    assert main.team_billing.savings_fee(total) == 0


def test_completion_after_removal_requires_a_valid_service_authorization_time(ingest, monkeypatch):
    client, ledger = ingest
    removed = datetime.now(timezone.utc) - timedelta(seconds=5)
    async def historical(sql, *args):
        # Match the database contract: authorization must predate revocation and
        # membership removal, while completion can follow an in-flight request.
        assert "$3 <= k.revoked_at" in sql and "$3 <= m.left_at" in sql
        return {"team_id": ledger.team, "membership_id": ledger.membership} if args[2] <= removed else None
    monkeypatch.setattr(ledger, "fetchrow", historical)
    event = payload()
    event["authorized_at"] = (removed - timedelta(seconds=1)).isoformat()
    assert post(client, event).status_code == 200
    event["event_id"] = str(uuid4())
    event["authorized_at"] = (removed + timedelta(seconds=1)).isoformat()
    assert post(client, event).status_code == 422
    assert len(ledger.rows) == 1
    del event["authorized_at"]
    assert post(client, event).status_code == 422  # Legacy events do not bypass time guards.
    assert client.post("/internal/analytics/events", json={"events": [event]}).status_code == 401


def test_invalid_authorization_order_is_rejected(ingest):
    client, _ = ingest
    event = payload()
    event["authorized_at"] = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    assert post(client, event).status_code == 422
