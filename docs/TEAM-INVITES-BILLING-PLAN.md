# Team invitations, savings attribution and monthly billing

Status: implemented locally behind `CONTEXTSHRINK_TEAMS_ENABLED=false`. PostgreSQL migration execution and Stripe sandbox validation are required before production rollout. See [TEAM-ROLLOUT.md](TEAM-ROLLOUT.md).

## Baseline before this implementation

- `api/schema.sql` stores subscriptions against individual users. It has a seat count, defaulting to one, but no team, membership or invitation records.
- `api/analytics.py` authorizes keys for their individual owner and ingests request events identified by user and key. Event IDs already prevent duplicate ingestion.
- `api/billing.py` calculates the existing savings fee from the proxy ledger. Its Stripe checkout and price recognition currently support Pro only; the savings-fee invoice is separate from the subscription invoice.
- `api/main.py` estimates Team charges as seat count times $5 plus the user's savings fee. This is not yet an organization-wide calculation.
- The landing page advertises a minimum of three Team seats, while the dashboard describes combined Team analytics as coming soon.

## Recommended product and pricing rules

1. A team has one owner, one billing account and one Stripe subscription. The owner occupies one seat. Start with one active team membership per person; retain a separate personal workspace.
2. Purchased seats have a minimum quantity of three. The initial seat charge is therefore at least $15/month, even if fewer than three seats are occupied.
3. Invitations reserve available purchased seats. Sending an invitation does not increase the purchased quantity or start an additional charge. Purchasing more seats requires an explicit owner action with the price shown.
4. Apply the current 5% savings fee to the team's combined eligible savings. Preserve the existing waiver: the savings fee is zero when combined cycle savings are $20 or less; above $20, charge 5% of the full amount. This threshold applies once to the team, not once per member.
5. Keep the existing ledger eligibility rules. Graph estimates excluded from monetary savings today remain excluded for Teams. Aggregate eligible signed savings before applying the current rounding and fee rules.
6. Members are not individually invoiced for Team usage. Their personal usage, if any, remains separate and is never counted again in the Team total.

For a full month with three purchased seats:

| Member | Eligible savings |
| --- | ---: |
| Owner | $100 |
| Member A | $200 |
| Member B | $300 |
| **Team total** | **$600** |

The monthly components are $15 in seats and $30 in savings fees: **$45 before any applicable tax or adjustments**. An unused purchased seat still costs $5 and contributes $0 in savings.

## Invitations and permissions

- Add a Team page showing purchased, occupied, available and reserved seats, plus member and invitation lists.
- Owners manage billing, seat purchases, roles and ownership transfer. Admins can invite and remove ordinary members within purchased capacity. Members see their own Team usage; owners and admins see the member breakdown and team totals.
- Reuse the existing email sender. Invitations use a random, single-use link; store only its hash. Recommend a seven-day expiry, resend/revoke actions and rate limits.
- Acceptance requires sign-in and a verified email matching the invited address. Recheck invitation state, membership and seat availability inside a database transaction. Concurrent accepts or retries must not overbook seats.
- Prevent removing the final owner. Require ownership transfer before the owner leaves.
- Removal revokes that member's Team keys and invalidates proxy authorization caches. Preserve historical membership, usage and billing attribution. The released seat can be reused; removing a member does not automatically lower the purchased quantity.

## Savings attribution and storage

Add these records without changing existing personal subscription identities:

- `core.teams`: team identity and lifecycle state.
- `core.team_memberships`: stable membership ID, user, team, role and join/leave history.
- `core.team_invitations`: invited email, role, token hash, expiry and acceptance/revocation state.
- `core.team_subscriptions`: team Stripe customer/subscription, purchased quantity, status and billing period. Enforce quantity >= 3 in the database and API.
- Team and membership references on API keys and proxy events. Existing rows remain personal; do not relabel historical personal usage as Team usage.
- Team billing-cycle snapshots with the source period, per-member savings, combined savings, fee, Stripe invoice and reconciliation state. Uniquely identify each team and billing period.

Each member receives their own Team-scoped keys. A person can have multiple keys or devices; their requests all roll up to the same membership. The authenticated key determines billing scope on the server. Client-supplied team or member IDs cannot select who pays.

Persist attribution with the event and keep key billing ownership immutable. Removing a member, changing an owner or revoking a key must not move already recorded savings to another account. Deduplicate by the existing event ID before aggregating.

