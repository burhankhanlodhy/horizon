"""Account key management and trusted, idempotent proxy telemetry.

All public reads derive ownership from the session, never a URL/header user ID.
Only the proxy service can write usage; account keys cannot invent telemetry.
"""

import csv
import hmac
import io
import json
import os
from datetime import datetime
from typing import Literal
from uuid import UUID

import db
import security
from fastapi import Depends, Header, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field


class KeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    scopes: list[Literal["proxy:messages", "proxy:responses"]] = Field(min_length=1, max_length=2)


class AuthorizeIn(BaseModel):
    key: str = Field(min_length=1, max_length=300)
    scope: Literal["proxy:messages", "proxy:responses"]


class RunIn(BaseModel):
    runtime_id: UUID
    started_at: datetime


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    event_id: UUID
    user_id: UUID
    key_id: UUID
    runtime_id: UUID
    occurred_at: datetime
    request_id: str = Field(max_length=150)
    provider: str = Field(default="unknown", max_length=100)
    model: str = Field(default="unknown", max_length=200)
    project: str | None = Field(default=None, max_length=100)
    agent: str | None = Field(default=None, max_length=100)
    status: int = Field(ge=100, le=599)
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    tokens_before: int = Field(default=0, ge=0)
    tokens_after: int = Field(default=0, ge=0)
    tokens_saved: int = Field(default=0, ge=0)
    cache_read: int = Field(default=0, ge=0)
    cache_write: int = Field(default=0, ge=0)
    response_cached: bool = False
    latency_ms: float = Field(default=0, ge=0)
    overhead_ms: float = Field(default=0, ge=0)
    ttfb_ms: float = Field(default=0, ge=0)
    # Net of keep-alive pings: a ping row carries its own cost as negative savings.
    savings_usd: float = 0
    cost_usd: float = Field(default=0, ge=0)
    pricing_basis: str = Field(default="unavailable", max_length=120)
    transforms: list[str] = Field(default_factory=list, max_length=100)
    # "keepalive": a cache keep-alive ping the proxy sent for the account, not
    # a request the user made. ``keepalive_usd``: on a request, the cache
    # rewrite it avoided because a ping kept its session warm (in savings_usd).
    kind: Literal["request", "keepalive"] = "request"
    keepalive_usd: float = Field(default=0, ge=0)


class BatchIn(BaseModel):
    events: list[EventIn] = Field(min_length=1, max_length=100)


async def service_auth(x_contextshrink_service_token: str | None = Header(default=None)):
    expected = os.environ.get("CONTEXTSHRINK_SERVICE_TOKEN", "")
    if not expected or not hmac.compare_digest(
        (x_contextshrink_service_token or "").encode(), expected.encode()
    ):
        raise HTTPException(401, "Invalid service credentials")


def public_key(row):
    return {**dict(row), "id": str(row["id"])}


# Free accounts get compression until they have saved this much in the
# current UTC calendar month; later requests pass through uncompressed.
FREE_SAVINGS_CAP_USD = float(os.environ.get("FREE_SAVINGS_CAP_USD", "20"))


async def compression_entitlement(user_id) -> dict:
    """Whether the proxy may compress for this account right now.

    Active Pro/Team plans are uncapped. Everything else, including a paid plan
    that is not active (e.g. a savings fee unpaid past the grace period), gets
    the Free cap.
    """
    row = await db._conn().fetchrow(
        f"""
        SELECT COALESCE(sub.plan,'free') AS plan,
               {db.EFFECTIVE_STATUS_SQL.format(grace="$2")} AS status,
               date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS cycle_start
        FROM core.users u LEFT JOIN core.subscriptions sub ON sub.user_id=u.id
        WHERE u.id=$1
        """,
        user_id,
        db.FEE_GRACE_DAYS,
    )
    cycle_start = row["cycle_start"]
    month = cycle_start.month
    cycle_end = (
        cycle_start.replace(year=cycle_start.year + 1, month=1)
        if month == 12
        else cycle_start.replace(month=month + 1)
    )
    base = {
        "plan": row["plan"],
        "cap_usd": FREE_SAVINGS_CAP_USD,
        "cycle_start": cycle_start.isoformat(),
        "cycle_end": cycle_end.isoformat(),
    }
    if row["plan"] in ("pro", "team") and row["status"] == "active":
        return {**base, "capped": False, "cycle_savings_usd": None, "compression_allowed": True}
    saved = await db._conn().fetchval(
        "SELECT COALESCE(SUM(NULLIF(data->>'savings_usd','')::numeric),0) "
        "FROM metrics.proxy_events WHERE user_id=$1 AND occurred_at >= $2",
        user_id,
        cycle_start,
    )
    saved = float(saved or 0)
    return {
        **base,
        "capped": True,
        "cycle_savings_usd": round(saved, 2),
        "compression_allowed": saved < FREE_SAVINGS_CAP_USD,
    }


