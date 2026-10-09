# Account analytics on the Raspberry Pis

## Request and identity flow

1. Sign in on Pi 4, open **API Keys**, and create a key. PostgreSQL stores its
   SHA-256 hash, owner UUID and allowed scopes. The complete key is shown once.
2. Point the tool at `https://proxy.contextshrink.com` (append `/v1` when your
   client requires it), or use the desktop app, which does this for you. Set
   `X-Horizon-Proxy-Token` to the account key. Keep the provider key/login in its
   usual header. All connections, including SSH tunnels to Pi 5 loopback,
   require an account key. The proxy hostname is the Pi 5's own Cloudflare
   Tunnel to the gateway's loopback listener (127.0.0.1:8790).
3. The outer proxy middleware calls the control plane with a server-only
   service credential. It checks the key's current revocation state and scope
   on every HTTP request. The verified UUID follows async tasks and streaming
   responses through a ContextVar, including WebSocket connections. Caller
   identity headers are discarded. The account credential is removed before
   forwarding upstream.
4. The common RequestOutcome funnel writes metadata to a persistent SQLite
   outbox. Failed requests are recorded without claiming savings. Successful
   requests use the same novel-conversation savings and cache-aware pricing as
   Horizon. Every event has a stable UUID; PostgreSQL ignores retries with the
   same UUID. Outbox rows are removed only after a successful API response.
5. **Advanced Analytics** is a static page shipped with the dashboard build
   and served by the Pi 4 at `/dashboard`; it calls `api.contextshrink.com`.
   Summary, feed, grouping and CSV queries all resolve the browser session
   server-side, then filter by that UUID before aggregating or limiting rows.
   No browser-provided user ID selects an account. Responses use `no-store`.

## Data and access boundaries

- Pi 4 Usage and Profile read `/api/usage/summary` on all plans. The API filters
  the authenticated account before computing totals, model mix and recorded
  proxy activity spans. Dates are UTC; comparisons use the preceding equal
  number of calendar days. Missing dates render as zero, and fetch failures
  display an error rather than generated data. Data refreshes every 10 seconds
  while the page is visible. Recorded activity spans are per proxy run/agent,
  not a count of active client connections or conversation sessions.
- Profile identity and membership date come from `/auth/me`; providers come
  from account usage. The displayed timezone is the browser timezone, and
  the dashboard link uses the browser's actual origin.
- The three plans are `free`, `pro` and `team`. Free can be selected without a
  payment method. Pro starts at `POST /billing/checkout`: Stripe Checkout
  saves a card on a $0/month subscription (`STRIPE_PRO_PRICE_LOOKUP_KEY`).
  The plan is granted only by verified webhooks at
  `https://api.contextshrink.com/stripe/webhook`
  (`STRIPE_WEBHOOK_SECRET`), which sync plan, status and period from Stripe;
  a browser request cannot grant paid entitlement. Cancelling goes through the
  Customer Portal (`POST /billing/portal`, at period end). Team is "coming
  soon" until team membership and invitations exist.
- Savings fee (option A, no Stripe metering): on each renewal invoice
  (`invoice.created`, `subscription_cycle`) the API computes the ended period's
  savings from `metrics.proxy_events` and, above $20, finalizes a separate
  Stripe invoice for 5% of the whole amount. When a subscription ends, the
  final partial period is billed the same way. `billing.savings_fees` holds one
  row per account per period, so redelivered events never charge twice.
  `api/stripe_setup.py` creates the Pro price, Customer Portal settings and the
  webhook endpoint idempotently.
- Savings-fee retries resume the stored invoice instead of treating an invoice
  ID as proof of completion. The recorded fee amount stays fixed across retries.
  A PostgreSQL advisory lock serializes processing for each account/period;
  competing webhook deliveries return 503 so Stripe retries them. Invoices are
  created with automatic advancement disabled and finalized only after the fee
  line exists. Stripe invoice/item reads recover successful calls whose response
  or database write was lost, including after idempotency keys expire. Unexpected
  lines or an already-finalized empty invoice require reconciliation and return
  502 rather than creating an additional charge. No schema migration is needed.
