"""ContextShrink control-plane API.

Session auth, account-owned proxy keys and private analytics. Only the
authenticated proxy service may submit usage events to the Postgres ledger.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

import account_email
import analytics
import asyncpg
import billing
import contact
import db
import invites
import security
import turnstile
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, EmailStr, Field

CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(
        ","
    )
    if o.strip()
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_pool()
    # Downgrades plans whose savings fee stays unpaid past the grace period.
    enforcer = asyncio.create_task(billing.enforce_unpaid_fees_forever())
    yield
    enforcer.cancel()
    await asyncio.gather(enforcer, return_exceptions=True)
    await db.close_pool()


app = FastAPI(title="ContextShrink Control Plane", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------- models


class SignupIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    # Cloudflare Turnstile pass from the signup page (required once enabled).
    turnstile_token: str | None = Field(default=None, max_length=4096)
    # Beta invite; required while SIGNUP_MODE=invite.
    invite_code: str | None = Field(default=None, max_length=64)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class UserOut(BaseModel):
    id: str
    name: str
    email: str


class AccountOut(UserOut):
    created_at: datetime
    plan: str
    subscription_status: str
    is_admin: bool = False
    email_verified: bool = True
    # True while unverified accounts may not create proxy keys.
    email_verification_required: bool = False


class SubscriptionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: Literal["free", "pro", "team"]


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def account_billing_estimate(user: asyncpg.Record) -> dict:
    """Calculate a private estimate from the authenticated account's proxy ledger."""
    row = await db._conn().fetchrow(
        "SELECT current_period_start, current_period_end, seat_count "
        "FROM core.subscriptions WHERE user_id=$1",
        user["user_id"],
    )
    now = datetime.now(timezone.utc)
    start = row["current_period_start"] if row else None
    end = row["current_period_end"] if row else None
    from_subscription = start is not None and end is not None and end > now
    if not from_subscription:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
    seats = int(row["seat_count"] or 1) if row else 1
    # Same ledger query and fee rule that billing.charge_savings_fee invoices.
    savings = _money(await billing.ledger_savings(user["user_id"], start, end))
    savings_fee = billing.savings_fee(savings)
    team_base = Decimal(seats * 5)
    current_plan = user["plan"]
    estimates = {
        "free": {"seat_fee": Decimal("0.00"), "savings_fee": Decimal("0.00"), "total": Decimal("0.00")},
        "pro": {"seat_fee": Decimal("0.00"), "savings_fee": savings_fee, "total": savings_fee},
        "team": {"seat_fee": team_base, "savings_fee": savings_fee, "total": team_base + savings_fee},
    }
    def as_usd(value: Decimal) -> float:
        return float(_money(value))
    return {
        "plan": current_plan,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "period_source": "subscription" if from_subscription else "calendar_month",
        "estimated_savings_usd": as_usd(savings),
        "savings_fee_threshold_usd": 20.0,
        "savings_fee_rate": 0.05,
        "seat_count": seats,
        "estimates": {plan: {key: as_usd(value) for key, value in breakdown.items()} for plan, breakdown in estimates.items()},
        "estimated_total_usd": as_usd(estimates.get(current_plan, estimates["free"])["total"]),
        "currency": "USD",
        "compression": await analytics.compression_entitlement(user["user_id"]),
        "payment_issue": await billing.fee_payment_issue(user["user_id"]),
    }


class AuthOut(BaseModel):
    token: str
    user: UserOut


class UsageEventIn(BaseModel):
    ts: str | None = None
    session_id: str | None = None
    agent: str | None = None
    model: str | None = None
    requests: int = 1
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_saved: int = 0
    savings_usd: float = 0.0
    cache_hit: bool | None = None


class IngestIn(BaseModel):
    events: list[UsageEventIn] = Field(min_length=1, max_length=1000)


# ----------------------------------------------------------------- helpers


async def current_user(
    authorization: str | None = Header(default=None),
) -> asyncpg.Record:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    row = await db.resolve_session(security.hash_token(token))
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired session")
    return row


# ------------------------------------------------------------ auth routes


# HEAD too: uptime monitors (UptimeRobot) probe with HEAD by default.
@app.api_route("/healthz", methods=["GET", "HEAD"])
async def healthz():
    # Uptime monitors call this, so it checks the database too: an API that
    # answers while Postgres is down cannot sign anyone in.
    try:
        await asyncio.wait_for(db._conn().fetchval("SELECT 1"), timeout=3)
    except Exception:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"ok": False, "service": "contextshrink-control-plane", "database": "down"},
        )
    return {"ok": True, "service": "contextshrink-control-plane"}