# Rows the user's own tools sent; keep-alive pings count in tokens, cost and
# (negative) savings, never as requests. No untrusted string enters these SQL
# expressions.
REQUEST = "data->>'kind' IS DISTINCT FROM 'keepalive'"
KEEPALIVE = "data->>'kind' = 'keepalive'"
TOTALS = f"""
 count(*) FILTER (WHERE {REQUEST})::bigint AS requests,
 count(*) FILTER (WHERE {REQUEST} AND status < 400)::bigint AS completed,
 count(*) FILTER (WHERE {REQUEST} AND status >= 400 AND status <> 429)::bigint AS failed,
 count(*) FILTER (WHERE {REQUEST} AND status = 429)::bigint AS rate_limited,
 COALESCE(sum((data->>'tokens_in')::bigint),0)::bigint AS tokens_in,
 COALESCE(sum((data->>'tokens_out')::bigint),0)::bigint AS tokens_out,
 COALESCE(sum((data->>'tokens_saved')::bigint),0)::bigint AS tokens_saved,
 COALESCE(sum((data->>'tokens_before')::bigint),0)::bigint AS tokens_before,
 COALESCE(sum((data->>'tokens_after')::bigint),0)::bigint AS tokens_after,
 COALESCE(sum((data->>'cache_read')::bigint),0)::bigint AS cache_read,
 COALESCE(sum((data->>'cache_write')::bigint),0)::bigint AS cache_write,
 COALESCE(sum((data->>'savings_usd')::double precision),0) AS savings_usd,
 COALESCE(sum((data->>'cost_usd')::double precision),0) AS cost_usd,
 COALESCE(avg((data->>'latency_ms')::double precision) FILTER (WHERE {REQUEST}),0) AS latency_ms,
 COALESCE(avg((data->>'overhead_ms')::double precision) FILTER (WHERE {REQUEST}),0) AS overhead_ms,
 count(*) FILTER (WHERE (data->>'response_cached')::boolean)::bigint AS response_cached,
 count(*) FILTER (WHERE {KEEPALIVE})::bigint AS keepalive_pings,
 COALESCE(sum((data->>'cost_usd')::double precision) FILTER (WHERE {KEEPALIVE}),0)
   AS keepalive_spend_usd,
 count(*) FILTER (WHERE (data->>'keepalive_usd')::double precision > 0)::bigint
   AS keepalive_resumes,
 COALESCE(sum((data->>'keepalive_usd')::double precision),0) AS keepalive_avoided_usd
"""


def scope_filter(user, scope, days):
    # Fixed ownership predicate also covers feed, CSV and grouping queries.
    return (
        """user_id=$1 AND (
        $2='lifetime' OR
        ($2='history' AND occurred_at >= now() - ($3::int * interval '1 day')) OR
        ($2='session' AND runtime_id=(SELECT runtime_id FROM metrics.proxy_runs
                                     ORDER BY started_at DESC LIMIT 1)))""",
        (user["user_id"], scope, days),
    )


