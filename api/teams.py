"""Team workspace membership and invitation API.

Team creation and membership are additive to personal accounts. Stripe webhooks
remain the only authority that changes a team's subscription to an active state.
"""

import hashlib
import html
import os
import secrets
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote
from uuid import UUID

import db
import mailer
from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field

APP_URL = os.environ.get("APP_URL", "https://app.contextshrink.com").rstrip("/")
ROLE_RANK = {"member": 1, "admin": 2, "owner": 3}


def enabled() -> bool:
    return os.environ.get("CONTEXTSHRINK_TEAMS_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _guard_enabled() -> None:
    if not enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Teams are not available")


def _uid(user: Any) -> Any:
    value = user.get("user_id") if hasattr(user, "get") else None
    if value is None:
        value = user.get("id") if hasattr(user, "get") else None
    if value is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid user")
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid user") from exc


def _team_id(team_id: UUID | str) -> UUID:
    try:
        return team_id if isinstance(team_id, UUID) else UUID(str(team_id))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Team not found") from exc


async def context(user: Any, team_id: UUID | str, minimum_role: str = "member") -> dict:
    """Return active membership context; pending owners can still manage setup."""
    _guard_enabled()
    if minimum_role not in ROLE_RANK:
        raise ValueError("minimum_role must be member, admin, or owner")
    team_id = _team_id(team_id)
    if user.get("sso_team_id") and str(user["sso_team_id"]) != str(team_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This SSO session is limited to its Team workspace")
    row = await db._conn().fetchrow(
        """SELECT t.id, t.name, ts.status, ts.seat_count, ts.pending_seat_count,
                  m.id AS membership_id, m.role
           FROM core.teams t
           JOIN core.team_memberships m ON m.team_id=t.id AND m.user_id=$2 AND m.left_at IS NULL
           JOIN core.team_subscriptions ts ON ts.team_id=t.id
           WHERE t.id=$1""",
        team_id, _uid(user),
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Team not found")
    if ROLE_RANK.get(row["role"], 0) < ROLE_RANK[minimum_role]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient team role")
    return {
        "id": row["id"], "team_id": row["id"], "name": row["name"],
        "membership_id": row["membership_id"], "role": row["role"],
        "status": row["status"], "seat_count": row["seat_count"],
        "pending_seat_count": row.get("pending_seat_count"),
    }


async def _lock_actor(conn, team_id: UUID, membership_id: UUID, minimum_role: str) -> str:
    row = await conn.fetchrow(
        """SELECT role FROM core.team_memberships
           WHERE id=$1 AND team_id=$2 AND left_at IS NULL FOR UPDATE""",
        membership_id, team_id,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Team membership not found")
    if ROLE_RANK.get(row["role"], 0) < ROLE_RANK[minimum_role]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient team role")
    return row["role"]


async def list_for_user(user: Any) -> list[dict]:
    _guard_enabled()
    rows = await db._conn().fetch(
        """SELECT t.id, t.name, m.role, ts.status, ts.seat_count, t.created_at,
                  (SELECT count(*) FROM core.team_memberships x
                   WHERE x.team_id=t.id AND x.left_at IS NULL) AS member_count,
                  (SELECT count(*) FROM core.team_invitations i
                   WHERE i.team_id=t.id AND i.status='pending' AND i.expires_at>now()) AS reserved_count
           FROM core.teams t
           JOIN core.team_memberships m ON m.team_id=t.id AND m.user_id=$1 AND m.left_at IS NULL
           JOIN core.team_subscriptions ts ON ts.team_id=t.id
           ORDER BY t.created_at""",
        _uid(user),
    )
    return [dict(r) for r in rows]


class TeamCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    seats: int = Field(default=3, ge=3, le=500)


class InvitationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    role: str = "member"


class RoleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str


class AcceptInvitation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=32, max_length=256)


class OwnershipTransfer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    membership_id: UUID


async def _send_invitation(email: str, team_name: str, token: str) -> bool:
    url = f"{APP_URL}/team-invitations/accept?token={quote(token)}"
    body = f"You have been invited to join {team_name} on ContextShrink.\n\nAccept within 7 days: {url}"
    html_body = f'<p>You have been invited to join {html.escape(team_name)} on ContextShrink.</p><p><a href="{html.escape(url, quote=True)}">Accept invitation</a></p><p>This link expires in 7 days.</p>'
    return await mailer.send(email, f"Invitation to join {team_name}", body, html_body)


def _require_reservation_entitlement(team: dict) -> None:
    if team["status"] not in ("active", "trialing"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Activate the team subscription before inviting members",
        )


def _capacity_limit(subscription: dict) -> int:
    purchased = int(subscription["seat_count"])
    scheduled = subscription.get("pending_seat_count")
    return min(purchased, int(scheduled)) if scheduled is not None else purchased


def _new_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode()).hexdigest()


def install(app: FastAPI, current_user) -> None:
    @app.get("/teams")
    async def teams(user=Depends(current_user)):
        return {"teams": await list_for_user(user)}

    @app.post("/teams", status_code=status.HTTP_201_CREATED)
    async def create_team(body: TeamCreate, user=Depends(current_user)):
        _guard_enabled()
        if user.get("email_verified_at") is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Verify your email before creating a team")
        user_id = _uid(user)
        name = body.name.strip()
        if not name:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Team name is required")
        conn = db._conn()
        async with conn.acquire() as c:
            async with c.transaction():
                # One active Team membership per user in v1, including a pending
                # subscription while checkout is arranged.
                await c.execute("SELECT pg_advisory_xact_lock(hashtext('team-membership-v1'))")
                existing = await c.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM core.team_memberships WHERE user_id=$1 AND left_at IS NULL)",
                    user_id,
                )
                if existing:
                    raise HTTPException(status.HTTP_409_CONFLICT, "Leave your current team before creating another")
                team = await c.fetchrow(
                    "INSERT INTO core.teams(name,created_by) VALUES($1,$2) RETURNING id,name,created_at",
                    name, user_id,
                )
                await c.execute(
                    "INSERT INTO core.team_memberships(team_id,user_id,role) VALUES($1,$2,'owner')",
                    team["id"], user_id,
                )
                sub = await c.fetchrow(
                    """INSERT INTO core.team_subscriptions(team_id,seat_count,status)
                       VALUES($1,$2,'pending') RETURNING seat_count,status""",
                    team["id"], body.seats,
                )
        return {"team": {"id": team["id"], "name": team["name"], "role": "owner",
                          "status": sub["status"], "seat_count": sub["seat_count"],
                          "member_count": 1, "reserved_count": 0,
                          "created_at": team["created_at"]}}

    @app.get("/teams/{team_id}")
    async def get_team(team_id: UUID, user=Depends(current_user)):
        member = await context(user, team_id)
        if member["role"] in ("owner", "admin"):
            conn = db._conn()
            members = await conn.fetch(
                """SELECT m.id AS membership_id,m.user_id,u.email,u.name,m.role,m.joined_at
                   FROM core.team_memberships m JOIN core.users u ON u.id=m.user_id
                   WHERE m.team_id=$1 AND m.left_at IS NULL ORDER BY m.joined_at""", team_id,
            )
            invitations = await conn.fetch(
                """SELECT id,email,role,status,expires_at,created_at FROM core.team_invitations
                   WHERE team_id=$1 AND status='pending' AND expires_at>now() ORDER BY created_at""", team_id,
            )
            counts = await conn.fetchrow(
                """SELECT count(*) FILTER(WHERE m.left_at IS NULL) AS member_count,
                          (SELECT count(*) FROM core.team_invitations i WHERE i.team_id=$1
                           AND i.status='pending' AND i.expires_at>now()) AS reserved_count
                   FROM core.team_memberships m WHERE m.team_id=$1""", team_id,
            )
            return {"team": {k: member[k] for k in ("id", "name", "role", "status", "seat_count", "pending_seat_count")}
                    | dict(counts), "members": [dict(x) for x in members],
                    "invitations": [dict(x) for x in invitations]}
        own = await db._conn().fetchrow(
            """SELECT m.id AS membership_id,m.user_id,u.email,u.name,m.role,m.joined_at
               FROM core.team_memberships m JOIN core.users u ON u.id=m.user_id WHERE m.id=$1""",
            member["membership_id"],
        )
        return {"team": {k: member[k] for k in ("id", "name", "role", "status", "seat_count")},
                "members": [dict(own)], "invitations": []}

    @app.post("/teams/{team_id}/invitations", status_code=status.HTTP_201_CREATED)
    async def invite(team_id: UUID, body: InvitationCreate, user=Depends(current_user)):
        _guard_enabled()
        ctx = await context(user, team_id, "admin")
        _require_reservation_entitlement(ctx)
        role = body.role.strip().lower()
        if role not in ("member", "admin"):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invitations may grant member or admin role")
        if role == "admin" and ctx["role"] != "owner":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner may invite an admin")
        email = str(body.email).strip().lower()
        if email == str(user["email"]).strip().lower():
            raise HTTPException(status.HTTP_409_CONFLICT, "You already belong to this team")
        token, token_hash = _new_token()
        conn = db._conn()
        async with conn.acquire() as c:
            async with c.transaction():
                await c.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"team:{team_id}:capacity")
                await _lock_actor(c, team_id, ctx["membership_id"], "admin")
                state = await c.fetchrow(
                    """SELECT ts.seat_count,ts.pending_seat_count,ts.status,
                       (SELECT count(*) FROM core.team_memberships m WHERE m.team_id=ts.team_id AND m.left_at IS NULL) AS occupied,
                       (SELECT count(*) FROM core.team_invitations i WHERE i.team_id=ts.team_id
                         AND i.status='pending' AND i.expires_at>now()) AS reserved
                       FROM core.team_subscriptions ts WHERE ts.team_id=$1 FOR UPDATE""", team_id,
                )
                if state is None:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, "Team not found")
                _require_reservation_entitlement(state)
                if int(state["occupied"]) + int(state["reserved"]) >= _capacity_limit(state):
                    raise HTTPException(status.HTTP_409_CONFLICT, "No available team seats")
                taken = await c.fetchval(
                    """SELECT EXISTS(SELECT 1 FROM core.team_memberships m JOIN core.users u ON u.id=m.user_id
                       WHERE lower(u.email)=lower($1) AND m.left_at IS NULL)""", email,
                )
                if taken:
                    raise HTTPException(status.HTTP_409_CONFLICT, "This account already belongs to a team")
                pending = await c.fetchrow(
                    """SELECT id FROM core.team_invitations WHERE team_id=$1 AND lower(email)=lower($2)
                       AND status='pending' AND expires_at>now()""", team_id, email,
                )
                if pending:
                    raise HTTPException(status.HTTP_409_CONFLICT, "An active invitation already exists")
                inv = await c.fetchrow(
                    """INSERT INTO core.team_invitations(team_id,email,role,token_hash,expires_at,invited_by)
                       VALUES($1,$2,$3,$4,now()+interval '7 days',$5)
                       RETURNING id,email,role,status,expires_at,created_at""",
                    team_id, email, role, token_hash, ctx["membership_id"],
                )
                team_name = ctx["name"]
        sent = await _send_invitation(email, team_name, token)
        result = dict(inv)
        result["email_sent"] = sent
        return {"invitation": result}

    async def resend_invitation(team_id: UUID, invitation_id: UUID, user):
        ctx = await context(user, team_id, "admin")
        _require_reservation_entitlement(ctx)
        token, token_hash = _new_token()
        conn = db._conn()
        async with conn.acquire() as c:
            async with c.transaction():
                await _lock_actor(c, team_id, ctx["membership_id"], "admin")
                current = await c.fetchrow(
                    """SELECT id,email,role,status,expires_at FROM core.team_invitations
                       WHERE team_id=$1 AND id=$2 FOR UPDATE""", team_id, invitation_id,
                )
                if current is None or current["status"] != "pending":
                    raise HTTPException(status.HTTP_404_NOT_FOUND, "Invitation not found")
                if current["role"] == "admin" and ctx["role"] != "owner":
                    raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner may resend an admin invitation")
                state = await c.fetchrow(
                    """SELECT ts.seat_count,ts.pending_seat_count,ts.status,
                       (SELECT count(*) FROM core.team_memberships m WHERE m.team_id=ts.team_id
                         AND m.left_at IS NULL) AS occupied,
                       (SELECT count(*) FROM core.team_invitations i WHERE i.team_id=ts.team_id
                         AND i.status='pending' AND i.expires_at>now()) AS reserved
                       FROM core.team_subscriptions ts WHERE ts.team_id=$1 FOR UPDATE""", team_id,
                )
                if state is None:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, "Team not found")
                _require_reservation_entitlement(state)
                duplicate = await c.fetchval(
                    """SELECT EXISTS(SELECT 1 FROM core.team_invitations
                       WHERE team_id=$1 AND id<>$2 AND lower(email)=lower($3)
                         AND status='pending' AND expires_at>now())""",
                    team_id, invitation_id, current["email"],
                )
                already_member = await c.fetchval(
                    """SELECT EXISTS(SELECT 1 FROM core.team_memberships m JOIN core.users u ON u.id=m.user_id
                       WHERE lower(u.email)=lower($1) AND m.left_at IS NULL)""", current["email"],
                )
                if duplicate or already_member:
                    raise HTTPException(status.HTTP_409_CONFLICT, "An active invitation or membership already exists")
                if current["expires_at"] <= datetime.now(timezone.utc):
                    if int(state["occupied"]) + int(state["reserved"]) >= _capacity_limit(state):
                        raise HTTPException(status.HTTP_409_CONFLICT, "No available team seats")
                row = await c.fetchrow(
                    """UPDATE core.team_invitations SET token_hash=$3,expires_at=now()+interval '7 days',
                         updated_at=now() WHERE team_id=$1 AND id=$2 AND status='pending'
                         RETURNING id,email,role,status,expires_at,created_at""",
                    team_id, invitation_id, token_hash,
                )
                if row is None:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, "Invitation not found")
        sent = await _send_invitation(row["email"], ctx["name"], token)
        return {"invitation": dict(row) | {"email_sent": sent}}

    @app.post("/teams/{team_id}/invitations/{invitation_id}/resend")
    async def resend(team_id: UUID, invitation_id: UUID, user=Depends(current_user)):
        _guard_enabled()
        return await resend_invitation(team_id, invitation_id, user)

    @app.delete("/teams/{team_id}/invitations/{invitation_id}")
    async def revoke_invitation(team_id: UUID, invitation_id: UUID, user=Depends(current_user)):
        _guard_enabled()
        ctx = await context(user, team_id, "admin")
        conn = db._conn()
        async with conn.acquire() as c:
            async with c.transaction():
                await _lock_actor(c, team_id, ctx["membership_id"], "admin")
                row = await c.fetchrow(
                    """UPDATE core.team_invitations SET status='revoked',revoked_at=now(),updated_at=now()
                       WHERE team_id=$1 AND id=$2 AND status='pending' RETURNING id""", team_id, invitation_id,
                )
                if row is None:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, "Invitation not found")
        return {"ok": True}

    @app.post("/team-invitations/accept")
    async def accept(body: AcceptInvitation, user=Depends(current_user)):
        _guard_enabled()
        email = str(user["email"]).strip().lower()
        if user.get("email_verified_at") is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Verify your email before accepting an invitation")
        token_hash = hashlib.sha256(body.token.encode()).hexdigest()
        conn = db._conn()
        async with conn.acquire() as c:
            async with c.transaction():
                await c.execute("SELECT pg_advisory_xact_lock(hashtext('team-membership-v1'))")
                inv = await c.fetchrow(
                    """SELECT i.*,t.name FROM core.team_invitations i JOIN core.teams t ON t.id=i.team_id
                       WHERE i.token_hash=$1 FOR UPDATE OF i""", token_hash,
                )
                if inv is None or inv["status"] != "pending" or inv["expires_at"] <= datetime.now(timezone.utc):
                    raise HTTPException(status.HTTP_410_GONE, "Invitation is expired or no longer available")
                if inv["email"].strip().lower() != email:
                    raise HTTPException(status.HTTP_403_FORBIDDEN, "Sign in with the invited verified email")
                if await c.fetchval("SELECT EXISTS(SELECT 1 FROM core.team_memberships WHERE user_id=$1 AND left_at IS NULL)", _uid(user)):
                    raise HTTPException(status.HTTP_409_CONFLICT, "You already belong to a team")
                sub = await c.fetchrow(
                    """SELECT ts.seat_count,ts.pending_seat_count,ts.status,
                       (SELECT count(*) FROM core.team_memberships m WHERE m.team_id=ts.team_id AND m.left_at IS NULL) AS occupied,
                       (SELECT count(*) FROM core.team_invitations x WHERE x.team_id=ts.team_id AND x.status='pending'
                         AND x.expires_at>now()) AS reserved FROM core.team_subscriptions ts
                       WHERE ts.team_id=$1 FOR UPDATE""", inv["team_id"],
                )
                if sub is None or int(sub["occupied"]) + int(sub["reserved"]) > _capacity_limit(sub):
                    raise HTTPException(status.HTTP_409_CONFLICT, "No available team seats")
                _require_reservation_entitlement(sub)
                membership = await c.fetchrow(
                    """INSERT INTO core.team_memberships(team_id,user_id,role)
                       VALUES($1,$2,$3) RETURNING id,team_id,user_id,role,joined_at""",
                    inv["team_id"], _uid(user), inv["role"],
                )
                await c.execute(
                    "UPDATE core.team_invitations SET status='accepted',accepted_at=now(),updated_at=now() WHERE id=$1",
                    inv["id"],
                )
        return {"team": {"id": inv["team_id"], "name": inv["name"], "role": membership["role"]},
                "membership": dict(membership)}

    @app.delete("/teams/{team_id}/members/{membership_id}")
    async def remove_member(team_id: UUID, membership_id: UUID, user=Depends(current_user)):
        _guard_enabled()
        actor = await context(user, team_id)
        conn = db._conn()
        async with conn.acquire() as c:
            async with c.transaction():
                actor_role = await _lock_actor(c, team_id, actor["membership_id"], "member")
                target = await c.fetchrow(
                    "SELECT id,user_id,role FROM core.team_memberships WHERE id=$1 AND team_id=$2 AND left_at IS NULL FOR UPDATE",
                    membership_id, team_id,
                )
                if target is None:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")
                if target["role"] == "owner":
                    raise HTTPException(status.HTTP_409_CONFLICT, "Transfer ownership before removing the owner")
                self_leave = actor["membership_id"] == target["id"] and target["role"] in ("admin", "member")
                if actor_role == "member" and not self_leave:
                    raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient team role")
                if actor_role == "admin" and not self_leave and target["role"] != "member":
                    raise HTTPException(status.HTTP_403_FORBIDDEN, "Admins may remove ordinary members only")
                await c.execute("UPDATE core.team_memberships SET left_at=now() WHERE id=$1", membership_id)
                await c.execute(
                    "UPDATE core.api_keys SET revoked_at=now() WHERE membership_id=$1 AND revoked_at IS NULL",
                    membership_id,
                )
        return {"ok": True}

    @app.patch("/teams/{team_id}/members/{membership_id}")
    async def change_role(team_id: UUID, membership_id: UUID, body: RoleUpdate, user=Depends(current_user)):
        _guard_enabled()
        actor = await context(user, team_id, "owner")
        role = body.role.strip().lower()
        if role not in ("admin", "member"):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Role must be admin or member")
        conn = db._conn()
        async with conn.acquire() as c:
            async with c.transaction():
                await _lock_actor(c, team_id, actor["membership_id"], "owner")
                row = await c.fetchrow(
                    """UPDATE core.team_memberships SET role=$3 WHERE id=$1 AND team_id=$2 AND left_at IS NULL
                       AND role<>'owner' RETURNING id,user_id,role,joined_at""", membership_id, team_id, role,
                )
                if row is None:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")
        return {"membership": dict(row)}

    @app.post("/teams/{team_id}/transfer-ownership")
    async def transfer_ownership(team_id: UUID, body: OwnershipTransfer, user=Depends(current_user)):
        _guard_enabled()
        actor = await context(user, team_id, "owner")
        target_id = body.membership_id
        conn = db._conn()
        async with conn.acquire() as c:
            async with c.transaction():
                await _lock_actor(c, team_id, actor["membership_id"], "owner")
                target = await c.fetchrow(
                    "SELECT id FROM core.team_memberships WHERE id=$1 AND team_id=$2 AND left_at IS NULL FOR UPDATE",
                    target_id, team_id,
                )
                if target is None:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")
                await c.execute("UPDATE core.team_memberships SET role='admin' WHERE team_id=$1 AND role='owner' AND left_at IS NULL", team_id)
                await c.execute("UPDATE core.team_memberships SET role='owner' WHERE id=$1", target_id)
        return {"ok": True, "owner_membership_id": target_id}