Shared automation keys, if supported, appear as a separate **Service usage** row included in the team total. A shared key cannot reliably identify the human making each request, so it must not be presented as a per-seat measurement.

Add a workspace choice to dashboard key creation and connector login/setup. Claude and Codex connectors must explicitly connect to Personal or the selected Team and remember the selected key. Keep existing keys personal. Offer existing Pro members a clear personal-subscription cancellation option during onboarding; do not silently cancel, transfer or duplicate their usage.

## Monthly invoice and seat changes

Create a recurring Stripe Team seat price of $5/month with quantity >= 3. Continue using Checkout and verified webhooks to activate a new paid subscription. Reuse the existing savings calculation rather than introduce a second metering definition.

- Initial checkout charges the purchased seats upfront. There is no savings fee before Team usage has occurred.
- At renewal, add the completed cycle's savings fee to the same draft invoice as the next month's recurring seat charge. Clearly label both periods. This requires a Team-specific draft-invoice flow; the existing separate Pro fee invoice cannot be used unchanged.
- Seat additions are prorated, with the adjustment included on the next monthly invoice. Show the owner the extra cost before increasing quantity. Seat reductions take effect at the next renewal, never below three, and must leave enough capacity for remaining members and reservations.
- Use the Stripe billing period, UTC and half-open ledger windows `[start, end)`. Allow for the current proxy ingestion delay before finalizing the savings snapshot.
- Freeze the calculation and persist the invoice ID before retrying work. Enforce both a database uniqueness constraint and Stripe idempotency keys per team/cycle. Repeated or out-of-order webhooks cannot create duplicate charges.
- Reconcile late events explicitly. Recompute the original cycle's fee and apply only the difference from the previously invoiced fee as an identified adjustment; do not apply the waiver again to late events in isolation or silently rewrite a paid invoice.
- Only verified Stripe subscription/payment state grants paid access. Reuse the current payment-failure/grace policy for the team billing account, with owner-visible recovery. Keep Customer Portal focused on payment methods, invoices and cancellation; route seat changes through the capacity-aware Team UI.
- Default cancellation is at period end. Seats are already paid for that period; settle any remaining savings fee once, without another seat charge.

## Dashboard behavior

- Team page: invite/resend/revoke, roles, removals and seat capacity.
- Team Usage: current-cycle member savings, request/token totals and the combined savings ledger. Use the same ledger definition for estimates and invoices.
- Subscriptions in Team context: purchased seats times $5, combined savings, one 5% savings fee and total estimate. Explain upfront seats versus completed-cycle savings so estimates do not imply that the initial seat payment is due twice.
- API Keys: clearly label personal and Team scope; keep revoked keys in Archived.
- Members can inspect their own contribution. Owner/admin views include former members' contribution to the selected cycle. Any service usage or late adjustment is visible and reconciles to the total.
- Treat optional per-member fee shares as informational allocations of the single team fee. Allocate rounding differences deterministically so displayed shares sum exactly to that fee.

## Implementation order and acceptance gates

1. **Additive data model and invitations:** migrations, permissions, email flow, capacity transactions and team-scoped key creation. Keep existing personal flows working.
2. **Proxy attribution:** update API authorization, gateway and ingestion together, with compatibility for existing personal clients. Validate removal and revoked-key behavior, cross-team isolation and duplicate event handling.
3. **Analytics and dashboard:** per-member and team rollups, workspace selection, Team page, current-tier billing estimate and connector support.
4. **Stripe sandbox integration:** Team seat price, checkout, subscription synchronization, prorations, draft invoice fee attachment and cycle snapshots. Preserve personal Pro billing behavior.
5. **End-to-end validation:** three seats minimum, owner plus two invitees, expired/reused/wrong-email invitations, concurrent accepts, multiple keys per member, member removal/rejoin, owner transfer, service keys, partial periods, duplicate webhooks, failed payments and late usage.
6. **Reconciliation and rollout:** verify member totals equal the team ledger and invoice; pilot behind a feature flag in a Stripe sandbox, then prepare a reviewed production release covering API/database, proxy/gateway, dashboard and connectors.

Required billing checks include: exactly $20 combined savings has no savings fee; three members saving $10 each produce one $1.50 savings fee; the $600 example produces $30; minimum seat charges remain $15; personal and Team events never appear in both invoices; a repeated cycle close never creates a second fee.

The recommended decisions above are sufficient to begin implementation after approval. They preserve the existing savings-fee waiver, charge for purchased rather than only occupied seats, and keep the team owner responsible for one consolidated monthly invoice.
