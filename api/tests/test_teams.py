"""Team API permission and invitation-secret tests."""

import asyncio
import copy
import hashlib
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import teams  # noqa: E402


class _DB:
    def __init__(self, row):
        self.row = row
        self.calls = 0

    async def fetchrow(self, *_args):
        self.calls += 1
        return self.row


class _Tx:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        self.db.lock.acquire()
        self.snapshot = copy.deepcopy(self.db.state)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type:
            self.db.state = self.snapshot
        self.db.lock.release()


class _Acquire:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *_args):
        return None


class _TeamDB:
    """Small SQL-aware fake for route lifecycle and transaction tests."""
    def __init__(self, users):
        self.state = {
            "users": users, "teams": {}, "memberships": {}, "subscriptions": {},
            "invitations": {}, "keys": [],
        }
        self.lock = threading.RLock()

    def acquire(self):
        return _Acquire(self)

    def transaction(self):
        return _Tx(self)

    @staticmethod
    def _sql(query):
        return " ".join(query.lower().split())

    def _members(self, team_id):
        return [m for m in self.state["memberships"].values()
                if m["team_id"] == team_id and m["left_at"] is None]

    def _pending(self, team_id):
        now = datetime.now(timezone.utc)
        return [i for i in self.state["invitations"].values()
                if i["team_id"] == team_id and i["status"] == "pending" and i["expires_at"] > now]

    async def fetchrow(self, query, *args):
        sql = self._sql(query)
        if "select role from core.team_memberships" in sql and "for update" in sql:
            member_id, team_id = args
            row = self.state["memberships"].get(member_id)
            return {"role": row["role"]} if row and row["team_id"] == team_id and row["left_at"] is None else None
        if "from core.teams t" in sql and "join core.team_memberships m" in sql:
            team_id, user_id = args
            team = self.state["teams"].get(team_id)
            member = next((m for m in self._members(team_id) if m["user_id"] == user_id), None)
            if not team or not member:
                return None
            return {"id": team_id, "name": team["name"], "status": self.state["subscriptions"][team_id]["status"],
                    "seat_count": self.state["subscriptions"][team_id]["seat_count"],
                    "membership_id": member["id"], "role": member["role"]}
        if "insert into core.teams" in sql:
            name, created_by = args
            team_id = uuid4()
            row = {"id": team_id, "name": name, "created_by": created_by,
                   "created_at": datetime.now(timezone.utc)}
            self.state["teams"][team_id] = row
            return row
        if "insert into core.team_subscriptions" in sql:
            team_id, seats = args
            row = {"team_id": team_id, "seat_count": seats, "pending_seat_count": None, "status": "pending"}
            self.state["subscriptions"][team_id] = row
            return {"seat_count": seats, "status": "pending"}
        if "select ts.seat_count" in sql:
            team_id = args[-1]
            sub = self.state["subscriptions"].get(team_id)
            if not sub:
                return None
            return {"seat_count": sub["seat_count"], "pending_seat_count": sub["pending_seat_count"],
                    "status": sub["status"],
                    "occupied": len(self._members(team_id)),
                    "reserved": len(self._pending(team_id))}
        if "select status from core.team_subscriptions" in sql:
            sub = self.state["subscriptions"].get(args[0])
            return None if not sub else {"status": sub["status"]}
        if "select id,email,role,status,expires_at from core.team_invitations" in sql:
            team_id, invitation_id = args
            row = self.state["invitations"].get(invitation_id)
            return {k: row[k] for k in ("id", "email", "role", "status", "expires_at")} \
                if row and row["team_id"] == team_id else None
        if "select id from core.team_invitations" in sql:
            team_id, email = args
            return next(({"id": i["id"]} for i in self._pending(team_id)
                         if i["email"].lower() == email.lower()), None)
        if "insert into core.team_invitations" in sql:
            team_id, email, role, token_hash, invited_by = args
            row = {"id": uuid4(), "team_id": team_id, "email": email, "role": role,
                   "token_hash": token_hash, "expires_at": datetime.now(timezone.utc) + timedelta(days=7),
                   "status": "pending", "invited_by": invited_by,
                   "created_at": datetime.now(timezone.utc)}
            self.state["invitations"][row["id"]] = row
            return {k: row[k] for k in ("id", "email", "role", "status", "expires_at", "created_at")}
        if "where i.token_hash=$1" in sql:
            token_hash = args[0]
            inv = next((i for i in self.state["invitations"].values() if i["token_hash"] == token_hash), None)
            return None if inv is None else dict(inv) | {"name": self.state["teams"][inv["team_id"]]["name"]}
        if "insert into core.team_memberships" in sql:
            team_id, user_id, role = args
            row = {"id": uuid4(), "team_id": team_id, "user_id": user_id, "role": role,
                   "joined_at": datetime.now(timezone.utc), "left_at": None}
            self.state["memberships"][row["id"]] = row
            return {k: row[k] for k in ("id", "team_id", "user_id", "role", "joined_at")}
        if "select id,user_id,role from core.team_memberships" in sql and "for update" in sql:
            member_id, team_id = args
            row = self.state["memberships"].get(member_id)
            return None if not row or row["team_id"] != team_id or row["left_at"] else row
        if "select id from core.team_memberships" in sql and "for update" in sql:
            member_id, team_id = args
            row = self.state["memberships"].get(member_id)
            return {"id": member_id} if row and row["team_id"] == team_id and row["left_at"] is None else None
        if "update core.team_invitations set token_hash" in sql:
            team_id, invitation_id, token_hash = args
            row = self.state["invitations"].get(invitation_id)
            if not row or row["team_id"] != team_id or row["status"] != "pending":
                return None
            row["token_hash"] = token_hash
            row["expires_at"] = datetime.now(timezone.utc) + timedelta(days=7)
            return {k: row[k] for k in ("id", "email", "role", "status", "expires_at", "created_at")}
        if "update core.team_invitations set status='revoked'" in sql:
            team_id, invitation_id = args
            row = self.state["invitations"].get(invitation_id)
            if not row or row["team_id"] != team_id or row["status"] != "pending":
                return None
            row["status"] = "revoked"
            return {"id": invitation_id}
        if "update core.team_memberships set role=$3" in sql:
            member_id, team_id, role = args
            row = self.state["memberships"].get(member_id)
            if not row or row["team_id"] != team_id or row["left_at"] or row["role"] == "owner":
                return None
            row["role"] = role
            return {k: row[k] for k in ("id", "user_id", "role", "joined_at")}
        return None

    async def fetchval(self, query, *args):
        sql = self._sql(query)
        if "exists(select 1 from core.team_invitations" in sql:
            team_id, invitation_id, email = args
            return any(i["team_id"] == team_id and i["id"] != invitation_id
                       and i["email"].lower() == email.lower() and i["status"] == "pending"
                       and i["expires_at"] > datetime.now(timezone.utc)
                       for i in self.state["invitations"].values())
        if "exists(select 1 from core.team_memberships where user_id=$1" in sql:
            return bool(self._memberships_for_user(args[0]))
        if "lower(u.email)=lower($1)" in sql:
            email = args[0].lower()
            return any(m["left_at"] is None and self.state["users"][m["user_id"]]["email"].lower() == email
                       for m in self.state["memberships"].values())
        return False

    def _memberships_for_user(self, user_id):
        return [m for m in self.state["memberships"].values()
                if m["user_id"] == user_id and m["left_at"] is None]

    async def execute(self, query, *args):
        sql = self._sql(query)
        if "insert into core.team_memberships" in sql:
            team_id, user_id = args
            row = {"id": uuid4(), "team_id": team_id, "user_id": user_id, "role": "owner",
                   "joined_at": datetime.now(timezone.utc), "left_at": None}
            self.state["memberships"][row["id"]] = row
        elif "update core.team_invitations set status='accepted'" in sql:
            row = self.state["invitations"][args[0]]
            row["status"] = "accepted"
        elif "update core.team_memberships set left_at=now()" in sql:
            self.state["memberships"][args[0]]["left_at"] = datetime.now(timezone.utc)
        elif "update core.api_keys set revoked_at=now()" in sql:
            for key in self.state["keys"]:
                if key["membership_id"] == args[0] and key["revoked_at"] is None:
                    key["revoked_at"] = datetime.now(timezone.utc)
        elif "update core.team_memberships set role='admin'" in sql:
            team_id = args[0]
            for row in self._members(team_id):
                if row["role"] == "owner":
                    row["role"] = "admin"
        elif "update core.team_memberships set role='owner'" in sql:
            self.state["memberships"][args[0]]["role"] = "owner"
        return "OK"


