"""Optional invite-only signup.

SIGNUP_MODE=open (the default) lets anyone sign up; SIGNUP_MODE=invite makes
/auth/signup require an invite code, for a gated launch. Admins
(contact.ADMIN_EMAILS) create codes on the dashboard, each with a note saying
who it is for, a use limit and an optional expiry, and share them as
https://app.contextshrink.com/signup?invite=<code>.

Codes are stored as typed (they are shown to admins again); a code is worth
one account per use, nothing more. Each account remembers the invite it used.
"""

import os
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

import asyncpg
import contact
import db
from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

# No 0/O, 1/I/L: codes get read aloud and retyped.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LEN = 10


def signup_mode() -> str:
    mode = os.environ.get("SIGNUP_MODE", "open").strip().lower()
    return "invite" if mode == "invite" else "open"


def invite_required() -> bool:
    return signup_mode() == "invite"


def new_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LEN))
    return f"{raw[:5]}-{raw[5:]}"


def normalize(code: str | None) -> str:
    """Upper-case, drop spaces and dashes, re-insert the dash: how codes are stored."""
    raw = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    return f"{raw[:5]}-{raw[5:]}" if len(raw) == _CODE_LEN else raw


class InviteIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    note: str = Field(default="", max_length=200)
    max_uses: int = Field(default=1, ge=1, le=1000)
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class InviteOut(BaseModel):
    id: str
    code: str
    note: str
    max_uses: int
    uses: int
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


# ----------------------------------------------------------------- database


async def claim(code: str) -> UUID | None:
    """Use up one slot of a live invite; None if the code cannot be used."""
    return await db._conn().fetchval(
        """
        UPDATE core.invites SET uses = uses + 1
        WHERE code = $1 AND revoked_at IS NULL AND uses < max_uses
          AND (expires_at IS NULL OR expires_at > now())
        RETURNING id
        """,
        normalize(code),
    )


async def release(invite_id: UUID) -> None:
    """Give back a slot claimed for a signup that then failed."""
    await db._conn().execute(
        "UPDATE core.invites SET uses = uses - 1 WHERE id = $1 AND uses > 0", invite_id
    )


async def attach(user_id, invite_id: UUID) -> None:
    await db._conn().execute(
        "UPDATE core.users SET invite_id = $2 WHERE id = $1", user_id, invite_id
    )


async def create(body: InviteIn, created_by) -> asyncpg.Record:
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)
        if body.expires_in_days
        else None
    )
    for _ in range(5):  # a collision in 31^10 codes is not going to happen twice
        try:
            return await db._conn().fetchrow(
                """
                INSERT INTO core.invites (code, note, max_uses, expires_at, created_by)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, code, note, max_uses, uses, expires_at, revoked_at, created_at
                """,
                new_code(),
                body.note,
                body.max_uses,
                expires_at,
                created_by,
            )
        except asyncpg.UniqueViolationError:
            continue
    raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not create an invite")


async def list_all(limit: int) -> list[asyncpg.Record]:
    return await db._conn().fetch(
        "SELECT id, code, note, max_uses, uses, expires_at, revoked_at, created_at "
        "FROM core.invites ORDER BY created_at DESC LIMIT $1",
        limit,
    )


async def revoke(invite_id: UUID) -> bool:
    result = await db._conn().execute(
        "UPDATE core.invites SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL",
        invite_id,
    )
    return result.endswith(" 1")


def _out(row) -> InviteOut:
    return InviteOut(**{**dict(row), "id": str(row["id"])})


# --------------------------------------------------------------- endpoints


def install(app: FastAPI, current_user) -> None:
    async def require_admin(user: asyncpg.Record = Depends(current_user)) -> asyncpg.Record:
        if not contact.is_admin(user["email"]):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Admins only")
        return user

    @app.get("/auth/signup-mode")
    async def get_signup_mode() -> dict:
        return {"invite_required": invite_required()}

    @app.get("/admin/invites", response_model=list[InviteOut])
    async def admin_list(limit: int = 200, _admin: asyncpg.Record = Depends(require_admin)):
        if not 1 <= limit <= 500:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "limit must be 1..500")
        return [_out(r) for r in await list_all(limit)]

    @app.post("/admin/invites", response_model=InviteOut, status_code=status.HTTP_201_CREATED)
    async def admin_create(body: InviteIn, admin: asyncpg.Record = Depends(require_admin)):
        return _out(await create(body, admin["id"]))

    @app.post("/admin/invites/{invite_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
    async def admin_revoke(invite_id: UUID, _admin: asyncpg.Record = Depends(require_admin)):
        if not await revoke(invite_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such active invite")
