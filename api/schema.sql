-- ContextShrink control-plane schema.
-- Idempotent: safe to run on every container start (docker-entrypoint runs it
-- only on first init, but the API also executes it as a startup safety net).

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================= core schema
CREATE SCHEMA IF NOT EXISTS core;

CREATE TABLE IF NOT EXISTS core.users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT        NOT NULL UNIQUE,
    password_hash TEXT        NOT NULL,
    name          TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Sessions hold SHA-256 hashes of opaque bearer tokens (never the token itself).
CREATE TABLE IF NOT EXISTS core.sessions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID        NOT NULL REFERENCES core.users (id) ON DELETE CASCADE,
    token_hash   TEXT        NOT NULL UNIQUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    last_used_at TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON core.sessions (user_id);

CREATE TABLE IF NOT EXISTS core.api_keys (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID        NOT NULL REFERENCES core.users (id) ON DELETE CASCADE,
    name         TEXT        NOT NULL,
    key_hash     TEXT        NOT NULL UNIQUE,
    prefix       TEXT        NOT NULL,
    scopes       TEXT[]      NOT NULL DEFAULT '{proxy:messages,stats:read}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON core.api_keys (user_id);

CREATE TABLE IF NOT EXISTS core.subscriptions (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            UUID        NOT NULL UNIQUE REFERENCES core.users (id) ON DELETE CASCADE,
    plan               TEXT        NOT NULL DEFAULT 'free',
    status             TEXT        NOT NULL DEFAULT 'active',
    seat_count         INTEGER     NOT NULL DEFAULT 1 CHECK (seat_count >= 1),
    current_period_start TIMESTAMPTZ,
    current_period_end TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Keep existing installations compatible as the billing estimate evolves.
ALTER TABLE core.subscriptions
    ADD COLUMN IF NOT EXISTS seat_count INTEGER NOT NULL DEFAULT 1;
ALTER TABLE core.subscriptions
    ADD COLUMN IF NOT EXISTS current_period_start TIMESTAMPTZ;

-- Stripe linkage. Plan state is written only by verified Stripe webhooks.
ALTER TABLE core.subscriptions
    ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT UNIQUE;
ALTER TABLE core.subscriptions
    ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT UNIQUE;
ALTER TABLE core.subscriptions
    ADD COLUMN IF NOT EXISTS cancel_at_period_end BOOLEAN NOT NULL DEFAULT false;
-- When a scheduled cancellation takes effect (cancel_at_period_end is true
-- whenever this is set).
ALTER TABLE core.subscriptions
    ADD COLUMN IF NOT EXISTS cancel_at TIMESTAMPTZ;

CREATE SCHEMA IF NOT EXISTS billing;

-- Webhook events already applied (Stripe redelivers on failure).
CREATE TABLE IF NOT EXISTS billing.stripe_events (
    event_id     TEXT PRIMARY KEY,
    type         TEXT        NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One savings-fee charge per account per ended billing period.
CREATE TABLE IF NOT EXISTS billing.savings_fees (
    user_id           UUID          NOT NULL REFERENCES core.users (id) ON DELETE CASCADE,
    period_start      TIMESTAMPTZ   NOT NULL,
    period_end        TIMESTAMPTZ   NOT NULL,
    savings_usd       NUMERIC(14,4) NOT NULL,
    fee_cents         INTEGER       NOT NULL CHECK (fee_cents >= 0),
    stripe_invoice_id TEXT,
    created_at        TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, period_start)
);

-- Payment state of each fee invoice, from invoice.* webhooks:
-- none (no fee), pending, paid, failed, void. payment_failed_at is the first
-- failure and is cleared once the invoice is paid.
ALTER TABLE billing.savings_fees
    ADD COLUMN IF NOT EXISTS payment_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE billing.savings_fees
    ADD COLUMN IF NOT EXISTS payment_failed_at TIMESTAMPTZ;
ALTER TABLE billing.savings_fees
    ADD COLUMN IF NOT EXISTS invoice_url TEXT;
CREATE INDEX IF NOT EXISTS idx_savings_fees_invoice ON billing.savings_fees (stripe_invoice_id);
UPDATE billing.savings_fees SET payment_status = 'none'
    WHERE fee_cents = 0 AND payment_status = 'pending';

-- True when a savings fee has stayed unpaid longer than the grace period.
CREATE OR REPLACE FUNCTION billing.fee_overdue(p_user_id UUID, p_grace_days INTEGER)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM billing.savings_fees
        WHERE user_id = p_user_id
          AND payment_status = 'failed'
          AND payment_failed_at < now() - make_interval(days => p_grace_days)
    );
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='subscriptions_plan_tier'
                   AND conrelid='core.subscriptions'::regclass) THEN
        ALTER TABLE core.subscriptions ADD CONSTRAINT subscriptions_plan_tier
            CHECK (plan IN ('free','pro','team'));
    END IF;
