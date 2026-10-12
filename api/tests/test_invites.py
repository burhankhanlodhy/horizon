"""Beta invites: signup gating and the admin endpoints (DB calls stubbed).

Run from the repo root with the API's requirements installed:
    python -m pytest api/tests
"""

import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import contact  # noqa: E402
import invites  # noqa: E402

ADMIN = "owner@example.com"
USERS = {"admin-token": ADMIN, "user-token": "someone@example.com"}
SIGNUP = {"name": "Ada", "email": "ada@example.com", "password": "correct horse battery"}


def test_codes_are_unambiguous_and_normalize():
    code = invites.new_code()
    assert re.fullmatch(r"[A-HJKMNP-Z2-9]{5}-[A-HJKMNP-Z2-9]{5}", code)
    assert invites.normalize(" abcde fghjk ") == "ABCDE-FGHJK"
    assert invites.normalize("abcdefghjk") == "ABCDE-FGHJK"
    assert invites.normalize("ABCDE-FGHJK") == "ABCDE-FGHJK"
    assert invites.normalize(None) == ""


def test_signup_mode_defaults_to_open(monkeypatch):
    monkeypatch.delenv("SIGNUP_MODE", raising=False)
    assert not invites.invite_required()
    monkeypatch.setenv("SIGNUP_MODE", "Invite")
    assert invites.invite_required()
    monkeypatch.setenv("SIGNUP_MODE", "anything-else")
    assert not invites.invite_required()


@pytest.fixture
def signup(monkeypatch):
    import main

    state = {"claimed": [], "released": [], "attached": [], "users": set(), "live": {"GOOD1-CODE2"}}

    async def claim(code):
        if invites.normalize(code) in state["live"]:
            invite_id = uuid4()
            state["claimed"].append(invite_id)
            return invite_id
        return None

    async def release(invite_id):
        state["released"].append(invite_id)

    async def attach(user_id, invite_id):
        state["attached"].append((user_id, invite_id))

    async def get_user_by_email(email):
        return {"id": uuid4()} if email in state["users"] else None

    async def create_user(email, name, password_hash):
        if email == "race@example.com":
            raise asyncpg.UniqueViolationError("duplicate")
        state["users"].add(email)
        return {"id": uuid4(), "email": email, "name": name}

    async def noop(*a, **k):
        return None

    async def verify(token, request):
        return None

    monkeypatch.setattr(invites, "claim", claim)
    monkeypatch.setattr(invites, "release", release)
    monkeypatch.setattr(invites, "attach", attach)
    monkeypatch.setattr(main.db, "get_user_by_email", get_user_by_email)
    monkeypatch.setattr(main.db, "create_user", create_user)
    monkeypatch.setattr(main.db, "ensure_subscription", noop)
    monkeypatch.setattr(main.db, "create_session", noop)
    monkeypatch.setattr(main.turnstile, "verify", verify)
    monkeypatch.setattr(main.account_email, "verification_required", lambda: False)

    app = FastAPI()
    app.post("/auth/signup", response_model=main.AuthOut)(main.signup)
    return TestClient(app), state


def test_open_mode_needs_no_code(signup, monkeypatch):
    c, state = signup
    monkeypatch.setenv("SIGNUP_MODE", "open")
    assert c.post("/auth/signup", json=SIGNUP).status_code == 200
    assert state["claimed"] == []


def test_invite_mode_requires_a_code(signup, monkeypatch):
    c, state = signup
    monkeypatch.setenv("SIGNUP_MODE", "invite")
    r = c.post("/auth/signup", json=SIGNUP)
    assert r.status_code == 403 and "invite" in r.json()["detail"].lower()
    r = c.post("/auth/signup", json={**SIGNUP, "invite_code": "NOPE0-NOPE0"})
    assert r.status_code == 403
    assert state["users"] == set()


