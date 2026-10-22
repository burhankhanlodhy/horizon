# ContextShrink Control Plane (API + Postgres)

Backend for the dashboard: user accounts, sessions, API keys, and the usage
metrics pipeline. Runs next to the horizon proxy as its own stack.

## Stack

- **Postgres 16** — two schemas:
  - `core` — users, sessions, api_keys, subscriptions
  - `metrics` — `usage_events` (append-only, monthly partitions) + `usage_daily` rollups
- **FastAPI** (`api/`) — argon2 password hashing, opaque bearer tokens stored
  as SHA-256 hashes, API-key-authenticated batch ingest

## Endpoints

| Method | Path             | Auth       | Purpose                          |
| ------ | ---------------- | ---------- | -------------------------------- |
| GET    | `/healthz`       | —          | liveness                         |
| POST   | `/auth/signup`   | —          | create account, returns token    |
| POST   | `/auth/login`    | —          | verify credentials, returns token|
| POST   | `/auth/logout`   | bearer     | revoke the session               |
| GET    | `/auth/me`       | bearer     | current user                     |
| POST   | `/ingest/usage`  | `X-API-Key`| batched usage rows from proxies  |
| GET    | `/usage/summary` | bearer     | totals + daily series (N days)   |
| GET    | `/usage/key-summary` | bearer API key | today's UTC totals for that key only (npx savings bar) |

## Deploy on the Pi

```bash
# on the Pi, in ~/horizon (after git pull)
cp controlplane.env.example .env        # then set POSTGRES_PASSWORD
docker compose -f docker-compose.controlplane.yml up -d --build
curl http://127.0.0.1:8788/healthz
```

Postgres binds `127.0.0.1:5432`, API binds `127.0.0.1:8788` — loopback only,
reached via SSH tunnel or the future reverse proxy. The horizon-proxy stack
(`docker-compose.yml`) is untouched and can run alongside.

## Database

- Schema lives in `api/schema.sql`; applied automatically on first Postgres
  init and re-asserted by the API on every start (idempotent).
- Partition maintenance: `usage_events` partitions are pre-created for the
  current month +2. Add a monthly cron:
  `CREATE TABLE IF NOT EXISTS metrics.usage_events_YYYY_MM PARTITION OF
  metrics.usage_events FOR VALUES FROM (...) TO (...);`
- Dashboard data flows: proxy batches → `POST /ingest/usage` → `usage_events`
  + auto-refreshed `usage_daily` → `GET /usage/summary`.

## Migrating to managed Postgres

Change one value: `DATABASE_URL` (or `POSTGRES_*` + host) in the environment,
restore a dump into the managed instance, done — no code changes. The app
never assumes local Postgres.

## Local dev (on the PC with Docker Desktop)

```powershell
cp controlplane.env.example .env   # set POSTGRES_PASSWORD
docker compose -f docker-compose.controlplane.yml up -d --build
```

The dashboard dev server (5173) talks to it via `VITE_API_URL` (default
`http://127.0.0.1:8788`).