END $$;

-- ========================================================== metrics schema
CREATE SCHEMA IF NOT EXISTS metrics;

-- Authoritative account analytics. No monthly partition maintenance required.
CREATE TABLE IF NOT EXISTS metrics.proxy_runs (
    runtime_id UUID PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS metrics.proxy_events (
    event_id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES core.users(id) ON DELETE CASCADE,
    key_id UUID NOT NULL REFERENCES core.api_keys(id),
    runtime_id UUID NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    status INTEGER NOT NULL CHECK (status BETWEEN 100 AND 599),
    model TEXT NOT NULL,
    provider TEXT NOT NULL,
    project TEXT,
    agent TEXT,
    data JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proxy_events_user_time ON metrics.proxy_events(user_id,occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_proxy_events_user_run ON metrics.proxy_events(user_id,runtime_id);
CREATE INDEX IF NOT EXISTS idx_proxy_events_key ON metrics.proxy_events(key_id);

-- Append-only per-request ledger, partitioned monthly. Proxies batch-upload
-- rows every 30-60s; the dashboard reads rollups, never this table directly.
CREATE TABLE IF NOT EXISTS metrics.usage_events (
    id           BIGINT       GENERATED ALWAYS AS IDENTITY,
    user_id      UUID         NOT NULL,
    ts           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    session_id   TEXT,
    agent        TEXT,
    model        TEXT,
    requests     INTEGER      NOT NULL DEFAULT 1,
    tokens_in    BIGINT       NOT NULL DEFAULT 0,
    tokens_out   BIGINT       NOT NULL DEFAULT 0,
    tokens_saved BIGINT       NOT NULL DEFAULT 0,
    savings_usd  NUMERIC(12, 4) NOT NULL DEFAULT 0,
    cache_hit    BOOLEAN,
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);

-- Partitions: current + next two months (extend monthly; see README).
DO $$
DECLARE
    d DATE;
BEGIN
    FOREACH d IN ARRAY ARRAY[DATE '2026-09-01', DATE '2026-10-01', DATE '2026-11-01']
    LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS metrics.usage_events_%s PARTITION OF metrics.usage_events FOR VALUES FROM (%L) TO (%L)',
            to_char(d, 'YYYY_MM'), d, d + INTERVAL '1 month'
        );
    END LOOP;
END $$;

CREATE INDEX IF NOT EXISTS idx_usage_events_user_ts
    ON metrics.usage_events (user_id, ts DESC);

-- Daily rollups: what the dashboard actually reads.
CREATE TABLE IF NOT EXISTS metrics.usage_daily (
    user_id      UUID   NOT NULL REFERENCES core.users (id) ON DELETE CASCADE,
    day          DATE   NOT NULL,
    requests     BIGINT NOT NULL DEFAULT 0,
    tokens_in    BIGINT NOT NULL DEFAULT 0,
    tokens_out   BIGINT NOT NULL DEFAULT 0,
    tokens_saved BIGINT NOT NULL DEFAULT 0,
    savings_usd  NUMERIC(14, 4) NOT NULL DEFAULT 0,
    cache_hits   BIGINT NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, day)
);

-- Incremental rollup helper: ingest calls this per (user, day) touched.
CREATE OR REPLACE FUNCTION metrics.refresh_usage_daily(p_user_id UUID, p_day DATE)
RETURNS void LANGUAGE sql AS $$
    INSERT INTO metrics.usage_daily AS ud
        (user_id, day, requests, tokens_in, tokens_out, tokens_saved, savings_usd, cache_hits, updated_at)
    SELECT p_user_id, p_day,
           COALESCE(SUM(requests), 0),
           COALESCE(SUM(tokens_in), 0),
           COALESCE(SUM(tokens_out), 0),
           COALESCE(SUM(tokens_saved), 0),
           COALESCE(SUM(savings_usd), 0),
           COALESCE(SUM(CASE WHEN cache_hit THEN requests ELSE 0 END), 0),
           now()
    FROM metrics.usage_events
    WHERE user_id = p_user_id AND ts >= p_day AND ts < p_day + INTERVAL '1 day'
    ON CONFLICT (user_id, day) DO UPDATE
        SET requests     = EXCLUDED.requests,
            tokens_in    = EXCLUDED.tokens_in,
            tokens_out   = EXCLUDED.tokens_out,
            tokens_saved = EXCLUDED.tokens_saved,
            savings_usd  = EXCLUDED.savings_usd,
            cache_hits   = EXCLUDED.cache_hits,
            updated_at   = now();
$$;

-- =========================================================== contact form
-- Messages from the public Contact page. Only the sender's own details are
-- kept: no IP address or user agent (the rate limit keys on a salted hash in
-- memory). Admins read them on the dashboard's Messages page.
CREATE TABLE IF NOT EXISTS core.contact_messages (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT        NOT NULL,
    email       TEXT        NOT NULL,
    topic       TEXT        NOT NULL,
    message     TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    handled_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_contact_messages_created
    ON core.contact_messages (created_at DESC);

-- ====================================================== account email
-- NULL until the user opens the confirmation link (or completes a password
-- reset, which proves the same thing).
ALTER TABLE core.users ADD COLUMN IF NOT EXISTS email_verified_at TIMESTAMPTZ;

-- Single-use links for password reset and email confirmation. Only the
-- SHA-256 of each token is stored.
CREATE TABLE IF NOT EXISTS core.auth_tokens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID        NOT NULL REFERENCES core.users (id) ON DELETE CASCADE,
    purpose     TEXT        NOT NULL CHECK (purpose IN ('reset', 'verify')),
    token_hash  TEXT        NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_user ON core.auth_tokens (user_id, purpose);

-- ================================================== ContextShrink Code
-- Flat-fee plan for Claude Code and Codex users (code_plan.py). Only hashes of
-- device keys and CLI codes are stored; a device key is minted at poll time.
CREATE TABLE IF NOT EXISTS core.code_plans (
    user_id                UUID        PRIMARY KEY REFERENCES core.users (id) ON DELETE CASCADE,
    trial_started_at       TIMESTAMPTZ,
    trial_ends_at          TIMESTAMPTZ,
    stripe_subscription_id TEXT        UNIQUE,
    status                 TEXT,
    current_period_end     TIMESTAMPTZ,
    cancel_at_period_end   BOOLEAN     NOT NULL DEFAULT false,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A device is one computer. key_hash is NULL until a poll mints its key; a
-- revoked_at device no longer counts toward the device limit.
CREATE TABLE IF NOT EXISTS core.code_devices (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID        NOT NULL REFERENCES core.users (id) ON DELETE CASCADE,
    name         TEXT        NOT NULL,
    platform     TEXT        NOT NULL,
    device_hash  TEXT        NOT NULL,
    key_hash     TEXT        UNIQUE,
    tools        TEXT[]      NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_code_devices_user ON core.code_devices (user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_code_devices_active_hash
    ON core.code_devices (user_id, device_hash) WHERE revoked_at IS NULL;

-- Legacy one-time trial claim (one per device_hash). No longer written; read only by
-- the free-week migration at the end of this file.
CREATE TABLE IF NOT EXISTS core.code_trial_devices (
    device_hash TEXT        PRIMARY KEY,
    user_id     UUID        NOT NULL REFERENCES core.users (id) ON DELETE CASCADE,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Pending and finished `npx` connect requests. poll_secret and user_code are
-- stored only as SHA-256 hashes; consumed_at marks the one-time key delivery.
CREATE TABLE IF NOT EXISTS core.code_cli_requests (
    id               UUID        PRIMARY KEY,
    user_code_hash   TEXT        NOT NULL UNIQUE,
    poll_secret_hash TEXT        NOT NULL,
    tool             TEXT        NOT NULL CHECK (tool IN ('claude', 'codex')),
    device_hash      TEXT        NOT NULL,
    device_name      TEXT        NOT NULL,
    platform         TEXT        NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at       TIMESTAMPTZ NOT NULL,
    approved_user_id UUID        REFERENCES core.users (id) ON DELETE CASCADE,
    device_id        UUID        REFERENCES core.code_devices (id) ON DELETE SET NULL,
    consumed_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_code_cli_requests_expiry ON core.code_cli_requests (expires_at);
-- Set when an approval was refused by the device limit: the terminal's poll then
-- reports device_limit (with this account's devices) until a later approval wins.
ALTER TABLE core.code_cli_requests ADD COLUMN IF NOT EXISTS limit_user_id UUID REFERENCES core.users (id) ON DELETE CASCADE;

-- Hourly usage counts per device (counts only: no prompts, models or paths).
-- An upload replaces a bucket's values, so re-sending a bucket is harmless.
CREATE TABLE IF NOT EXISTS metrics.code_usage_hourly (
    user_id       UUID        NOT NULL REFERENCES core.users (id) ON DELETE CASCADE,
    device_id     UUID        NOT NULL REFERENCES core.code_devices (id) ON DELETE CASCADE,
    hour          TIMESTAMPTZ NOT NULL,
    tool          TEXT        NOT NULL CHECK (tool IN ('claude', 'codex')),
    requests      BIGINT      NOT NULL DEFAULT 0 CHECK (requests >= 0),
    tokens_before BIGINT      NOT NULL DEFAULT 0 CHECK (tokens_before >= 0),
    tokens_after  BIGINT      NOT NULL DEFAULT 0 CHECK (tokens_after >= 0),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, hour, tool)
);
CREATE INDEX IF NOT EXISTS idx_code_usage_user_hour ON metrics.code_usage_hourly (user_id, hour);

-- Free week: each account gets one 7-day week per UTC month (started at its first
-- use that month), and each device_hash gets one per month across all accounts.
-- month is the first day of the UTC month the week started in.
CREATE TABLE IF NOT EXISTS core.code_free_weeks (
    user_id     UUID        NOT NULL REFERENCES core.users (id) ON DELETE CASCADE,
    device_hash TEXT        NOT NULL,
    month       DATE        NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL,
    ends_at     TIMESTAMPTZ NOT NULL,
    UNIQUE (user_id, month),
    UNIQUE (device_hash, month)
);

-- Migration, runs on every boot and is idempotent: a one-time trial that started in
-- the current UTC month becomes that month's free week, so those accounts keep it.
INSERT INTO core.code_free_weeks (user_id, device_hash, month, started_at, ends_at)
SELECT p.user_id, t.device_hash,
       date_trunc('month', p.trial_started_at AT TIME ZONE 'UTC')::date,
       p.trial_started_at, p.trial_ends_at
FROM core.code_plans p
JOIN core.code_trial_devices t ON t.user_id = p.user_id
WHERE p.trial_started_at IS NOT NULL
  AND p.trial_ends_at IS NOT NULL
  AND date_trunc('month', p.trial_started_at AT TIME ZONE 'UTC')
      = date_trunc('month', now() AT TIME ZONE 'UTC')
ON CONFLICT DO NOTHING;
