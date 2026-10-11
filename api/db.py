"""Database pool, schema bootstrap, authentication and legacy metrics helpers."""

import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import asyncpg

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgres://contextshrink:contextshrink@localhost:5432/contextshrink"
)
SESSION_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", "30"))
# Days a savings fee may stay unpaid before paid features pause.
FEE_GRACE_DAYS = int(os.environ.get("FEE_GRACE_DAYS", "7"))

# Effective subscription status: an active plan with a savings fee unpaid past
# the grace period behaves as past_due (Free cap, no Advanced Analytics) until
# the invoice is paid. Expects the subscription row as "sub" and the grace
# days as a query parameter.
EFFECTIVE_STATUS_SQL = (
    "CASE WHEN COALESCE(sub.status,'active')='active' AND billing.fee_overdue(sub.user_id, {grace}) "
    "THEN 'past_due' ELSE COALESCE(sub.status,'active') END"
)

_pool: asyncpg.Pool | None = None

SCHEMA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


async def init_pool() -> asyncpg.Pool:
    global _pool
    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with _pool.acquire() as conn:
        with open(SCHEMA_FILE, encoding="utf-8") as fh:
            await conn.execute(fh.read())
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def _conn() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("database pool not initialised")
    return _pool


# ------------------------------------------------------------------- users


async def get_user_by_email(email: str) -> asyncpg.Record | None:
    return await _conn().fetchrow("SELECT * FROM core.users WHERE email = $1", email)


async def create_user(email: str, name: str, password_hash: str) -> asyncpg.Record:
    query = """
        INSERT INTO core.users (email, name, password_hash)
        VALUES ($1, $2, $3)
        RETURNING id, email, name, created_at
    """
    return await _conn().fetchrow(query, email, name, password_hash)


async def ensure_subscription(user_id: str) -> None:
    await _conn().execute(
        "INSERT INTO core.subscriptions (user_id) VALUES ($1) ON CONFLICT DO NOTHING",
        user_id,
    )


# ---------------------------------------------------------------- sessions


async def create_session(user_id: str, token_hash: str) -> datetime:
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)
    await _conn().execute(
        "INSERT INTO core.sessions (user_id, token_hash, expires_at) VALUES ($1, $2, $3)",
        user_id,
        token_hash,
        expires,
    )
    return expires


async def resolve_session(token_hash: str) -> asyncpg.Record | None:
    query = f"""
        SELECT u.id AS user_id, u.email, u.name, u.created_at, u.email_verified_at,
               s.id AS session_id,
               COALESCE(sub.plan, 'free') AS plan,
               {EFFECTIVE_STATUS_SQL.format(grace="$2")} AS subscription_status
        FROM core.sessions s
        JOIN core.users u ON u.id = s.user_id
        LEFT JOIN core.subscriptions sub ON sub.user_id = u.id
        WHERE s.token_hash = $1
          AND s.revoked_at IS NULL
          AND s.expires_at > now()
    """
    row = await _conn().fetchrow(query, token_hash, FEE_GRACE_DAYS)
    if row:
        await _conn().execute(
            "UPDATE core.sessions SET last_used_at = now() WHERE token_hash = $1",
            token_hash,
        )
    return row


async def revoke_session(token_hash: str) -> None:
    await _conn().execute(
        "UPDATE core.sessions SET revoked_at = now() WHERE token_hash = $1 AND revoked_at IS NULL",
        token_hash,
    )


# ---------------------------------------------------------------- api keys


async def resolve_api_key(key_hash: str) -> asyncpg.Record | None:
    row = await _conn().fetchrow(
        "SELECT id, user_id, scopes FROM core.api_keys WHERE key_hash = $1 AND revoked_at IS NULL",
        key_hash,
    )
    if row:
        await _conn().execute(
            "UPDATE core.api_keys SET last_used_at = now() WHERE id = $1", row["id"]
        )
    return row


# ----------------------------------------------------------------- metrics


async def insert_usage_events(rows: list[dict[str, Any]]) -> None:
    query = """
        INSERT INTO metrics.usage_events
            (user_id, ts, session_id, agent, model, requests,
             tokens_in, tokens_out, tokens_saved, savings_usd, cache_hit)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
    """
    async with _conn().acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                query,
                [
                    (
                        r["user_id"],
                        r.get("ts") or datetime.now(timezone.utc),
                        r.get("session_id"),
                        r.get("agent"),
                        r.get("model"),
                        int(r.get("requests", 1)),
                        int(r.get("tokens_in", 0)),
                        int(r.get("tokens_out", 0)),
                        int(r.get("tokens_saved", 0)),
                        float(r.get("savings_usd", 0)),
                        r.get("cache_hit"),
                    )
                    for r in rows
                ],
            )
            touched = {
                (r["user_id"], (r.get("ts") or datetime.now(timezone.utc)).date()) for r in rows
            }
            for uid, day in touched:
                await conn.execute(
                    "SELECT metrics.refresh_usage_daily($1, $2)",
                    uid,
                    date(day.year, day.month, day.day),
                )


async def usage_summary(user_id: str, days: int) -> dict[str, Any]:
    series = await _conn().fetch(
        """
        SELECT day::text AS date, requests, tokens_in, tokens_out,
               tokens_saved, savings_usd, cache_hits
        FROM metrics.usage_daily
        WHERE user_id = $1 AND day >= (CURRENT_DATE - $2::int)
        ORDER BY day
        """,
        user_id,
        days,
    )
    totals = await _conn().fetchrow(
        """
        SELECT COALESCE(SUM(requests), 0)     AS requests,
               COALESCE(SUM(tokens_saved), 0) AS tokens_saved,
               COALESCE(SUM(savings_usd), 0)  AS savings_usd
        FROM metrics.usage_daily
        WHERE user_id = $1 AND day >= (CURRENT_DATE - $2::int)
        """,
        user_id,
        days,
    )
    return {
        "days": days,
        "totals": {
            "requests": int(totals["requests"]),
            "tokens_saved": int(totals["tokens_saved"]),
            "savings_usd": float(totals["savings_usd"]),
        },
        "series": [dict(r) for r in series],
    }
