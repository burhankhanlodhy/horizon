"""Password reset and email verification.

Links carry a random single-use token; only its SHA-256 is stored, with an
expiry (reset 30 minutes, verification 7 days). "Forgot password" always
answers the same way and sends in the background, so it never reveals whether
an address has an account. A completed reset revokes every session and also
verifies the address (the user just proved they read its mail).

Verification gates only proxy key creation, which is how both the desktop app
and ``horizon login`` connect, and only while email can actually be sent
(mailer.configured()); otherwise nobody could ever verify.
"""

import asyncio
import hashlib
import html
import logging
import os
import secrets
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Literal

import asyncpg
import db
import mailer
import security
from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field

logger = logging.getLogger("contextshrink.account_email")

APP_URL = os.environ.get("APP_URL", "https://app.contextshrink.com").rstrip("/")
RESET_TTL = timedelta(minutes=30)
VERIFY_TTL = timedelta(days=7)
Purpose = Literal["reset", "verify"]

# Background sends are kept referenced until they finish.
_tasks: set[asyncio.Task] = set()


def verification_required() -> bool:
    if os.environ.get("EMAIL_VERIFICATION_REQUIRED", "1").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return False
    return mailer.configured()


def require_verified(user: asyncpg.Record) -> None:
    """Raise 403 for an unverified account while verification is enforced."""
    if verification_required() and user["email_verified_at"] is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Verify your email address first: open the link we sent to "
            f"{user['email']}, or resend it from the dashboard.",
        )


class _Window:
    """Sliding-window counter per key, in memory only (keys are hashed)."""

    def __init__(self, limit: int, window_s: float) -> None:
        self.limit, self.window_s = limit, window_s
        self._salt = secrets.token_bytes(16)
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        k = hashlib.sha256(self._salt + key.encode("utf-8")).hexdigest()
        q = self._hits.setdefault(k, deque())
        while q and q[0] <= now - self.window_s:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        if len(self._hits) > 10_000:
            for stale in [
                x for x, v in self._hits.items() if not v or v[-1] <= now - self.window_s
            ]:
                del self._hits[stale]
        return True


forgot_by_email = _Window(3, 15 * 60)
forgot_by_client = _Window(10, 15 * 60)
resend_by_user = _Window(3, 15 * 60)


def _client(request: Request) -> str:
    return request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else "unknown"
    )


# ------------------------------------------------------------------ storage


async def issue_token(user_id, purpose: Purpose) -> str:
    """Create a token, retiring the user's earlier unused ones for the purpose."""
    token = secrets.token_urlsafe(32)
    ttl = RESET_TTL if purpose == "reset" else VERIFY_TTL
    async with db._conn().acquire() as conn, conn.transaction():
        await conn.execute(
            "UPDATE core.auth_tokens SET used_at = now() "
            "WHERE user_id = $1 AND purpose = $2 AND used_at IS NULL",
            user_id,
            purpose,
        )
        await conn.execute(
            "INSERT INTO core.auth_tokens(user_id,purpose,token_hash,expires_at) VALUES($1,$2,$3,$4)",
            user_id,
            purpose,
            security.hash_token(token),
            datetime.now(timezone.utc) + ttl,
        )
    return token


async def consume_token(token: str, purpose: Purpose) -> asyncpg.Record | None:
    """Mark a valid token used and return its user, or None if invalid."""
    return await db._conn().fetchrow(
        "UPDATE core.auth_tokens t SET used_at = now() FROM core.users u "
        "WHERE t.token_hash = $1 AND t.purpose = $2 AND t.used_at IS NULL "
        "AND t.expires_at > now() AND u.id = t.user_id "
        "RETURNING u.id AS user_id, u.email",
        security.hash_token(token),
        purpose,
    )


async def mark_verified(user_id) -> None:
    await db._conn().execute(
        "UPDATE core.users SET email_verified_at = COALESCE(email_verified_at, now()) WHERE id = $1",
        user_id,
    )


async def set_password(user_id, password_hash: str) -> None:
    async with db._conn().acquire() as conn, conn.transaction():
        await conn.execute(
            "UPDATE core.users SET password_hash = $2 WHERE id = $1", user_id, password_hash
        )
        await conn.execute(
            "UPDATE core.sessions SET revoked_at = now() WHERE user_id = $1 AND revoked_at IS NULL",
            user_id,
        )


# ------------------------------------------------------------------- emails


