"""Contact form and admin inbox (no Postgres: the DB calls are stubbed).

Run from the repo root with the API's requirements installed:
    python -m pytest api/tests
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import contact  # noqa: E402

ADMIN = "owner@example.com"
USERS = {"admin-token": ADMIN, "user-token": "someone@example.com"}

GOOD = {
    "name": "Ada",
    "email": "Ada@Example.com",
    "topic": "billing",
    "message": "How is the savings fee calculated?",
}


@pytest.fixture
def client(monkeypatch):
    saved: list = []
    handled: dict = {}
    monkeypatch.setattr(contact, "ADMIN_EMAILS", frozenset({ADMIN}))
    monkeypatch.setattr(contact, "limiter", contact._RateLimiter())

    async def save_message(body):
        saved.append(body)

    async def list_messages(state, limit):
        return [
            {
                "id": uuid4(),
                "name": b.name,
                "email": b.email.lower(),
                "topic": b.topic,
                "message": b.message,
                "created_at": datetime.now(timezone.utc),
                "handled_at": None,
            }
            for b in saved
        ][:limit]

    async def set_handled(message_id, value):
        handled[message_id] = value
        return True

    monkeypatch.setattr(contact, "save_message", save_message)
    monkeypatch.setattr(contact, "list_messages", list_messages)
    monkeypatch.setattr(contact, "set_handled", set_handled)

    async def current_user(authorization: str | None = Header(default=None)):
        token = (authorization or "").removeprefix("Bearer ").strip()
        if token not in USERS:
            raise HTTPException(401, "Missing bearer token")
        return {"email": USERS[token]}

    app = FastAPI()
    contact.install(app, current_user)
    c = TestClient(app)
    c.saved, c.handled = saved, handled
    return c


def test_a_message_is_saved_with_only_what_the_sender_typed(client):
    r = client.post("/contact", json=GOOD)
    assert r.status_code == 202
    (body,) = client.saved
    assert body.email == "Ada@example.com" and body.topic == "billing"
    assert not hasattr(body, "ip")


@pytest.mark.parametrize(
    "bad",
    [
        {"message": "short"},
        {"email": "not-an-email"},
        {"topic": "sales"},
        {"name": ""},
        {"message": "x" * 5001},
        {"extra": "field"},
    ],
)
def test_invalid_messages_are_rejected(client, bad):
    assert client.post("/contact", json={**GOOD, **bad}).status_code == 422
    assert client.saved == []


def test_the_honeypot_drops_bots_without_telling_them(client):
    r = client.post("/contact", json={**GOOD, "website": "http://spam.example"})
    assert r.status_code == 202
    assert client.saved == []


def test_one_sender_is_rate_limited(client):
    headers = {"CF-Connecting-IP": "203.0.113.7"}
    codes = [client.post("/contact", json=GOOD, headers=headers).status_code for _ in range(4)]
    assert codes == [202, 202, 202, 429]
    # Another sender is unaffected.
    other = client.post("/contact", json=GOOD, headers={"CF-Connecting-IP": "198.51.100.2"})
    assert other.status_code == 202


def test_the_global_cap_holds_back_a_flood(monkeypatch):
    monkeypatch.setattr(contact, "GLOBAL_HOURLY_LIMIT", 5)
    limiter = contact._RateLimiter()
    allowed = [limiter.allow(f"10.0.0.{i}", now=100.0) for i in range(8)]
    assert allowed == [True] * 5 + [False] * 3
    # An hour later the cap has rolled over.
    assert limiter.allow("10.0.0.99", now=100.0 + 3601)


def test_the_sender_window_rolls_over():
    limiter = contact._RateLimiter()
    assert all(limiter.allow("a", now=0.0) for _ in range(3))
    assert not limiter.allow("a", now=1.0)
    assert limiter.allow("a", now=contact.PER_SENDER_WINDOW_S + 1)


def test_only_admins_can_read_the_inbox(client):
    client.post("/contact", json=GOOD)
    assert client.get("/admin/contact-messages").status_code == 401
    user = client.get("/admin/contact-messages", headers={"Authorization": "Bearer user-token"})
    assert user.status_code == 403
    admin = client.get("/admin/contact-messages", headers={"Authorization": "Bearer admin-token"})
    assert admin.status_code == 200
    assert [m["topic"] for m in admin.json()] == ["billing"]


def test_admins_can_close_and_reopen_a_message(client):
    mid = uuid4()
    url = f"/admin/contact-messages/{mid}/handled"
    auth = {"Authorization": "Bearer admin-token"}
    assert client.post(url, json={"handled": True}, headers=auth).status_code == 204
    assert client.post(url, json={"handled": False}, headers=auth).status_code == 204
    assert client.handled[mid] is False
    denied = client.post(url, json={}, headers={"Authorization": "Bearer user-token"})
    assert denied.status_code == 403


@pytest.mark.parametrize(
    "email,expected",
    [
        (ADMIN, True),
        (" Owner@Example.com ", True),
        ("someone@example.com", False),
        ("", False),
        (None, False),
    ],
)
def test_admin_check_ignores_case_and_spacing(monkeypatch, email, expected):
    monkeypatch.setattr(contact, "ADMIN_EMAILS", frozenset({ADMIN}))
    assert contact.is_admin(email) is expected