def test_invite_mode_accepts_a_live_code_and_records_it(signup, monkeypatch):
    c, state = signup
    monkeypatch.setenv("SIGNUP_MODE", "invite")
    r = c.post("/auth/signup", json={**SIGNUP, "invite_code": "good1code2"})
    assert r.status_code == 200
    assert len(state["claimed"]) == 1
    assert state["attached"][0][1] == state["claimed"][0]
    assert state["released"] == []


def test_failed_signup_gives_the_invite_back(signup, monkeypatch):
    c, state = signup
    monkeypatch.setenv("SIGNUP_MODE", "invite")
    r = c.post("/auth/signup", json={**SIGNUP, "email": "race@example.com", "invite_code": "GOOD1-CODE2"})
    assert r.status_code == 409
    assert state["released"] == state["claimed"] and len(state["claimed"]) == 1


def test_existing_email_does_not_spend_an_invite(signup, monkeypatch):
    c, state = signup
    monkeypatch.setenv("SIGNUP_MODE", "invite")
    state["users"].add("ada@example.com")
    r = c.post("/auth/signup", json={**SIGNUP, "invite_code": "GOOD1-CODE2"})
    assert r.status_code == 409 and state["claimed"] == []


@pytest.fixture
def admin(monkeypatch):
    rows: list = []
    revoked: list = []
    monkeypatch.setattr(contact, "ADMIN_EMAILS", frozenset({ADMIN}))

    async def create(body, created_by):
        row = {
            "id": uuid4(),
            "code": invites.new_code(),
            "note": body.note,
            "max_uses": body.max_uses,
            "uses": 0,
            "expires_at": None,
            "revoked_at": None,
            "created_at": datetime.now(timezone.utc),
        }
        rows.append(row)
        return row

    async def list_all(limit):
        return rows[:limit]

    async def revoke(invite_id):
        revoked.append(invite_id)
        return any(r["id"] == invite_id for r in rows)

    monkeypatch.setattr(invites, "create", create)
    monkeypatch.setattr(invites, "list_all", list_all)
    monkeypatch.setattr(invites, "revoke", revoke)

    async def current_user(authorization: str = Header(default="")):
        email = USERS.get(authorization.removeprefix("Bearer "))
        if not email:
            raise HTTPException(401, "no")
        return {"id": uuid4(), "email": email}

    app = FastAPI()
    invites.install(app, current_user)
    return TestClient(app), rows, revoked


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_admins_create_list_and_revoke_invites(admin):
    c, rows, revoked = admin
    r = c.post("/admin/invites", json={"note": "Sam from the meetup", "max_uses": 3}, headers=auth("admin-token"))
    assert r.status_code == 201
    created = r.json()
    assert created["note"] == "Sam from the meetup" and created["max_uses"] == 3
    listed = c.get("/admin/invites", headers=auth("admin-token")).json()
    assert [i["code"] for i in listed] == [created["code"]]
    assert c.post(f"/admin/invites/{created['id']}/revoke", headers=auth("admin-token")).status_code == 204
    assert c.post(f"/admin/invites/{uuid4()}/revoke", headers=auth("admin-token")).status_code == 404


def test_non_admins_cannot_touch_invites(admin):
    c, rows, _ = admin
    assert c.get("/admin/invites", headers=auth("user-token")).status_code == 403
    assert c.post("/admin/invites", json={}, headers=auth("user-token")).status_code == 403
    assert c.get("/admin/invites").status_code == 401
    assert rows == []


def test_invite_input_is_bounded(admin):
    c, *_ = admin
    h = auth("admin-token")
    assert c.post("/admin/invites", json={"max_uses": 0}, headers=h).status_code == 422
    assert c.post("/admin/invites", json={"max_uses": 5000}, headers=h).status_code == 422
    assert c.post("/admin/invites", json={"expires_in_days": 0}, headers=h).status_code == 422
    assert c.post("/admin/invites", json={"surprise": 1}, headers=h).status_code == 422


def test_signup_mode_is_public(admin, monkeypatch):
    c, *_ = admin
    monkeypatch.setenv("SIGNUP_MODE", "invite")
    assert c.get("/auth/signup-mode").json() == {"invite_required": True}
