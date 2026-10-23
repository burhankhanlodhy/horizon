"""Public contact form and the admin inbox that reads it.

POST /contact is unauthenticated (the landing page's Contact form). It keeps
only what the sender typed: no IP address or user agent is stored. Abuse is
held back by a hidden honeypot field, a per-sender rate limit keyed on a
salted hash of the client address (memory only, never written), and a global
hourly cap so a flood cannot fill the table.

Admins are the accounts whose email is listed in ADMIN_EMAILS
(comma-separated). They read and close messages on the dashboard.
"""

import hashlib
import os
import secrets
import time
from collections import deque
from datetime import datetime
from typing import Literal
from uuid import UUID

import asyncpg
import db
import turnstile
from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field

ADMIN_EMAILS = frozenset(
    e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()
)

#: Messages one sender may send per window, and the window length.
PER_SENDER_LIMIT = 3
PER_SENDER_WINDOW_S = 10 * 60
#: Messages accepted from everyone together per hour.
GLOBAL_HOURLY_LIMIT = int(os.environ.get("CONTACT_GLOBAL_HOURLY_LIMIT", "100"))

Topic = Literal["general", "support", "billing", "privacy", "security"]


def is_admin(email: str | None) -> bool:
    return bool(email) and email.strip().lower() in ADMIN_EMAILS


class ContactIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    topic: Topic = "general"
    message: str = Field(min_length=10, max_length=5000)
    # Honeypot: hidden from people, filled in by form-spamming bots.
    website: str = Field(default="", max_length=200)
    # Cloudflare Turnstile pass from the Contact page (required once enabled).
    turnstile_token: str | None = Field(default=None, max_length=4096)


class ContactMessageOut(BaseModel):
    id: str
    name: str
    email: str
    topic: str
    message: str
    created_at: datetime
    handled_at: datetime | None


class HandledIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    handled: bool = True


class _RateLimiter:
    """Sliding-window counts per sender and overall, in memory only."""

    def __init__(self) -> None:
        self._salt = secrets.token_bytes(16)
        self._by_sender: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()

    def _key(self, client: str) -> str:
        return hashlib.sha256(self._salt + client.encode("utf-8")).hexdigest()

    @staticmethod
    def _trim(q: deque[float], window: float, now: float) -> None:
        while q and q[0] <= now - window:
            q.popleft()

    def allow(self, client: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        self._trim(self._all, 3600, now)
        if len(self._all) >= GLOBAL_HOURLY_LIMIT:
            return False
        key = self._key(client)
        q = self._by_sender.setdefault(key, deque())
        self._trim(q, PER_SENDER_WINDOW_S, now)
        if len(q) >= PER_SENDER_LIMIT:
            return False
        q.append(now)
        self._all.append(now)
        # Forget senders whose window has passed so the map stays small.
        if len(self._by_sender) > 10_000:
            for k in [
                k for k, v in self._by_sender.items() if not v or v[-1] <= now - PER_SENDER_WINDOW_S
            ]:
                del self._by_sender[k]
        return True


limiter = _RateLimiter()


def _client_address(request: Request) -> str:
    # The API is reachable only through the Pi 5 gateway behind Cloudflare,
    # which sets CF-Connecting-IP; fall back to the socket peer elsewhere.
    return request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else "unknown"
    )


async def save_message(body: ContactIn) -> None:
    await db._conn().execute(
        "INSERT INTO core.contact_messages(name,email,topic,message) VALUES($1,$2,$3,$4)",
        body.name,
        body.email.lower(),
        body.topic,
        body.message,
    )


async def list_messages(state: str, limit: int) -> list[asyncpg.Record]:
    where = {
        "open": "WHERE handled_at IS NULL",
        "handled": "WHERE handled_at IS NOT NULL",
        "all": "",
    }[state]
    return await db._conn().fetch(
        "SELECT id,name,email,topic,message,created_at,handled_at "
        f"FROM core.contact_messages {where} ORDER BY created_at DESC LIMIT $1",
        limit,
    )


async def set_handled(message_id: UUID, handled: bool) -> bool:
    result = await db._conn().execute(
        "UPDATE core.contact_messages SET handled_at = CASE WHEN $2 THEN now() ELSE NULL END "
        "WHERE id = $1",
        message_id,
        handled,
    )
    return result.endswith(" 1")


def install(app: FastAPI, current_user) -> None:
    async def require_admin(user: asyncpg.Record = Depends(current_user)) -> asyncpg.Record:
        if user.get("sso_team_id") or not is_admin(user["email"]):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Admins only")
        return user

    @app.post("/contact", status_code=status.HTTP_202_ACCEPTED)
    async def contact(body: ContactIn, request: Request) -> dict:
        if body.website:
            # A bot filled the hidden field: look accepted, keep nothing.
            return {"ok": True}
        await turnstile.verify(body.turnstile_token, request)
        if not limiter.allow(_client_address(request)):
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many messages. Please wait a few minutes and try again.",
            )
        await save_message(body)
        return {"ok": True}

    @app.get("/admin/contact-messages", response_model=list[ContactMessageOut])
    async def admin_messages(
        state: Literal["open", "handled", "all"] = "open",
        limit: int = 200,
        _admin: asyncpg.Record = Depends(require_admin),
    ):
        if not 1 <= limit <= 500:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "limit must be 1..500")
        rows = await list_messages(state, limit)
        return [ContactMessageOut(**{**dict(r), "id": str(r["id"])}) for r in rows]

    @app.post(
        "/admin/contact-messages/{message_id}/handled", status_code=status.HTTP_204_NO_CONTENT
    )
    async def admin_mark_handled(
        message_id: UUID,
        body: HandledIn,
        _admin: asyncpg.Record = Depends(require_admin),
    ) -> None:
        if not await set_handled(message_id, body.handled):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such message")