@pytest.fixture
def route_fixture(monkeypatch):
    owner_id, member_id, other_id = uuid4(), uuid4(), uuid4()
    users = {
        "owner": {"user_id": owner_id, "email": "owner@example.com", "name": "Owner",
                  "email_verified_at": datetime.now(timezone.utc)},
        "member": {"user_id": member_id, "email": "member@example.com", "name": "Member",
                   "email_verified_at": datetime.now(timezone.utc)},
        "other": {"user_id": other_id, "email": "other@example.com", "name": "Other",
                  "email_verified_at": datetime.now(timezone.utc)},
        "unverified": {"user_id": uuid4(), "email": "new@example.com", "name": "New",
                       "email_verified_at": None},
    }
    fake = _TeamDB({u["user_id"]: u for u in users.values()})
    sent = []
    async def send(email, subject, text, html=None):
        sent.append((email, subject, text, html))
        return True

    monkeypatch.setattr(teams, "enabled", lambda: True)
    monkeypatch.setattr(teams.db, "_conn", lambda: fake)
    monkeypatch.setattr(teams.mailer, "send", send)

    async def current_user(authorization: str | None = Header(default=None)):
        token = (authorization or "").removeprefix("Bearer ")
        if token not in users:
            raise HTTPException(401, "Missing bearer token")
        return users[token]

    app = FastAPI()
    teams.install(app, current_user)
    return TestClient(app), fake, sent, users