async def basic_usage(user, days):
    """Calendar-day usage available on every plan; all dates and comparisons are UTC."""
    current = "user_id=$1 AND occurred_at >= $2 AND occurred_at <= $3"
    async with (
        db._conn().acquire() as conn,
        conn.transaction(isolation="repeatable_read", readonly=True),
    ):
        bounds = await conn.fetchrow(
            "SELECT now() AS as_of, "
            "(date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') "
            "- (($1::int - 1) * interval '1 day') AS start",
            days,
        )
        args = (user["user_id"], bounds["start"], bounds["as_of"])
        totals = await conn.fetchrow(
            f"SELECT {TOTALS} FROM metrics.proxy_events WHERE {current}", *args
        )
        previous = await conn.fetchrow(
            f"SELECT {TOTALS} FROM metrics.proxy_events WHERE user_id=$1 "
            "AND occurred_at >= $2::timestamptz - ($3::int * interval '1 day') AND occurred_at < $2::timestamptz",
            user["user_id"],
            bounds["start"],
            days,
        )
        series = await conn.fetch(
            f"SELECT to_char(occurred_at AT TIME ZONE 'UTC','YYYY-MM-DD') AS date,{TOTALS} "
            f"FROM metrics.proxy_events WHERE {current} GROUP BY 1 ORDER BY 1",
            *args,
        )
        models = await conn.fetch(
            f"SELECT model AS name,count(*)::bigint AS requests FROM metrics.proxy_events "
            f"WHERE {current} AND {REQUEST} GROUP BY 1 ORDER BY requests DESC,name",
            *args,
        )
        providers = await conn.fetch(
            f"SELECT DISTINCT provider AS name FROM metrics.proxy_events WHERE {current} ORDER BY 1",
            *args,
        )
        # These are recorded activity spans per proxy process/agent, not live connections.
        sessions = await conn.fetch(
            f"SELECT runtime_id::text || ':' || COALESCE(agent,'Unspecified') AS id, "
            f"COALESCE(agent,'Unspecified') AS agent,{TOTALS}, "
            "min(occurred_at) AS started_at,max(occurred_at) AS last_activity, "
            "extract(epoch FROM (max(occurred_at)-min(occurred_at)))/60 AS minutes "
            f"FROM metrics.proxy_events WHERE {current} GROUP BY runtime_id,agent "
            "ORDER BY last_activity DESC,id LIMIT 5",
            *args,
        )
    return {
        "days": days,
        "as_of": bounds["as_of"],
        "start": bounds["start"],
        "totals": dict(totals),
        "previous": dict(previous),
        "series": [dict(r) for r in series],
        "models": [dict(r) for r in models],
        "providers": [dict(r) for r in providers],
        "sessions": [dict(r) for r in sessions],
    }


