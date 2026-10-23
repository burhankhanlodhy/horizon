"""npx bar reads only the authenticated key's aggregate, on every plan."""
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analytics


@pytest.fixture
def client(monkeypatch):
    owner, key_id = uuid4(), uuid4()
    queries = []
    keys = {
        analytics.security.hash_token("cs_live_valid"): {"user_id": owner, "id": key_id, "scopes": ["proxy:messages"]},
        analytics.security.hash_token("cs_live_noscope"): {"user_id": owner, "id": key_id, "scopes": []},
    }

    async def resolve(hashed):
        return keys.get(hashed)

    class DB:
        async def fetchrow(self, query, *args):
            queries.append((query, args))
            return {"as_of": datetime(2026, 10, 20, tzinfo=timezone.utc), "requests": 3,
                    "tokens_before": 1000, "tokens_saved": 250, "savings_usd": -0.02}

    monkeypatch.setattr(analytics.db, "resolve_api_key", resolve)
    monkeypatch.setattr(analytics.db, "_conn", DB)

    async def no_session():
        pytest.fail("bar must not require or resolve an account session")

    app = FastAPI()
    analytics.install(app, no_session)
    with TestClient(app) as c:
        c.queries, c.owner, c.key_id, c.keys = queries, owner, key_id, keys
        yield c


def test_summary_derives_both_owners_from_key_and_ignores_supplied_identifiers(client):
    response = client.get("/usage/key-summary", headers={"Authorization": "Bearer cs_live_valid"},
                          params={"user_id": str(uuid4()), "key_id": str(uuid4()), "scope": "lifetime"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"scope": "key", "period": "today_utc", "as_of": "2026-10-20T00:00:00+00:00",
                               "requests": 3, "tokens_before": 1000, "tokens_saved": 250, "savings_usd": -0.02}
    [(query, args)] = client.queries
    assert args == (client.owner, client.key_id)
    assert "user_id=$1 AND key_id=$2" in query
    assert "AT TIME ZONE 'UTC'" in query and "occurred_at <= now()" in query
    # Match the dashboard: pings do not count as requests; negative savings stay.
    assert analytics.REQUEST in query


@pytest.mark.parametrize("header", [None, "cs_live_valid", "Bearer session-token", "Bearer cs_live_revoked", "Bearer " + "cs_live_" + "x" * 301])
def test_invalid_or_revoked_keys_do_not_reach_the_ledger(client, header):
    headers = {} if header is None else {"Authorization": header}
    assert client.get("/usage/key-summary", headers=headers).status_code == 401
    assert not client.queries


def test_proxy_scope_is_required(client):
    assert client.get("/usage/key-summary", headers={"Authorization": "Bearer cs_live_noscope"}).status_code == 403
    assert not client.queries


def test_revocation_is_checked_on_each_poll(client):
    headers = {"Authorization": "Bearer cs_live_valid"}
    assert client.get("/usage/key-summary", headers=headers).status_code == 200
    client.keys.clear()
    assert client.get("/usage/key-summary", headers=headers).status_code == 401
    assert len(client.queries) == 1


def test_gateway_exposes_only_the_named_usage_routes():
    caddy = (Path(__file__).resolve().parents[2] / "deploy/pi5-webtier/Caddyfile").read_text()
    pattern = next(line.split("path_regexp api ", 1)[1] for line in caddy.splitlines() if "@public_api path_regexp api " in line)
    team = "00000000-0000-4000-8000-000000000099"
    for allowed in ("/usage/key-summary", "/usage/summary", "/teams", f"/teams/{team}",
                    f"/teams/{team}/invitations", f"/teams/{team}/invitations/{team}/resend",
                    f"/teams/{team}/members/{team}", f"/teams/{team}/transfer-ownership",
                    f"/teams/{team}/billing/seats/preview", "/team-invitations/accept"):
        assert re.fullmatch(pattern, allowed)
    for private in ("/usage/keys", "/usage/key-summary/other", "/internal/proxy/authorize", "/docs",
                    f"/teams/{team}/anything", f"/teams/{team}/billing/secrets"):
        assert not re.fullmatch(pattern, private)