- Unpaid fees: the $0 subscription renews regardless, so `invoice.payment_failed`
  / `invoice.paid` / `invoice.voided` on fee invoices are tracked per row in
  `billing.savings_fees` (first failure time kept across Stripe's retries).
  After `FEE_GRACE_DAYS` (7) unpaid, a background check in the API (every 10
  minutes) cancels the Stripe subscription, tagged `cancel_reason:
  unpaid_savings_fee` so no final-period fee is billed, and the account drops
  to Free. Until that check runs, `billing.fee_overdue` already treats the
  account as `past_due`. Checkout is refused while any fee is unpaid; once the
  invoice is paid the banner clears and the customer can upgrade again. The
  dashboard shows the failed payment with Pay invoice / Update card actions
  throughout.
- `/billing/estimate` calculates each account's savings from its own proxy
  events over its subscription period when one is recorded, or the current UTC
  calendar month before Stripe periods are available. Profile and Subscription
  use this same value. Pro's 5% savings fee is zero at or below $20 and 5% of
  the full savings above $20. Team adds $5 per account seat each month even
  when its savings fee is waived. These are estimates only; no charges are
  collected until Stripe billing is configured.
- Advanced Analytics is available only on active Pro/Team plans. The menu
  updates immediately after a plan change. Summary, feed and CSV endpoints
  enforce this entitlement server-side on every request; Free remains able
  to access its own basic Usage/Profile data. Direct advanced links redirect
  Free accounts to Subscriptions, including after a downgrade.
- Free compression is capped at `FREE_SAVINGS_CAP_USD` ($20 by default) of
  savings per UTC calendar month. The control plane returns
  `compression_allowed` from `/internal/proxy/authorize` on every request; once
  the cap is reached the proxy serves the account as full passthrough (the same
  path as `x-horizon-bypass: true`), tags the decision `plan_cap_reached` and
  adds `X-ContextShrink-Compression: paused` to responses. Requests keep
  working and are still recorded, with zero savings. Active Pro/Team plans are
  uncapped; a paid plan that is not active falls back to the cap. Because
  savings are known only after a response, a burst of concurrent requests can
  end slightly above the cap. An open WebSocket picks up the cap on its next
  turn; per-connection compression settings apply again on reconnect. The
  dashboard shows a notice from 75% of the cap and when compression is paused.

- `metrics.proxy_events` is the authoritative ledger, indexed by account/time,
  account/run and key. It needs no monthly partition job. Existing accounts,
  sessions, subscriptions and keys survive migration.
- Current proxy run selects the newest registered process run; Lifetime retains
  all account events; History selects the last 7–365 days. Daily dates are UTC.
  This deployment runs one proxy worker. If scaling to multiple workers, use a
  shared deployment run ID before treating this selector as a cluster session.
- The account page shows measured request/token/cache/latency information,
  estimated costs and model/provider/project/agent breakdowns. It never reads
  global internal metrics. Prompt/response bodies and arbitrary client tags are
  excluded from the account ledger. Full message logging is forced off in
  account deployment mode. The original operator UI stays on Pi 5 loopback.
- Response cache, compression cache, computed prefix sessions and conversation
  savings keys are namespaced by account UUID. Memory identity uses that UUID.
- API keys do not authorize browser analytics or service telemetry ingestion.
  The legacy `/ingest/usage` route is disabled. `/internal/*` requires the
  service credential and is not routed publicly at all.
- The Pi 5 gateway is the only public entry (its own Cloudflare Tunnel; every
  service listens on loopback). `api.contextshrink.com` exposes only the public
  API routes; `proxy.contextshrink.com` exposes only supported Messages, Chat
  Completions, Responses and Gemini inference/model operations. Both return 404
  for `/internal/*`, `/docs`, raw `/stats`, global transformation feeds,
  admin/settings and provider response retrieval endpoints. Other provider
  surfaces must receive an explicit identity/isolation review before exposure.
- Cache keep-alive (proxy `HORIZON_CACHE_KEEPALIVE=1`, off by default): only
  sessions that send `X-Horizon-Keepalive-Id` (`wrap claude` and the desktop
  app do) on an account whose plan still allows savings are kept warm. The
  session group is that id hashed with the verified account UUID. Each ping is
  its own `metrics.proxy_events` row (`kind: "keepalive"`, its cost as
  `cost_usd` and as negative `savings_usd`), so pings for sessions nobody
  resumes still count against savings. A request that resumes a kept-warm
  session adds the avoided rewrite to its `savings_usd` (`keepalive_usd`).
  Request counts and latency averages exclude ping rows. When the tool exits,
  `wrap` posts `/v1/horizon/keepalive/end` through the forwarder; the gateway
  allows that one path, the account middleware requires the account key, and
  the proxy ends only that account's session with that id. Ping rows need an
  API that knows `kind`; deploy the API before turning keep-alive on.
- Flash Observations and the price policies (`HORIZON_SAVINGS`) lower a
  request's price without removing tokens before it is counted, so the proxy
  prices them per request (`horizon/proxy/policy_savings.py`), adds them to
  `savings_usd` and lists them in `policy_usd` (`flash`, `fast_mode`, `flex`,
  `modernize`). They therefore count toward Est. savings, the Pro fee and the
  Free cap. Flash credits only outputs a request no longer carries, priced as
  the cache reads they replace, and nothing when usage has no cache breakdown;
  fast mode credits the premium on models that bill it; Flex credits standard
  minus Flex price only when no 429 fallback happened, and the row's
  `cost_usd` is the Flex price; modernization compares the requested model's
  price on its own tokenizer. Prices come from the catalog only, never a
  fallback rate. The price-cliff guard's extra compression is already in the
  compression figure. Rows with `policy_usd` need an API that knows it; deploy
  the API first.
- Chat Completions, Claude Messages, Gemini `generateContent` and Bedrock
  `InvokeModel` requests carry the whole transcript,
  and every earlier removal is sent compressed again (replayed in cache mode,
  recompressed in token mode), so a request's `tokens_saved` is the
  conversation's running total. The proxy keys it by the account, the system
  prompt and the first user message, ignoring `cache_control`
  (`transcript_savings_key`): each removal is booked once, and its repeats as
  retained savings priced as cache reads (as for Codex). Conversations in one
  account that share their opening share a running total.
- Revocation immediately blocks new requests/connections and the next incoming
  WebSocket turn (revalidated before forwarding). An inference operation already
  sent upstream can finish.

## Deployment

Set the same strong `CONTEXTSHRINK_SERVICE_TOKEN` in the API and proxy environment;
set `CONTEXTSHRINK_API_URL=http://127.0.0.1:8788` for the host-network proxy.
Keep secrets out of Git. Build API and proxy images, deploy the API first, then
the proxy, and validate/reload both Caddy configurations. Deploy the rebuilt Pi 4
React assets alongside its Caddyfile. The proxy workspace volume holds the
outbox; do not remove this volume to reset analytics.

The initial migration clears the user-authorized mixed test savings/history and
legacy usage tables only. New account events survive restarts and delivery
outages. Account lookup fails closed with 503 if authentication is unavailable;
an ingestion outage retains already-authenticated usage in the outbox and
retries with exponential backoff. Monitor delivery warnings and disk usage.

These addresses are the existing trusted LAN deployment. Before internet
exposure, use HTTPS on the web and proxy ingress so session/API keys are encrypted
in transit, and restrict the control-plane service port to the required hosts.