def _invite_token(sent, index=-1):
    match = re.search(r"token=([^\s&]+)", sent[index][2])
    assert match
    return unquote(match.group(1))


def _make_team(client):
    response = client.post("/teams", headers={"Authorization": "Bearer owner"},
                           json={"name": "Research", "seats": 3})
    assert response.status_code == 201
    return response.json()["team"]["id"]


def _activate(fake, team_id):
    fake.state["subscriptions"][UUID(team_id)]["status"] = "active"


def test_team_creation_requires_verified_account_and_creates_pending_owner(route_fixture):
    client, fake, _sent, _users = route_fixture
    denied = client.post("/teams", headers={"Authorization": "Bearer unverified"}, json={"name": "No"})
    assert denied.status_code == 403
    assert fake.state["teams"] == {}

    created = client.post("/teams", headers={"Authorization": "Bearer owner"},
                          json={"name": "Research", "seats": 3})
    assert created.status_code == 201
    team = created.json()["team"]
    assert team["role"] == "owner" and team["status"] == "pending" and team["seat_count"] == 3
    membership = next(iter(fake.state["memberships"].values()))
    assert membership["user_id"] == _users["owner"]["user_id"] and membership["role"] == "owner"


def test_pending_team_cannot_reserve_seats(route_fixture):
    client, fake, _sent, _users = route_fixture
    team_id = _make_team(client)
    response = client.post(f"/teams/{team_id}/invitations", headers={"Authorization": "Bearer owner"},
                           json={"email": "member@example.com"})
    assert response.status_code == 409
    assert fake.state["invitations"] == {}