@app.post("/auth/signup", status_code=status.HTTP_201_CREATED, response_model=AuthOut)
async def signup(body: SignupIn, request: Request):
    await turnstile.verify(body.turnstile_token, request)
    email = body.email.strip().lower()
    if await db.get_user_by_email(email):
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists")
    invite_id = None
    if invites.invite_required():
        if not (body.invite_code or "").strip():
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "ContextShrink is invite-only for now: enter your invite code."
            )
        invite_id = await invites.claim(body.invite_code)
        if invite_id is None:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "This invite code is not valid, used up, or expired."
            )
    try:
        user = await db.create_user(email, body.name.strip(), security.hash_password(body.password))
    except asyncpg.UniqueViolationError:
        if invite_id is not None:
            await invites.release(invite_id)
        raise HTTPException(
            status.HTTP_409_CONFLICT, "An account with this email already exists"
        ) from None
    except BaseException:
        if invite_id is not None:
            await invites.release(invite_id)
        raise
    if invite_id is not None:
        await invites.attach(user["id"], invite_id)
    await db.ensure_subscription(str(user["id"]))
    if account_email.verification_required():
        account_email.in_background(
            account_email.send_verification(user["id"], user["email"], user["name"])
        )
    token = security.new_session_token()
    await db.create_session(str(user["id"]), security.hash_token(token))
    return AuthOut(
        token=token, user=UserOut(id=str(user["id"]), name=user["name"], email=user["email"])
    )


@app.post("/auth/login", response_model=AuthOut)
async def login(body: LoginIn):
    email = body.email.strip().lower()
    user = await db.get_user_by_email(email)
    if user is None or not security.verify_password(body.password, user["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    token = security.new_session_token()
    await db.create_session(str(user["id"]), security.hash_token(token))
    return AuthOut(
        token=token, user=UserOut(id=str(user["id"]), name=user["name"], email=user["email"])
    )


@app.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(authorization: str | None = Header(default=None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        await db.revoke_session(security.hash_token(token))


@app.get("/auth/me", response_model=AccountOut)
async def me(user: asyncpg.Record = Depends(current_user)):
    return AccountOut(
        id=str(user["user_id"]),
        name=user["name"],
        email=user["email"],
        created_at=user["created_at"],
        plan=user["plan"],
        subscription_status=user["subscription_status"],
        is_admin=contact.is_admin(user["email"]),
        email_verified=user["email_verified_at"] is not None,
        email_verification_required=account_email.verification_required(),
    )


@app.get("/subscription")
async def subscription(user: asyncpg.Record = Depends(current_user)):
    row = await db._conn().fetchrow(
        "SELECT stripe_customer_id, stripe_subscription_id, cancel_at_period_end, cancel_at, current_period_end "
        "FROM core.subscriptions WHERE user_id=$1",
        user["user_id"],
    )
    return {
        "plan": user["plan"], "status": user["subscription_status"],
        "billing_mode": "stripe" if billing.SECRET_KEY else "stripe_not_configured",
        "has_billing_account": bool(row and row["stripe_customer_id"]),
        "has_subscription": bool(row and row["stripe_subscription_id"]),
        "cancel_at_period_end": bool(row and row["cancel_at_period_end"]),
        "cancel_at": row["cancel_at"] if row else None,
        "current_period_end": row["current_period_end"] if row else None,
        "estimate": await account_billing_estimate(user),
    }


@app.put("/subscription")
async def change_subscription(body: SubscriptionIn, user: asyncpg.Record = Depends(current_user)):
    # Only Free is self-serve here. Paid plans start in Stripe Checkout
    # (POST /billing/checkout) and are granted solely by verified webhooks.
    if body.plan != "free":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Upgrade through checkout to add a payment method."
        )
    has_subscription = await db._conn().fetchval(
        "SELECT stripe_subscription_id IS NOT NULL FROM core.subscriptions WHERE user_id=$1",
        user["user_id"],
    )
    if has_subscription:
        # Cancelling must go through Stripe so the final savings fee is billed.
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Cancel your paid plan from Manage billing."
        )
    await db._conn().execute(
        "INSERT INTO core.subscriptions(user_id,plan,status) VALUES($1,$2,'active') "
        "ON CONFLICT(user_id) DO UPDATE SET plan=EXCLUDED.plan,status='active', "
        "current_period_start=NULL,current_period_end=NULL,updated_at=now()",
        user["user_id"],
        body.plan,
    )
    return {"plan": body.plan, "status": "active", "billing_mode": "free"}


@app.get("/billing/estimate")
async def billing_estimate(user: asyncpg.Record = Depends(current_user)):
    return await account_billing_estimate(user)


# --------------------------------------------------------- metrics routes


@app.post("/ingest/usage", status_code=status.HTTP_202_ACCEPTED)
async def ingest_usage(
    body: IngestIn,
    x_api_key: str | None = Header(default=None),
):
    """Legacy client-supplied telemetry is disabled; only the proxy may ingest."""
    raise HTTPException(status.HTTP_410_GONE, "Use trusted proxy analytics ingestion")


@app.get("/usage/summary")
async def usage_summary(
    days: int = 30,
    user: asyncpg.Record = Depends(current_user),
):
    if not 1 <= days <= 365:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "days must be 1..365")
    return await analytics.basic_usage(user, days)


analytics.install(app, current_user)
billing.install(app, current_user)
contact.install(app, current_user)
account_email.install(app, current_user)
invites.install(app, current_user)
