import { useEffect, useState, type FormEvent } from "react";
import { Badge, Card, GhostButton, GradientButton, SectionHeader } from "../components/ui";
import { beginSso } from "../lib/auth";
import { API_URL, apiFetch } from "../lib/api";
import { getSsoSessionTeamId } from "../lib/auth";

type SsoStatus = {
  configured: boolean;
  enabled: boolean;
  issuer?: string;
  client_id?: string;
  linked: boolean;
  available: boolean;
  redirect_uri?: string;
};

export default function TeamSso({ teamId, isOwner }: { teamId: string; isOwner: boolean }) {
  const [status, setStatus] = useState<SsoStatus | null>(null);
  const [issuer, setIssuer] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const canConnect = Boolean(status?.configured && status.enabled && status.available);
  const ssoSession = getSsoSessionTeamId() === teamId;
  const redirectUri = status?.redirect_uri ?? `${API_URL.replace(/\/+$/, "")}/auth/sso/callback`;
  const statusLabel = !status ? "Checking" : !status.available ? "Unavailable" : !status.configured ? "Not configured" : status.enabled ? "Enabled" : "Disabled";

  async function load() {
    try {
      const result = await apiFetch<SsoStatus>(`/teams/${encodeURIComponent(teamId)}/sso`);
      setStatus(result);
      setIssuer(result.issuer ?? "");
      setClientId(result.client_id ?? "");
      setEnabled(result.enabled);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to load Team SSO settings");
    }
  }
  useEffect(() => { void load(); }, [teamId]);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (status?.configured && !window.confirm("Saving changes the Team SSO configuration revision, requires every member to relink, and invalidates existing SSO sessions. Save these changes?")) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await apiFetch(`/teams/${encodeURIComponent(teamId)}/sso`, {
        method: "PUT",
        body: { issuer: issuer.trim(), client_id: clientId.trim(), client_secret: clientSecret, enabled },
      });
      setClientSecret("");
      setNotice("Team SSO settings saved.");
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : "Unable to save Team SSO settings"); }
    finally { setBusy(false); }
  }

  async function removeConfiguration() {
    if (busy || !window.confirm("Disable and remove this Team’s SSO configuration? Existing SSO sessions will be invalidated, and members will need another sign-in method until a new configuration is saved.")) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await apiFetch(`/teams/${encodeURIComponent(teamId)}/sso`, { method: "DELETE" });
      setClientSecret(""); setNotice("Team SSO configuration removed."); await load();
    } catch (e) { setError(e instanceof Error ? e.message : "Unable to remove Team SSO configuration"); }
    finally { setBusy(false); }
  }

  async function unlink() {
    if (busy || !window.confirm("Unlink your company identity from this account? You will need another sign-in method to connect it again.")) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await apiFetch(`/teams/${encodeURIComponent(teamId)}/sso/identity`, { method: "DELETE" });
      setNotice("Your company identity is unlinked."); await load();
    } catch (e) { setError(e instanceof Error ? e.message : "Unable to unlink company identity"); }
    finally { setBusy(false); }
  }

  async function connect() {
    if (busy || !canConnect) return;
    setBusy(true); setError("");
    try { await beginSso(teamId, true, `/team?id=${encodeURIComponent(teamId)}`); }
    catch (e) { setError(e instanceof Error ? e.message : "Unable to start company sign-in"); setBusy(false); }
  }

  return (
    <Card hairline className="p-6">
      <SectionHeader eyebrow="Company identity" title="Single sign-on" action={<Badge tone={status?.configured && status.enabled && status.available ? "green" : "slate"}>{statusLabel}</Badge>} />
      {isOwner && ssoSession && <p className="mt-3 rounded-lg border border-ink/10 bg-ink/5 p-3 text-sm text-ink-3">This SSO session is locked to its Team and cannot change SSO settings or link/unlink identities. Sign in with your password for SSO management or account recovery.</p>}
      {isOwner && !ssoSession && !status && <p className="mt-3 text-sm text-ink-3">Checking whether Team SSO is available…</p>}
      {isOwner && !ssoSession && status && !status.available && <p className="mt-3 rounded-lg border border-ink/10 bg-ink/5 p-3 text-sm text-ink-3">SSO settings are unavailable until the service operator approves this identity provider’s issuer host. Ask the operator to approve the host, then reload this page.</p>}
      {isOwner && !ssoSession && status?.available && <>
        <p className="mt-3 text-sm text-ink-3">Configure your company’s OpenID Connect issuer and client. The client secret is write-only and is never shown again. Set the identity provider’s redirect URI to <code className="select-all text-ink">{redirectUri}</code>.</p>
        <p className="mt-3 rounded-lg border border-ember/25 bg-ember-soft p-3 text-xs leading-relaxed text-ink">Saving changes the configuration revision. Every member must relink their identity, and existing SSO sessions will be invalidated.</p>
        <form onSubmit={(event) => void save(event)} className="mt-4 grid gap-3 sm:grid-cols-2">
          <label className="text-xs text-ink-3">Issuer URL<input required type="url" value={issuer} onChange={(event) => setIssuer(event.target.value)} placeholder="https://identity.example.com" className="mt-1 block w-full rounded-lg border border-ink/15 bg-card px-3 py-2 text-sm text-ink" /></label>
          <label className="text-xs text-ink-3">Client ID<input required value={clientId} onChange={(event) => setClientId(event.target.value)} className="mt-1 block w-full rounded-lg border border-ink/15 bg-card px-3 py-2 text-sm text-ink" /></label>
          <label className="text-xs text-ink-3 sm:col-span-2">Client secret<input required type="password" autoComplete="new-password" value={clientSecret} onChange={(event) => setClientSecret(event.target.value)} placeholder={status?.configured ? "Enter a secret to replace the saved one" : "Client secret"} className="mt-1 block w-full rounded-lg border border-ink/15 bg-card px-3 py-2 text-sm text-ink" /></label>
          <label className="flex items-center gap-2 text-sm text-ink-2 sm:col-span-2"><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />Enable SSO for this Team</label>
          <div className="flex flex-wrap gap-3 sm:col-span-2"><GradientButton disabled={busy}>{busy ? "Saving…" : "Save SSO settings"}</GradientButton>{status?.configured && <button type="button" disabled={busy} onClick={() => void removeConfiguration()} className="rounded-lg border border-ink/10 px-4 py-2 text-sm font-medium text-ink-3 hover:border-ember/40 disabled:opacity-60">Remove configuration</button>}</div>
        </form>
      </>}
      <div className="mt-5 border-t border-ink/10 pt-4">
        <div className="flex flex-wrap items-center justify-between gap-3"><div><div className="text-sm font-medium text-ink">Your company identity</div><p className="mt-1 text-xs text-ink-3">{ssoSession ? "This SSO session is locked to the Team; sign in with a password to manage identity links." : status?.linked ? "A company identity is linked to your existing account." : !status ? "Checking Team SSO availability…" : canConnect ? "Link your existing account to your company identity." : !status.available ? "The service operator must approve the issuer host before members can connect." : "The Team owner must configure and enable SSO before you can connect."}</p></div>{status?.linked ? <GhostButton disabled={busy || ssoSession} onClick={() => void unlink()}>Unlink identity</GhostButton> : <GradientButton disabled={busy || ssoSession || !canConnect} onClick={() => void connect()}>{busy ? "Connecting…" : "Connect company identity"}</GradientButton>}</div>
        <p className="mt-3 text-xs leading-relaxed text-ink-3">Existing members must sign in to their current account and link their company identity here. Identities are linked only after company authentication; matching email addresses do not link accounts automatically, and new accounts are not created just-in-time.</p>
        <p className="mt-2 text-xs leading-relaxed text-ink-3">SSO sessions are locked to their Team and cannot change SSO settings or link/unlink identities. Password sign-in remains the recovery and SSO management path.</p>
      </div>
      {error && <p role="alert" className="mt-3 text-sm text-ember">{error}</p>}{notice && <p role="status" className="mt-3 text-sm text-sage">{notice}</p>}
    </Card>
  );
}
