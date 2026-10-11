"""Password reset, email confirmation and the mailer (no Postgres, no SMTP).

Run from the repo root with the API's requirements installed:
    python -m pytest api/tests
"""

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import account_email  # noqa: E402
import mailer  # noqa: E402

UID = uuid4()
GOOD_TOKEN = "t" * 43


@pytest.fixture
def env(monkeypatch):
    state = {"sent": [], "pending": [], "passwords": [], "verified": [], "consumed": []}
    monkeypatch.setattr(mailer, "SMTP_HOST", "smtp.example.com")
    for name in ("forgot_by_email", "forgot_by_client", "resend_by_user"):
        old = getattr(account_email, name)
        monkeypatch.setattr(account_email, name, account_email._Window(old.limit, old.window_s))

    async def send(to, subject, text, html=None):
        state["sent"].append((to, subject, text, html))
        return True

    async def issue_token(user_id, purpose):
        return f"{purpose}-token-for-{user_id}"

    async def consume_token(token, purpose):
        state["consumed"].append((token, purpose))
        return {"user_id": UID, "email": "ada@example.com"} if token == GOOD_TOKEN else None

    async def get_user_by_email(email):
        return {"id": UID, "email": email} if email == "ada@example.com" else None

    async def set_password(user_id, password_hash):
        state["passwords"].append((user_id, password_hash))

    async def mark_verified(user_id):
        state["verified"].append(user_id)

    monkeypatch.setattr(mailer, "send", send)
    monkeypatch.setattr(account_email, "issue_token", issue_token)
    monkeypatch.setattr(account_email, "consume_token", consume_token)
    monkeypatch.setattr(account_email, "set_password", set_password)
    monkeypatch.setattr(account_email, "mark_verified", mark_verified)
    monkeypatch.setattr(account_email.db, "get_user_by_email", get_user_by_email)
    monkeypatch.setattr(account_email, "in_background", state["pending"].append)
    return state


def flush(state):
    for coro in state["pending"]:
        asyncio.run(coro)
    state["pending"].clear()


@pytest.fixture
def client(env):
    users = {
        "unverified": {
            "user_id": UID,
            "email": "ada@example.com",
            "name": "Ada",
            "email_verified_at": None,
        },
        "verified": {
            "user_id": uuid4(),
            "email": "bo@example.com",
            "name": "Bo",
            "email_verified_at": datetime.now(timezone.utc),
        },
    }

    async def current_user(authorization: str | None = Header(default=None)):
        token = (authorization or "").removeprefix("Bearer ").strip()
        if token not in users:
            raise HTTPException(401, "Missing bearer token")
        return users[token]

    app = FastAPI()
    account_email.install(app, current_user)
    return TestClient(app)


# ----------------------------------------------------------- forgot password