def test_invitation_reservations_email_matching_and_single_use(route_fixture):
    client, fake, sent, users = route_fixture
    team_id = _make_team(client)
    _activate(fake, team_id)
    # Upcoming reductions constrain new reservations immediately.
    fake.state["subscriptions"][UUID(team_id)]["seat_count"] = 10
    fake.state["subscriptions"][UUID(team_id)]["pending_seat_count"] = 3
    one = client.post(f"/teams/{team_id}/invitations", headers={"Authorization": "Bearer owner"},
                      json={"email": "member@example.com"})
    assert one.status_code == 201
    token = _invite_token(sent, 0)
    mismatch = client.post("/team-invitations/accept", headers={"Authorization": "Bearer other"},
                           json={"token": token})
    assert mismatch.status_code == 403
    joined = client.post("/team-invitations/accept", headers={"Authorization": "Bearer member"},
                         json={"token": token})
    assert joined.status_code == 200
    replay = client.post("/team-invitations/accept", headers={"Authorization": "Bearer member"},
                         json={"token": token})
    assert replay.status_code == 410
    assert fake.state["invitations"][UUID(one.json()["invitation"]["id"])]["status"] == "accepted"
    existing = client.post(f"/teams/{team_id}/invitations", headers={"Authorization": "Bearer owner"},
                           json={"email": "member@example.com"})
    assert existing.status_code == 409

    two = client.post(f"/teams/{team_id}/invitations", headers={"Authorization": "Bearer owner"},
                      json={"email": "other@example.com"})
    assert two.status_code == 201
    full = client.post(f"/teams/{team_id}/invitations", headers={"Authorization": "Bearer owner"},
                       json={"email": "third@example.com"})
    assert full.status_code == 409
    assert len(fake._pending(UUID(team_id))) == 1


def test_expired_invitation_cannot_be_accepted(route_fixture):
    client, fake, sent, _users = route_fixture
    team_id = _make_team(client)
    _activate(fake, team_id)
    created = client.post(f"/teams/{team_id}/invitations", headers={"Authorization": "Bearer owner"},
                          json={"email": "member@example.com"})
    token = _invite_token(sent)
    invitation = fake.state["invitations"][UUID(created.json()["invitation"]["id"])]
    invitation["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    response = client.post("/team-invitations/accept", headers={"Authorization": "Bearer member"},
                           json={"token": token})
    assert response.status_code == 410
    assert not fake._members(UUID(team_id)) or len(fake._members(UUID(team_id))) == 1


def test_acceptance_rejects_user_who_joined_another_team_after_invite(route_fixture):
    client, fake, sent, users = route_fixture
    team_id = _make_team(client)
    _activate(fake, team_id)
    created = client.post(f"/teams/{team_id}/invitations", headers={"Authorization": "Bearer owner"},
                          json={"email": "other@example.com"})
    assert created.status_code == 201
    token = _invite_token(sent)
    foreign_team = uuid4()
    fake.state["teams"][foreign_team] = {"id": foreign_team, "name": "Other team",
                                          "created_by": users["owner"]["user_id"]}
    fake.state["subscriptions"][foreign_team] = {
        "team_id": foreign_team, "seat_count": 3, "pending_seat_count": None, "status": "active"
    }
    foreign_membership = {"id": uuid4(), "team_id": foreign_team,
                          "user_id": users["other"]["user_id"], "role": "member",
                          "joined_at": datetime.now(timezone.utc), "left_at": None}
    fake.state["memberships"][foreign_membership["id"]] = foreign_membership

    response = client.post("/team-invitations/accept", headers={"Authorization": "Bearer other"},
                           json={"token": token})
    assert response.status_code == 409
    assert len(fake._members(UUID(team_id))) == 1


def test_concurrent_invites_cannot_overbook_last_seat(route_fixture):
    client, fake, _sent, _users = route_fixture
    team_id = _make_team(client)
    _activate(fake, team_id)
    first = client.post(f"/teams/{team_id}/invitations", headers={"Authorization": "Bearer owner"},
                        json={"email": "member@example.com"})
    assert first.status_code == 201

    def invite(email):
        return client.post(f"/teams/{team_id}/invitations", headers={"Authorization": "Bearer owner"},
                           json={"email": email})

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(invite, ("other@example.com", "third@example.com")))
    assert sorted(result.status_code for result in results) == [201, 409]
    assert len(fake._members(UUID(team_id))) + len(fake._pending(UUID(team_id))) == 3