def install(app, current_user):
    async def advanced_user(user=Depends(current_user)):
        if user["plan"] not in ("pro", "team") or user["subscription_status"] != "active":
            raise HTTPException(
                403, "Advanced Analytics requires an active Pro or Team subscription"
            )
        return user

    @app.middleware("http")
    async def private_responses(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/keys")
    async def list_keys(user=Depends(current_user)):
        rows = await db._conn().fetch(
            """
            SELECT k.id,k.name,k.prefix,k.scopes,k.created_at,k.last_used_at,k.revoked_at,
                   (SELECT count(*) FROM metrics.proxy_events e WHERE e.key_id=k.id AND e.user_id=$1
                    AND e.data->>'kind' IS DISTINCT FROM 'keepalive') AS requests
            FROM core.api_keys k WHERE k.user_id=$1 ORDER BY k.created_at DESC
        """,
            user["user_id"],
        )
        return [public_key(r) for r in rows]

    @app.post("/keys", status_code=201)
    async def create_key(body: KeyIn, user=Depends(current_user)):
        if not body.name.strip():
            raise HTTPException(422, "Key name cannot be blank")
        key = "cs_live_" + security.new_session_token()
        row = await db._conn().fetchrow(
            """
            INSERT INTO core.api_keys(user_id,name,key_hash,prefix,scopes)
            VALUES($1,$2,$3,$4,$5) RETURNING id,name,prefix,scopes,created_at,last_used_at,revoked_at
        """,
            user["user_id"],
            body.name.strip(),
            security.hash_token(key),
            key[:16],
            list(set(body.scopes)),
        )
        return {**public_key(row), "key": key, "requests": 0}

    @app.delete("/keys/{key_id}", status_code=204)
    async def revoke_key(key_id: UUID, user=Depends(current_user)):
        row = await db._conn().fetchrow(
            """
            UPDATE core.api_keys SET revoked_at=COALESCE(revoked_at,now())
            WHERE id=$1 AND user_id=$2 RETURNING id
        """,
            key_id,
            user["user_id"],
        )
        if row is None:
            raise HTTPException(404, "Key not found")
        return Response(status_code=204)

    @app.post("/internal/proxy/authorize", dependencies=[Depends(service_auth)])
    async def authorize(body: AuthorizeIn):
        row = await db.resolve_api_key(security.hash_token(body.key))
        if row is None:
            raise HTTPException(401, "Invalid or revoked proxy key")
        if body.scope not in row["scopes"]:
            raise HTTPException(403, "Proxy key does not have the required scope")
        entitlement = await compression_entitlement(row["user_id"])
        return {
            "user_id": str(row["user_id"]),
            "key_id": str(row["id"]),
            "plan": entitlement["plan"],
            "compression_allowed": entitlement["compression_allowed"],
        }

    @app.post("/internal/analytics/run", dependencies=[Depends(service_auth)])
    async def register_run(body: RunIn):
        await db._conn().execute(
            """
            INSERT INTO metrics.proxy_runs(runtime_id,started_at) VALUES($1,$2)
            ON CONFLICT DO NOTHING
        """,
            body.runtime_id,
            body.started_at,
        )
        return {"ok": True}

    @app.post("/internal/analytics/events", dependencies=[Depends(service_auth)])
    async def ingest(body: BatchIn):
        async with db._conn().acquire() as conn, conn.transaction():
            for e in body.events:
                # Existing (including revoked) key ownership is mandatory.
                owned = await conn.fetchval(
                    "SELECT 1 FROM core.api_keys WHERE id=$1 AND user_id=$2", e.key_id, e.user_id
                )
                if not owned:
                    raise HTTPException(422, "Event key ownership mismatch")
                await conn.execute(
                    """
                    INSERT INTO metrics.proxy_events(event_id,user_id,key_id,runtime_id,occurred_at,status,model,provider,project,agent,data)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb) ON CONFLICT(event_id) DO NOTHING
                """,
                    e.event_id,
                    e.user_id,
                    e.key_id,
                    e.runtime_id,
                    e.occurred_at,
                    e.status,
                    e.model,
                    e.provider,
                    e.project,
                    e.agent,
                    e.model_dump_json(),
                )
        return {"accepted": len(body.events)}

    @app.get("/analytics/summary")
    async def summary(
        scope: Literal["session", "lifetime", "history"] = "lifetime",
        days: int = Query(30, ge=1, le=365),
        user=Depends(advanced_user),
    ):
        where, args = scope_filter(user, scope, days)
        async with (
            db._conn().acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            totals = await conn.fetchrow(
                f"SELECT {TOTALS} FROM metrics.proxy_events WHERE {where}", *args
            )
            series = await conn.fetch(
                f"SELECT to_char(occurred_at AT TIME ZONE 'UTC','YYYY-MM-DD') AS date,{TOTALS} FROM metrics.proxy_events WHERE {where} GROUP BY 1 ORDER BY 1",
                *args,
            )
            groups = {}
            for dimension in ("model", "provider", "project", "agent"):
                rows = await conn.fetch(
                    f"SELECT COALESCE({dimension},'Unspecified') AS name,{TOTALS} FROM metrics.proxy_events WHERE {where} GROUP BY 1 ORDER BY requests DESC LIMIT 50",
                    *args,
                )
                groups[dimension] = [dict(r) for r in rows]
        return {
            "scope": scope,
            "days": days,
            "totals": dict(totals),
            "series": [dict(r) for r in series],
            "groups": groups,
        }

    @app.get("/analytics/feed")
    async def feed(
        scope: Literal["session", "lifetime", "history"] = "lifetime",
        days: int = Query(30, ge=1, le=365),
        limit: int = Query(50, ge=1, le=200),
        user=Depends(advanced_user),
    ):
        where, args = scope_filter(user, scope, days)
        rows = await db._conn().fetch(
            f"SELECT data FROM metrics.proxy_events WHERE {where} ORDER BY occurred_at DESC,event_id DESC LIMIT $4",
            *args,
            limit,
        )
        return [json.loads(r["data"]) for r in rows]

    @app.get("/analytics/history.csv")
    async def export(
        scope: Literal["session", "lifetime", "history"] = "lifetime",
        days: int = Query(30, ge=1, le=365),
        user=Depends(advanced_user),
    ):
        where, args = scope_filter(user, scope, days)
        fields = [
            "occurred_at",
            "request_id",
            "provider",
            "model",
            "project",
            "agent",
            "status",
            "tokens_in",
            "tokens_out",
            "tokens_saved",
            "cache_read",
            "cache_write",
            "savings_usd",
            "cost_usd",
            "latency_ms",
        ]

        def csv_line(values):
            buf = io.StringIO()
            # Spreadsheet formula injection protection for client labels.
            csv.writer(buf).writerow(
                [
                    "'" + v
                    if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r"))
                    else v
                    for v in values
                ]
            )
            return buf.getvalue()

        async def stream():
            yield csv_line(fields)
            async with db._conn().acquire() as conn, conn.transaction(readonly=True):
                async for row in conn.cursor(
                    f"SELECT data FROM metrics.proxy_events WHERE {where} ORDER BY occurred_at",
                    *args,
                ):
                    data = json.loads(row["data"])
                    yield csv_line([data.get(f, "") for f in fields])

        return StreamingResponse(
            stream(),
            media_type="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="my-analytics.csv"',
                "Cache-Control": "no-store",
            },
        )
