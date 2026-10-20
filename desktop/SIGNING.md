# Windows installer signing (Azure Artifact Signing)

The Windows installer (`ContextShrink_<version>_x64-setup.exe`), the app exe, the
bundled Code-tab helpers and the frozen Horizon client are signed with Azure
Artifact Signing (formerly Trusted Signing). Signing is off until the repo is
configured; until then the build is unsigned and says so. The updater signature
(`TAURI_SIGNING_PRIVATE_KEY`, minisign) is separate and unchanged.

How it fits together:

| Piece | Role |
|---|---|
| `desktop/sign.ps1` | Signs the files given (signtool + Artifact Signing dlib), or prints `Signing skipped (not configured)`. Verifies every signature. |
| `desktop/app/src-tauri/tauri.signing.generated.json` | Written by `build-installer.ps1` (git-ignored): `bundle.windows.signCommand`, so Tauri signs the app exe and installer through `sign.ps1`. |
| `desktop/build-installer.ps1` | When configured, signs the helper exes and the client's files that are not already valid, then adds the signing config to `tauri build`. |
| `.github/workflows/desktop.yml`, job `windows` | Azure OIDC login, dlib install, build, smoke test, signature check, upload. |

## One-time Azure setup (owner)

1. **Artifact Signing account.** Confirm the account, identity validation and the
   certificate profile `ContextShrink` exist. Note the account's region.
2. **Entra app registration.** Entra admin center > App registrations > New
   registration (single tenant). Create no client secret: CI uses OIDC.
   From its Overview page, note the **Application (client) ID** and the
   **Directory (tenant) ID**.
3. **Federated credential for GitHub.** App > Certificates & secrets > Federated
   credentials > Add credential > scenario *GitHub Actions deploying Azure
   resources*. Organization `burhankhanlodhy`, repository `horizon`, entity type
   **Environment**, value **`release`**. The portal fills in the subject
   (`repo:burhankhanlodhy/horizon:environment:release`). If CI sign-in fails with a
   subject mismatch, compare the `sub` claim in the job log with this credential.
4. **Signer role.** On the certificate profile `ContextShrink` (Access control
   (IAM)), add a role assignment for **Artifact Signing Certificate Profile Signer**
   to the app registration.
5. **GitHub environment.** Repo Settings > Environments > New environment
   **`release`** (optional: required reviewers). The `windows` job runs in it.
6. **Repo variables** (Settings > Secrets and variables > Actions > Variables).
   Add these; they are not secrets.

   | Variable | Value and where to find it |
   |---|---|
   | `AZURE_CLIENT_ID` | Application (client) ID from step 2. Setting it turns signing on. |
   | `AZURE_TENANT_ID` | Directory (tenant) ID from step 2. |
   | `AZURE_SUBSCRIPTION_ID` | Subscription that holds the Artifact Signing account (Subscriptions blade, or the account's Overview). |
   | `ARTIFACT_SIGNING_ENDPOINT` | Regional endpoint that matches the account's region, for example `https://wus2.codesigning.azure.net`. The region table is in the Microsoft Learn page below. A mismatch gives a 403 at signing time. |
   | `ARTIFACT_SIGNING_ACCOUNT` | The Artifact Signing account resource name (account Overview). |
   | `ARTIFACT_SIGNING_PROFILE` | `ContextShrink` (the certificate profile name). |

   The existing secret `TAURI_SIGNING_PRIVATE_KEY` is unchanged. If `AZURE_CLIENT_ID`
   is set but an `ARTIFACT_SIGNING_*` variable is empty, the job fails before the build.

## Sign locally (Windows PC)

1. Install the Artifact Signing Client Tools, which provide the dlib (the .NET 8
   runtime is a prerequisite): `winget install -e --id Microsoft.Azure.ArtifactSigningClientTools`.
   If the dlib is not found automatically, set `CS_SIGN_DLIB` to the full path of
   `Azure.CodeSigning.Dlib.dll` (x64).
2. `az login` with an account that holds the signer role.
3. In the same PowerShell window:

   ```powershell
   $env:CS_SIGN_ENDPOINT = 'https://wus2.codesigning.azure.net'   # your region
   $env:CS_SIGN_ACCOUNT  = '<account name>'
   $env:CS_SIGN_PROFILE  = 'ContextShrink'
   .\desktop\sign.ps1 'C:\path\to\file.exe'
   ```

   Leave the three variables unset and the same command prints
   `Signing skipped (not configured)` and exits 0. With the variables set, a full
   local `.\desktop\build-installer.ps1` signs automatically.

## Verify a signed file

```powershell
Get-AuthenticodeSignature .\ContextShrink_0.7.5_x64-setup.exe |
    Format-List Status, StatusMessage, SignerCertificate, TimeStamperCertificate
```

`Status` must be `Valid` and `TimeStamperCertificate` must be set. `signtool verify /pa /v <file>`
(Windows SDK) gives the same result. The Explorer check is Properties > Digital Signatures.

## Sources

- Microsoft Learn, *Set up signing integrations to use Artifact Signing* (SignTool, dlib, `metadata.json`, endpoints, timestamp server, RBAC role): https://learn.microsoft.com/en-us/azure/artifact-signing/how-to-signing-integrations
- Microsoft Learn, *Workload identity federation: create a trust* (GitHub entity types): https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-create-trust
- Tauri v2, *Windows code signing* (`bundle.windows.signCommand`): https://v2.tauri.app/distribute/sign/windows/
- NuGet, `Microsoft.ArtifactSigning.Client` (dlib, pinned to 1.0.146 in the workflow): https://www.nuget.org/packages/Microsoft.ArtifactSigning.Client
- `azure/artifact-signing-action` was reviewed but not used: it signs a folder after the build, and Tauri needs each exe signed during bundling.
