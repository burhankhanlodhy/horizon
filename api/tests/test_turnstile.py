"""Turnstile gate on public forms, and the database-aware health check.

Run from the repo root with the API's requirements installed:
    python -m pytest api/tests
"""

import sys
import urllib.error
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import turnstile  # noqa: E402


@pytest.fixture
def app():
    app = FastAPI()

    @app.post("/form")
    async def form(request: Request):
        body = await request.json()
        await turnstile.verify(body.get("turnstile_token"), request)
        return {"ok": True}

    return TestClient(app)


def test_everything_passes_while_turnstile_is_off(app, monkeypatch):
    monkeypatch.delenv("TURNSTILE_SECRET", raising=False)
    assert app.post("/form", json={}).status_code == 200


def test_a_missing_token_is_refused_once_enabled(app, monkeypatch):
    monkeypatch.setenv("TURNSTILE_SECRET", "secret")
    r = app.post("/form", json={})
    assert r.status_code == 400 and "security check" in r.json()["detail"]


def test_cloudflare_decides_with_the_token_and_client_ip(app, monkeypatch):
    monkeypatch.setenv("TURNSTILE_SECRET", "secret")
    seen = []

    def siteverify(token, remote_ip):
        seen.append((token, remote_ip))
        return {"success": token == "good"}

    monkeypatch.setattr(turnstile, "_siteverify", siteverify)
    headers = {"CF-Connecting-IP": "203.0.113.9"}
    assert app.post("/form", json={"turnstile_token": "good"}, headers=headers).status_code == 200
    assert app.post("/form", json={"turnstile_token": "bad"}, headers=headers).status_code == 400
    assert seen == [("good", "203.0.113.9"), ("bad", "203.0.113.9")]


def test_an_unreachable_cloudflare_fails_closed(app, monkeypatch):
    monkeypatch.setenv("TURNSTILE_SECRET", "secret")

    def down(token, remote_ip):
        raise urllib.error.URLError("timeout")

    monkeypatch.setattr(turnstile, "_siteverify", down)
    assert app.post("/form", json={"turnstile_token": "x"}).status_code == 503


def test_contact_form_requires_the_check_after_the_honeypot(monkeypatch):
    import contact

    monkeypatch.setenv("TURNSTILE_SECRET", "secret")
    monkeypatch.setattr(contact, "limiter", contact._RateLimiter())
    monkeypatch.setattr(turnstile, "_siteverify", lambda token, ip: {"success": token == "good"})
    saved = []

    async def save_message(body):
        saved.append(body)

    monkeypatch.setattr(contact, "save_message", save_message)
    app = FastAPI()
    contact.install(app, lambda: None)
    c = TestClient(app)
    msg = {"name": "Ada", "email": "ada@example.com", "message": "A question about plans."}
    assert c.post("/contact", json=msg).status_code == 400
    assert c.post("/contact", json={**msg, "turnstile_token": "good"}).status_code == 202
    # Bots tripping the honeypot still get a quiet 202 and nothing is stored.
    assert c.post("/contact", json={**msg, "website": "spam"}).status_code == 202
    assert len(saved) == 1


def test_health_check_reports_a_down_database(monkeypatch):
    import main

    class DownPool:
        async def fetchval(self, *a):
            raise OSError("connection refused")

    class UpPool:
        async def fetchval(self, *a):
            return 1

    app = FastAPI()
    app.get("/healthz")(main.healthz)
    c = TestClient(app)
    monkeypatch.setattr(main.db, "_conn", lambda: DownPool())
    r = c.get("/healthz")
    assert r.status_code == 503 and r.json()["database"] == "down"
    monkeypatch.setattr(main.db, "_conn", lambda: UpPool())
    assert c.get("/healthz").json()["ok"] is True