def _email(heading: str, lines: list[str], button: str, link: str, footer: str) -> tuple[str, str]:
    text = "\n\n".join([heading, *lines, f"{button}: {link}", footer, "— ContextShrink"])
    paras = "".join(f'<p style="margin:0 0 14px">{html.escape(x)}</p>' for x in lines)
    body = f"""<div style="font-family:Arial,Helvetica,sans-serif;max-width:520px;margin:0 auto;padding:24px;color:#1d1a16;line-height:1.5">
<p style="font-size:20px;font-weight:bold;margin:0 0 18px">{html.escape(heading)}</p>
{paras}
<p style="margin:22px 0"><a href="{html.escape(link, quote=True)}" style="background:#1d1a16;color:#f0e9da;padding:11px 18px;border-radius:6px;text-decoration:none;font-weight:bold">{html.escape(button)}</a></p>
<p style="margin:0 0 14px;font-size:13px;color:#6b6358">If the button doesn't work, paste this link into your browser:<br><span style="word-break:break-all">{html.escape(link)}</span></p>
<p style="margin:0;font-size:13px;color:#6b6358">{html.escape(footer)}</p>
<p style="margin:18px 0 0;font-size:13px;color:#6b6358">— ContextShrink</p>
</div>"""
    return text, body


async def send_verification(user_id, email: str, name: str) -> bool:
    token = await issue_token(user_id, "verify")
    link = f"{APP_URL}/verify-email?token={token}"
    text, body = _email(
        f"Confirm your email, {name}" if name else "Confirm your email",
        [
            "Thanks for signing up for ContextShrink. Confirm this is your address to create proxy keys and connect the desktop app."
        ],
        "Confirm email",
        link,
        "The link works for 7 days. If you didn't create an account, you can ignore this email.",
    )
    return await mailer.send(email, "Confirm your ContextShrink email", text, body)


async def send_reset(user_id, email: str) -> bool:
    token = await issue_token(user_id, "reset")
    link = f"{APP_URL}/reset-password?token={token}"
    text, body = _email(
        "Reset your password",
        [
            "Someone asked to reset the password for your ContextShrink account. Choose a new one with the link below."
        ],
        "Choose a new password",
        link,
        "The link works for 30 minutes and only once. If you didn't ask for this, ignore this email; your password stays the same.",
    )
    return await mailer.send(email, "Reset your ContextShrink password", text, body)


def in_background(coro) -> None:
    """Run a send without making the request wait or fail on it."""

    async def run():
        try:
            await coro
        except Exception as exc:
            logger.error("background email failed: %s", type(exc).__name__)

    task = asyncio.create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# ------------------------------------------------------------------- routes


class ForgotIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr


class ResetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(min_length=8, max_length=200)


class VerifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=20, max_length=200)


def install(app: FastAPI, current_user) -> None:
    @app.post("/auth/password/forgot", status_code=status.HTTP_202_ACCEPTED)
    async def forgot_password(body: ForgotIn, request: Request) -> dict:
        if not mailer.configured():
            # A global state, so it reveals nothing about any account.
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Password reset by email isn't available yet. Reach us through the "
                "Contact page on contextshrink.com and we'll help you back in.",
            )
        email = body.email.strip().lower()
        # Same answer whether or not the account exists, or the limit hit.
        reply = {
            "ok": True,
            "detail": "If an account uses that address, we've emailed a reset link.",
        }
        if not (forgot_by_client.allow(_client(request)) and forgot_by_email.allow(email)):
            return reply
        user = await db.get_user_by_email(email)
        if user is not None:
            in_background(send_reset(user["id"], user["email"]))
        return reply

    @app.post("/auth/password/reset", status_code=status.HTTP_204_NO_CONTENT)
    async def reset_password(body: ResetIn) -> None:
        row = await consume_token(body.token, "reset")
        if row is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "This reset link is invalid or has expired. Ask for a new one.",
            )
        await set_password(row["user_id"], security.hash_password(body.password))
        await mark_verified(row["user_id"])

    @app.post("/auth/email/verify", status_code=status.HTTP_204_NO_CONTENT)
    async def verify_email(body: VerifyIn) -> None:
        row = await consume_token(body.token, "verify")
        if row is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "This confirmation link is invalid or has expired. Send a new one from the dashboard.",
            )
        await mark_verified(row["user_id"])

    @app.post("/auth/email/resend", status_code=status.HTTP_202_ACCEPTED)
    async def resend_verification(user: asyncpg.Record = Depends(current_user)) -> dict:
        if user["email_verified_at"] is not None:
            return {"ok": True, "detail": "Your email is already confirmed."}
        if not mailer.configured():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Email isn't set up yet.")
        if not resend_by_user.allow(str(user["user_id"])):
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "We've sent a few already. Check your inbox and spam folder, or try again in 15 minutes.",
            )
        in_background(send_verification(user["user_id"], user["email"], user["name"]))
        return {"ok": True, "detail": f"We've sent a new link to {user['email']}."}
