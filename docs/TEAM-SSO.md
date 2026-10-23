# Team single sign-on

Optional OpenID Connect (OIDC) sign-in is implemented for the dashboard. It uses
existing Team memberships and does not create accounts or consume extra seats.
Password sign-in remains available for recovery. SAML, mandatory SSO, SCIM and
browser sign-in for desktop/npx clients are outside this implementation.

## Operator setup

1. Back up PostgreSQL. Build the API with its updated requirements. On startup,
   `api/schema.sql` adds SSO tables and nullable session columns; personal sessions
   retain their existing behavior. Validate this migration on staging first.
2. Set these API environment variables (also wired through
   `docker-compose.controlplane.yml`):
   - `CONTEXTSHRINK_TEAMS_ENABLED=true`
   - `CONTEXTSHRINK_TEAM_SSO_ENABLED=true`
   - `SSO_API_URL=https://api.contextshrink.com`
   - `APP_URL=https://app.contextshrink.com`
   - `SSO_ALLOWED_HOSTS`: exact comma-separated trusted IdP hostnames for issuer
     discovery, authorization, token exchange and JWKS. No wildcards or private
     addresses. Only approve identity-provider hosts controlled by a trusted party.
   - `SSO_ENCRYPTION_KEY`: a Fernet key generated privately using
     `cryptography.fernet.Fernet.generate_key()`. Keep it in the server secret
     store with a secure backup. It encrypts client secrets and transient PKCE
     verifiers. Do not put it in source control or send it through chat.
3. Deploy the API, the updated Pi 5 Caddy public-route allowlist and the dashboard.
   Register the callback **`https://api.contextshrink.com/auth/sso/callback`** at
   the IdP. The dashboard's `/sso/callback` is the final browser handoff, not the
   callback registered at the IdP.
4. The IdP must support authorization code, PKCE S256, `client_secret_post`,
   RS256 or ES256 ID tokens, and return `email` plus boolean
   `email_verified=true` with `openid email` scopes. Configure a specific tenant
   issuer; do not use a shared multitenant issuer. Provider compatibility must be
   confirmed with a real test tenant before enabling customer access.

## Owner and member setup

1. The owner signs in with a password and a verified email, then opens **Team →
   Single sign-on** and saves the exact issuer, client ID and client secret.
   The secret is never returned to the dashboard. Discovery checks the provider
   endpoints; it does not prove the client credentials work until a sign-in test.
2. Each existing member signs in with their password, verifies their account
   email and clicks **Connect company identity** on the Team page. The company
   identity's verified email must match that member's account. Invitation and
   purchased-seat rules still apply; an email-domain match never grants access.
3. After linking, sign out and choose **Sign in with SSO** on the login page.
   Enter the Team ID shown on the Team page, then authenticate with the provider.

SSO sessions last eight hours and stay in that Team workspace. They cannot read
personal billing/usage, switch workspaces, access the operator inbox or manage
SSO identities/settings. Password sessions retain their normal access.

Changing the configuration invalidates old links, pending handoffs and existing
SSO sessions; members must relink. Removing the configuration or a member blocks
their SSO session immediately on its next request. A removed member who rejoins
must link again. Password sign-in is required to unlink an identity; unlinking
also revokes that member's SSO sessions. IdP account deactivation alone is checked
on the next SSO sign-in; there is no continuous IdP session or SCIM synchronization.

Keep `SSO_ENCRYPTION_KEY` stable. To rotate it without a ciphertext migration,
disable SSO, replace the key and have owners save new provider configurations;
old links and sessions will be invalidated when the configurations are saved.

## Verification before production

Mocked tests cover signed JWT validation, browser-verifier binding, single-use
state/code handling, active membership, encrypted secrets and Team session scope.
A real PostgreSQL migration and live IdP login/link/remove test remain required.
Test both an owner and a member, failed/replayed callbacks, member removal and
configuration rotation. Confirm password login still works after disabling SSO.
