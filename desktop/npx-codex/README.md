# contextshrink-codex

Connect local Codex CLI and desktop coding chats to ContextShrink while keeping
your existing ChatGPT sign-in. The connector configures a custom model provider;
it does not patch the desktop executable. Graph integration is **off by default**
and is not enabled by connecting.

## Install and connect

Requires Node.js 18+, a ContextShrink account, and a Codex build supporting custom
model providers. Existing-chat migration additionally requires Node.js 22.13+.
Close Codex Desktop and active Codex CLI sessions before changing the connection.
In PowerShell or your terminal, run:

```powershell
npx contextshrink-codex@latest
npx contextshrink-codex@latest status
```

Sign in with your **ContextShrink** credentials when prompted. Keep your existing
ChatGPT sign-in in Codex; no OpenAI API key is requested. Restart Codex, start a
new local chat and make a request. Check the ContextShrink dashboard for the
device key named `<computer> (npx Codex)`.

```powershell
npx contextshrink-codex@latest savings
npx contextshrink-codex@latest savings-window
```

The second command opens the optional live Windows savings companion. Savings
are server-recorded totals for this API key today (UTC), not per-chat figures or
a reduction to a fixed subscription fee. Server support is required for the
savings endpoint and native Codex model catalog.

To restore the previous connection settings, close Codex and run:

```powershell
npx contextshrink-codex@latest disconnect
```

Restart Codex afterwards. Disconnect leaves the device API key active. `logout`
disconnects and asks for ContextShrink sign-in again to revoke that key. `login`
rotates the connector's key; `login --key` accepts an externally managed key,
which is not revoked by logout. Upgrading does not require another sign-in.

## Version 0.2.5

- Savings below a cent show their real size (for example "$0.0015") instead of
  "$0.00", in the savings command and the Windows companion window.

## Version 0.2.4

- New-chat connection, opt-in existing-chat migration with restoration, and a
  live Windows savings companion, including its visibility fix.
- Native Codex model-catalog routing and HTTP/WebSocket Responses support.
- Experimental graph tools are not included in the npm package. Whole-chat
  benchmarks did **not** establish graph savings; they remain in the repository
  for experiments (`desktop/npx-codex/GRAPH.md`).
- Updated proxy/API code treats graph receipts as retrieval diagnostics with
  zero dollar credit. These server-side accounting protections require a
  separate server deployment; publishing this package does not deploy them.

New desktop chats were user-tested with requests appearing on the dashboard.
Existing-chat migration and the companion were also exercised on the user's
setup; migration remains experimental because it edits Codex's internal local
state. Use a single-chat preview before applying broader migrations.

## Existing local chats

These commands require **Node.js 22.13 or newer** for its built-in SQLite support.
Check with `node --version`. The other connector commands still support Node 18+.
Keep using your already connected account; an upgrade does not need a new key.

Close Codex/ChatGPT Desktop and all Codex CLI sessions. List the local chat IDs,
then preview a migration (replace `CHAT_ID` with the ID of one old chat):

```powershell
npx contextshrink-codex@latest chats list
npx contextshrink-codex@latest chats migrate CHAT_ID
npx contextshrink-codex@latest chats migrate CHAT_ID --apply
```

Reopen Codex, continue **that same existing chat**, and verify its next request
on the dashboard. Its history, ID, title, project, and pin should remain intact.
After validating one chat, preview/apply all eligible active chats with:

```powershell
npx contextshrink-codex@latest chats migrate --all
npx contextshrink-codex@latest chats migrate --all --apply
```

Only `openai` chats are eligible. Bulk migration skips archived chats by default;
add `--include-archived` to include them, or select an archived ID explicitly.
Other custom providers are not changed. Commands default to a dry run;
`--apply` refuses while a Codex/ChatGPT process is running.

This is an **experimental offline migration of Codex's internal local state**,
not a public desktop provider-switch API. It supports the inspected
`state_5.sqlite` schema, updating only `threads.model_provider` and the provider
in each rollout's first `session_meta` record. Conversation bodies are streamed
unchanged, file timestamps are preserved, and unrelated database fields are
not restored or rewritten. The actual bundled engine used for the regression
test is `codex-cli 0.163.0-alpha.10.2`; different builds still need validation.

A private `.contextshrink-npx-codex-chats.json` journal in `CODEX_HOME` stores the
original and managed metadata headers **before** changes. SQLite transactions
and the journal support recovery between file and database writes. The journal
contains original session metadata/instructions; do not share it. Disconnect
automatically restores these migrated provider references before removing the
custom provider. Turns added since migration are preserved. Use version 0.1.2
or newer to disconnect; migration upgrades the connector state so 0.1.0 refuses
to remove routing without restoring chats.

To undo migration while keeping ContextShrink connected, close Codex and run:

```powershell
npx contextshrink-codex@latest chats restore --all
npx contextshrink-codex@latest chats restore --all --apply
```

Restore also accepts individual IDs. If interrupted, keep Codex closed and retry
`chats restore --all --apply`. Unexpected edits to the metadata, removed chats,
or paths outside the local session folders cause a refusal and keep the journal
for review. Do not delete the restoration journal to work around a refusal.
Chats created originally under ContextShrink have no prior provider in this
journal; these commands restore only the older chats explicitly migrated.

## Live savings companion (Windows)

With the connector already connected, open another PowerShell window in the
folder containing the package and run:

```powershell
npx contextshrink-codex@latest savings-window
```

A small movable window opens at the bottom right. It shows the trimmed percent,
estimated net token-cost savings (or cost), request count, key/day scope, and last
update time. It refreshes every 30 seconds, has a **Keep on top** checkbox, and
shows **Savings unavailable** on errors or stale data instead of old totals.
Leave that PowerShell command running; close the companion or press Ctrl+C to
stop it. Closing its Node owner also closes the window. Disconnecting leaves it
open showing unavailable until it is closed or the connection returns.

This is the companion display selected for this connector, **not an injected Codex
footer**. It does not patch the desktop app or install an auto-start task. Node
reads the managed connection and fetches the same key-scoped savings endpoint;
only numeric totals and a timestamp go into the window's temporary snapshot.
No keys, ChatGPT credentials, conversation content, or raw service errors are
passed to PowerShell or command-line arguments. Key rotation is picked up on
the next refresh. API requests have a two-second deadline.

The launcher uses Windows PowerShell and WinForms with a process-only
`RemoteSigned` policy. It does not change saved user/system execution policies
or override enterprise Group Policy; a policy that disallows the script produces
an error. No OpenAI model requests are made by the companion itself.

## What changes

- The root `model_provider` is selected as `contextshrink_npx`.
- A marked `[model_providers.contextshrink_npx]` block points at
  `https://proxy.contextshrink.com/k/<computer-id>/v1`.
- `requires_openai_auth = true` preserves Codex's ChatGPT authentication.
- `X-Horizon-Proxy-Token` carries the separate ContextShrink account key; the
  proxy validates and strips it before forwarding the original OpenAI credentials.
- `X-Horizon-Model-Catalog = codex` requests a native Codex model catalog, instead
  of an OpenAI API `data[]` list. Both HTTP and WebSocket Responses are enabled.
- `.contextshrink-npx-codex.json` beside the config records the original bytes and
  the exact provider block. `~/.contextshrink/npx-codex.json` remembers the device
  key id and keep-alive id, never the raw key.

The ContextShrink key is in plain text in the provider block and restoration
state. Files are written owner-only where supported; Windows uses inherited
profile ACLs. Do not share those files. Revoking the key on the dashboard stops
access. OpenAI credentials stay in Codex's normal credential storage. The proxy
can see the requests and upstream credentials needed to forward them, just as
the existing ContextShrink Codex integration does.

An unchanged config is restored byte for byte. Unrelated edits and comments are
preserved on disconnect. Edits to the managed provider cause a refusal, keeping
the config and state for review. Another provider, an existing routing profile,
or an unsupported TOML layout is refused before sign-in. There is no `--force`.
An active connector lock prevents simultaneous writers; after an interrupted
operation, check that no connector is running before removing its `.lock` file.

`CODEX_HOME` selects a different config directory. For a desktop app launched
normally, this must be the same directory that the app uses. Remote computers
and WSL environments have their own configs and must be connected separately.

## Scope and limitations

- This targets local Codex model-provider requests. Ordinary ChatGPT chats,
  cloud tasks, sign-in, MCP/plugin traffic, voice, and all other desktop network
  activity are outside this connector's routing scope.
- The savings display is a separate Windows companion, not a native desktop
  footer. `status` confirms configuration only, not a completed model call.
- Savings are server-recorded totals for **this API key today (UTC)**, including
  keep-alive costs. They do not measure a reduction to a fixed subscription fee.
- API-key-authenticated Codex is not supported by this connector. Existing third-party
  routing is kept. ChatGPT plan limits and workspace-managed policies still apply.
- Existing chats retain their provider identity until explicitly migrated with
  `chats migrate`. No chats are migrated automatically by connect. Use the new
  package to restore migrated chats or disconnect; always restart the desktop app.
- Model catalogs are passed through from the caller's ChatGPT identity. The
  native catalog response returns 503 on registry failure rather than inventing
  model availability. The client may use its own bundled catalog as a fallback.
- New-chat routing and savings have passed the user's live desktop test.
  Existing-chat migration and the companion still need the user's desktop test.
  Automated tests do not establish compatibility with every build or entitlement.

## Deployment and release

The proxy must include the native-catalog compatibility changes in
`horizon/providers/codex/model_metadata.py` and `horizon/proxy/account_analytics.py`
before a first connection. The catalog uses the `proxy:responses` scope;
the connector's key does not need permission for Anthropic Messages requests.
The existing gateway allowlist already permits the authenticated `/k/.../v1/models`
and Responses paths; no new public route or API schema is needed. The
`/usage/key-summary` API deployed for the Claude npm package is reused unchanged.
Creating a package does not deploy those changes. New-chat routing has now been
observed working on the user's connected proxy; this local 0.1.2 update adds no
new backend endpoint or deployment requirement.

After live validation, publish this separate npm package as `contextshrink-codex`.
The published Claude package and its commands remain separate.

```powershell
npm ci --ignore-scripts
npm test
npm pack --ignore-scripts
```

Tests isolate HOME, USERPROFILE, CODEX_HOME, and service URLs in temp folders.
They use fake services and credentials and never use a real ChatGPT quota.

Official references: [Gateway setup with ChatGPT sign-in](https://learn.chatgpt.com/docs/enterprise/sign-in-with-chatgpt-through-a-gateway),
[provider configuration](https://learn.chatgpt.com/docs/config-file/config-reference).