def test_forgot_password_answers_the_same_for_known_and_unknown_addresses(client, env):
    known = client.post("/auth/password/forgot", json={"email": "ADA@example.com"})
    unknown = client.post("/auth/password/forgot", json={"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    flush(env)
    assert [s[0] for s in env["sent"]] == ["ada@example.com"]
    to, subject, text, html = env["sent"][0]
    assert "reset-password?token=reset-token-for-" in text and "30 minutes" in text
    assert html and "reset-password?token=" in html


def test_forgot_password_is_rate_limited_silently(client, env):
    for _ in range(5):
        r = client.post("/auth/password/forgot", json={"email": "ada@example.com"})
        assert r.status_code == 202
    flush(env)
    assert len(env["sent"]) == 3


def test_forgot_password_says_so_while_email_is_off(client, env, monkeypatch):
    monkeypatch.setattr(mailer, "SMTP_HOST", "")
    for email in ("ada@example.com", "nobody@example.com"):
        r = client.post("/auth/password/forgot", json={"email": email})
        assert r.status_code == 503 and "Contact page" in r.json()["detail"]
    assert env["pending"] == []


# ------------------------------------------------------------ reset password


def test_a_valid_reset_link_sets_the_password_and_confirms_the_email(client, env):
    r = client.post(
        "/auth/password/reset", json={"token": GOOD_TOKEN, "password": "new-long-password"}
    )
    assert r.status_code == 204
    ((user_id, password_hash),) = env["passwords"]
    assert user_id == UID and password_hash.startswith("$argon2")
    assert env["verified"] == [UID]
    assert env["consumed"] == [(GOOD_TOKEN, "reset")]


@pytest.mark.parametrize(
    "body,code",
    [
        ({"token": "x" * 43, "password": "new-long-password"}, 400),
        ({"token": GOOD_TOKEN, "password": "short"}, 422),
        ({"token": "short", "password": "new-long-password"}, 422),
    ],
)
def test_bad_reset_requests_change_nothing(client, env, body, code):
    assert client.post("/auth/password/reset", json=body).status_code == code
    assert env["passwords"] == [] and env["verified"] == []


# ------------------------------------------------------------ verify / resend


def test_a_confirmation_link_verifies_once(client, env):
    assert client.post("/auth/email/verify", json={"token": GOOD_TOKEN}).status_code == 204
    assert env["verified"] == [UID]
    assert client.post("/auth/email/verify", json={"token": "y" * 43}).status_code == 400


def test_resend_needs_a_session_and_is_rate_limited(client, env):
    assert client.post("/auth/email/resend").status_code == 401
    auth = {"Authorization": "Bearer unverified"}
    codes = [client.post("/auth/email/resend", headers=auth).status_code for _ in range(4)]
    assert codes == [202, 202, 202, 429]
    flush(env)
    assert len(env["sent"]) == 3
    assert "verify-email?token=verify-token-for-" in env["sent"][0][2]


def test_resend_for_a_verified_account_sends_nothing(client, env):
    r = client.post("/auth/email/resend", headers={"Authorization": "Bearer verified"})
    assert r.status_code == 202 and "already" in r.json()["detail"]
    flush(env)
    assert env["sent"] == []


def test_resend_without_email_configured_says_so(client, env, monkeypatch):
    monkeypatch.setattr(mailer, "SMTP_HOST", "")
    r = client.post("/auth/email/resend", headers={"Authorization": "Bearer unverified"})
    assert r.status_code == 503


# ------------------------------------------------------------------- gating


def test_unverified_accounts_cannot_create_keys_once_email_works(monkeypatch):
    unverified = {"email": "ada@example.com", "email_verified_at": None}
    verified = {"email": "bo@example.com", "email_verified_at": datetime.now(timezone.utc)}
    monkeypatch.setattr(mailer, "SMTP_HOST", "smtp.example.com")
    with pytest.raises(HTTPException) as err:
        account_email.require_verified(unverified)
    assert err.value.status_code == 403 and "ada@example.com" in err.value.detail
    account_email.require_verified(verified)


@pytest.mark.parametrize("host,flag", [("", "1"), ("smtp.example.com", "0")])
def test_nothing_is_gated_while_email_is_off_or_disabled(monkeypatch, host, flag):
    monkeypatch.setattr(mailer, "SMTP_HOST", host)
    monkeypatch.setenv("EMAIL_VERIFICATION_REQUIRED", flag)
    account_email.require_verified({"email": "ada@example.com", "email_verified_at": None})


# ------------------------------------------------------------------- mailer


def test_the_message_has_both_parts_and_a_message_id():
    msg = mailer.build_message("ada@example.com", "Hi", "plain", "<p>html</p>")
    assert msg["To"] == "ada@example.com" and msg["Message-ID"].endswith("@contextshrink.com>")
    assert [p.get_content_type() for p in msg.iter_parts()] == ["text/plain", "text/html"]


def test_emails_escape_names_in_html(monkeypatch):
    text, body = account_email._email("Hi <b>", ["a & b"], "Go", "https://x/?t=1&u=2", "f")
    assert "<b>" in text and "&lt;b&gt;" in body and "a &amp; b" in body
    assert 'href="https://x/?t=1&amp;u=2"' in body


def test_send_uses_starttls_and_login(monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context):
            calls.append(("starttls",))

        def login(self, user, password):
            calls.append(("login", user))

        def send_message(self, msg):
            calls.append(("send", msg["To"]))

    monkeypatch.setattr(mailer, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(mailer, "SMTP_USERNAME", "apikey")
    monkeypatch.setattr(mailer, "SMTP_SECURITY", "starttls")
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    assert asyncio.run(mailer.send("ada@example.com", "s", "t")) is True
    assert calls == [
        ("connect", "smtp.example.com", 587),
        ("starttls",),
        ("login", "apikey"),
        ("send", "ada@example.com"),
    ]


def test_send_reports_failure_instead_of_raising(monkeypatch):
    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(mailer, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(mailer.smtplib, "SMTP", boom)
    assert asyncio.run(mailer.send("ada@example.com", "s", "t")) is False
    monkeypatch.setattr(mailer, "SMTP_HOST", "")
    assert asyncio.run(mailer.send("ada@example.com", "s", "t")) is False