def test_resend_replaces_single_use_token(route_fixture):
    client, fake, sent, _users = route_fixture
    team_id = _make_team(client)
    _activate(fake, team_id)
    created = client.post(f"/teams/{team_id}/invitations", headers={"Authorization": "Bearer owner"},
                          json={"email": "member@example.com"})
    invitation_id = created.json()["invitation"]["id"]
    original = _invite_token(sent)
    resent = client.post(f"/teams/{team_id}/invitations/{invitation_id}/resend",
                         headers={"Authorization": "Bearer owner"})
    assert resent.status_code == 200
    replacement = _invite_token(sent)
    assert replacement != original
    old_attempt = client.post("/team-invitations/accept", headers={"Authorization": "Bearer member"},
                              json={"token": original})
    new_attempt = client.post("/team-invitations/accept", headers={"Authorization": "Bearer member"},
                              json={"token": replacement})
    assert old_attempt.status_code == 410 and new_attempt.status_code == 200


@pytest.mark.parametrize(("purchased", "pending"), [(3, None), (10, 3)])
def test_resend_of_expired_invite_cannot_reclaim_full_or_reduced_seat(purchased, pending, route_fixture):
    client, fake, sent, _users = route_fixture
    team_id = _make_team(client)
    _activate(fake, team_id)
    sub = fake.state["subscriptions"][UUID(team_id)]
    sub["seat_count"] = purchased
    sub["pending_seat_count"] = pending
    expired = client.post(f"/teams/{team_id}/invitations", headers={"Authorization": "Bearer owner"},
                          json={"email": "member@example.com"})
    assert expired.status_code == 201
    expired_id = expired.json()["invitation"]["id"]
    client.post(f"/teams/{team_id}/invitations", headers={"Authorization": "Bearer owner"},
                json={"email": "other@example.com"})
    fake.state["invitations"][UUID(expired_id)]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    fill = client.post(f"/teams/{team_id}/invitations", headers={"Authorization": "Bearer owner"},
                       json={"email": "third@example.com"})
    assert fill.status_code == 201

    resent = client.post(f"/teams/{team_id}/invitations/{expired_id}/resend",
                         headers={"Authorization": "Bearer owner"})
    assert resent.status_code == 409
    assert fake.state["invitations"][UUID(expired_id)]["expires_at"] < datetime.now(timezone.utc)


def test_only_owner_can_grant_admin_role(route_fixture):
    client, fake, sent, _users = route_fixture
    team_id = _make_team(client)
    _activate(fake, team_id)
    elevated = client.post(f"/teams/{team_id}/invitations", headers={"Authorization": "Bearer owner"},
                           json={"email": "member@example.com", "role": "admin"})
    assert elevated.status_code == 201
    accepted = client.post("/team-invitations/accept", headers={"Authorization": "Bearer member"},
                           json={"token": _invite_token(sent)})
    assert accepted.status_code == 200
    denied = client.post(f"/teams/{team_id}/invitations", headers={"Authorization": "Bearer member"},
                         json={"email": "other@example.com", "role": "admin"})
    assert denied.status_code == 403


def test_member_removal_revokes_keys_and_retains_membership_history(route_fixture):
    client, fake, sent, users = route_fixture
    team_id = _make_team(client)
    _activate(fake, team_id)
    client.post(f"/teams/{team_id}/invitations", headers={"Authorization": "Bearer owner"},
                json={"email": "member@example.com"})
    client.post("/team-invitations/accept", headers={"Authorization": "Bearer member"},
                json={"token": _invite_token(sent)})
    member = next(m for m in fake._members(UUID(team_id)) if m["role"] == "member")
    joined_at = member["joined_at"]
    fake.state["keys"].append({"membership_id": member["id"], "revoked_at": None})

    removed = client.delete(f"/teams/{team_id}/members/{member['id']}",
                            headers={"Authorization": "Bearer owner"})
    assert removed.status_code == 200
    assert member["left_at"] is not None and member["joined_at"] == joined_at
    assert fake.state["keys"][0]["revoked_at"] is not None
    assert fake.state["memberships"][member["id"]]["user_id"] == users["member"]["user_id"]


