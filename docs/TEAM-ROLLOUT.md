# Team implementation and rollout

Implemented locally; not deployed or published. The API flag `CONTEXTSHRINK_TEAMS_ENABLED` defaults to `false`. The dashboard hides Team onboarding when the API reports Teams unavailable.

## Implemented behavior

- One Team owner and one Team subscription; owner, admin and member roles. A user can have one active Team membership alongside their personal workspace.
- Owner buys at least three $5 monthly seats, including their own. Invitations reserve purchased capacity, expire after seven days, are single use, and require a matching verified email. Resending rotates the token. Seat reductions also limit new reservations immediately.
- A member's Team keys are separate from their personal keys. Removing a member revokes their Team keys and preserves historical contributions. Rejoining creates a new membership.
- Usage and savings follow immutable key/membership attribution verified by the API. Proxy requests and incoming WebSocket turns reauthorize. Personal analytics and billing exclude Team events.
- Keep-alive caches use the same workspace boundary and reauthorize before background pings. Service-authenticated authorization timestamps allow an in-flight request to finish after removal while retaining the key and membership revocation checks; legacy events keep completion-time checks.
- Owner/admin see Team totals and member contributions. Members see their own contribution. Team key lists identify the key's owner. Ownership transfer requires the owner.
- The fee is 5% of combined eligible savings only when the combined amount exceeds $20. Signed negative savings remain in the total. Graph dollar estimates are excluded. Three seats and $600 combined savings produce $15 seats + $30 savings fee = $45 before tax.
- Seats are charged upfront. The completed cycle's savings fee is attached to the renewal draft invoice. Cycle and member snapshots are transactional; fee attachment uses Stripe idempotency and metadata. Late events recompute the original cycle's fee and carry the difference forward. Cancellation settles the final cycle without buying another month of seats.
- Seat increases require an owner action and a preview, with proration on the next invoice. Decreases are scheduled for renewal and cannot fall below three or occupied/reserved capacity. The Team billing portal cannot bypass seat controls.
- Personal subscriptions and historical keys are not silently migrated or cancelled.

## Connector selection

Copy the Team ID from the Team page after joining and activating the subscription:

```powershell
npx contextshrink-codex connect --team <TEAM-ID>
npx contextshrink-claude connect --team <TEAM-ID>
```

For an already configured connector, `login --team <TEAM-ID>` explicitly replaces the connection. Replacing or logging out revokes the old key in its saved workspace. Personal remains the default when signing in without `--team`. Pasting a dashboard key uses that key's existing workspace.

The native desktop app has an optional Team ID on its sign-in form. The Python CLI supports `horizon login --team <TEAM-ID>`; use `--force` when replacing an existing connection. Selected workspaces survive restart. These connector changes require new releases; the published npm packages have not been updated by this implementation.

## Validation completed locally

- API, proxy and Python CLI regression suites, including workspace isolation, authoritative ingestion, historical membership checks, per-turn WebSocket checks, invitation races, reservations, roles and billing replay cases.
- Both npx connector suites, including scoped creation/logout and invalid Team IDs. Two pre-existing optional Codex graph/native-engine tests remain skipped.
- Dashboard production build, desktop TypeScript build, and native `cargo check`.
- Python Ruff checks and Git whitespace checks.

Final regression result: 257 Python tests passed, 68 Claude connector tests passed, and 63 Codex connector tests passed (two existing optional Codex tests skipped).

No local PostgreSQL server or Docker runtime was available. The SQL migration has not been executed against a real database. Stripe calls, SMTP delivery and account data were replaced by fakes in the tests; no real checkout, card payment, invitation or production deployment occurred.

## Rollout gates

1. Back up the database and apply `api/schema.sql` on a PostgreSQL staging copy, both from a clean database and an existing schema. Verify the composite attribution constraints, immutable history triggers and concurrent membership/seat operations.
2. Build and deploy the API image containing `teams.py` and `team_billing.py`, plus the proxy and Pi gateway route changes. Keep the Team flag off until staging checks pass. Set the flag on the staging API only.
3. Use existing protected Stripe sandbox credentials to run `python stripe_setup.py` in the API runtime. This creates/reuses the $5 monthly Team price and a separate Team portal configuration. Configure the signed webhook for all events in `stripe_setup.py`. `STRIPE_TEAM_PRICE_LOOKUP_KEY` and `STRIPE_TEAM_PORTAL_CONFIGURATION_ID` are optional overrides. Do not use production Stripe credentials for validation.
4. Exercise owner checkout, real email invitation/acceptance, both connectors, Team usage, removal, ownership transfer, seat preview/increase/decrease, and personal workspace isolation. Use Stripe test clocks to test renewal, payment failure/recovery and cancellation. Confirm there is exactly one invoice fee per cycle and that late adjustments use the original combined threshold.
5. Review invoice currency, billing contact and tax handling. Amounts above exclude tax; assess Stripe Tax/required registrations before enabling collection. Store Stripe credentials in protected server configuration, never source or frontend code.
6. Enable Teams for the intended release after staging approval, deploy the dashboard, and release the connectors. Monitor Team cycles against Stripe invoices and retain the existing personal billing path.

Review any legacy individual subscriptions marked `team` before migration; they are not automatically converted to the new Team workspace model. If the duplicate-subscription guard logs a cancellation, reconcile the associated Stripe payment and any refund separately.

Disabling the flag blocks new Team use and hides onboarding. It does not cancel existing Stripe subscriptions or waive invoices. Continue processing signed billing webhooks and reconcile any existing Team subscriptions during rollback.