def test_role_changes_transfer_ownership_and_protect_final_owner(route_fixture):
    client, fake, sent, _users = route_fixture
    team_id = _make_team(client)
    _activate(fake, team_id)
    client.post(f"/teams/{team_id}/invitations", headers={"Authorization": "Bearer owner"},
                json={"email": "member@example.com"})
    client.post("/team-invitations/accept", headers={"Authorization": "Bearer member"},
                json={"token": _invite_token(sent)})
    owner = next(m for m in fake._members(UUID(team_id)) if m["role"] == "owner")
    member = next(m for m in fake._members(UUID(team_id)) if m["role"] == "member")

    promoted = client.patch(f"/teams/{team_id}/members/{member['id']}",
                            headers={"Authorization": "Bearer owner"}, json={"role": "admin"})
    assert promoted.status_code == 200
    assert fake.state["memberships"][member["id"]]["role"] == "admin"
    owner_remove = client.delete(f"/teams/{team_id}/members/{owner['id']}",
                                 headers={"Authorization": "Bearer owner"})
    assert owner_remove.status_code == 409

    transfer = client.post(f"/teams/{team_id}/transfer-ownership", headers={"Authorization": "Bearer owner"},
                           json={"membership_id": str(member["id"])})
    assert transfer.status_code == 200
    assert fake.state["memberships"][member["id"]]["role"] == "owner"
    assert fake.state["memberships"][owner["id"]]["role"] == "admin"
    assert sum(m["role"] == "owner" for m in fake._members(UUID(team_id))) == 1


def test_context_returns_scoped_membership_and_allows_pending_owner(monkeypatch):
    row = {
        "id": uuid4(), "name": "Research", "status": "pending", "seat_count": 3,
        "membership_id": uuid4(), "role": "owner",
    }
    fake = _DB(row)
    monkeypatch.setattr(teams, "enabled", lambda: True)
    monkeypatch.setattr(teams.db, "_conn", lambda: fake)

    result = asyncio.run(teams.context({"user_id": uuid4()}, str(row["id"]), "owner"))

    assert result == {
        "id": row["id"], "team_id": row["id"], "name": "Research",
        "membership_id": row["membership_id"], "role": "owner",
        "status": "pending", "seat_count": 3, "pending_seat_count": None,
    }


def test_context_rejects_insufficient_role(monkeypatch):
    fake = _DB({"id": uuid4(), "name": "Research", "status": "active", "seat_count": 3,
                "membership_id": uuid4(), "role": "member"})
    monkeypatch.setattr(teams, "enabled", lambda: True)
    monkeypatch.setattr(teams.db, "_conn", lambda: fake)

    with pytest.raises(HTTPException) as error:
        asyncio.run(teams.context({"user_id": uuid4()}, str(uuid4()), "admin"))
    assert error.value.status_code == 403


def test_invitation_token_is_random_and_only_hash_is_storable():
    raw, digest = teams._new_token()
    assert len(raw) >= 40
    assert digest == hashlib.sha256(raw.encode()).hexdigest()
    assert raw != digest


def test_team_feature_flag_is_closed_by_default(monkeypatch):
    monkeypatch.delenv("CONTEXTSHRINK_TEAMS_ENABLED", raising=False)
    assert teams.enabled() is False


def test_schema_preserves_membership_history_and_key_attribution():
    schema = (Path(__file__).resolve().parents[1] / "schema.sql").read_text(encoding="utf-8")
    assert "team_memberships_history_guard" in schema
    assert "closed team memberships cannot be reopened" in schema
    assert "API key team attribution is immutable" in schema
    assert "FOREIGN KEY (membership_id, team_id, user_id)" in schema


def test_acceptance_requires_verified_email_before_database_access(monkeypatch):
    monkeypatch.setattr(teams, "enabled", lambda: True)
    db = _DB(None)
    monkeypatch.setattr(teams.db, "_conn", lambda: db)

    async def current_user(authorization: str | None = Header(default=None)):
        return {"user_id": uuid4(), "email": "invitee@example.com", "email_verified_at": None}

    app = FastAPI()
    teams.install(app, current_user)
    response = TestClient(app).post("/team-invitations/accept", json={"token": "t" * 43})

    assert response.status_code == 403
    assert "Verify" in response.json()["detail"]
    assert db.calls == 0
