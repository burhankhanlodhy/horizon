import { useEffect, useState } from "react";
import { KeyRound, Plus } from "lucide-react";
import {
  Badge,
  Card,
  CopyButton,
  GhostButton,
  GradientButton,
  Modal,
  SectionHeader,
} from "../components/ui";
import { apiFetch, PROXY_URL } from "../lib/api";
import { cn } from "../lib/utils";
import { useSelectedTeamId } from "../lib/workspace";

type Key = {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  requests: number;
owner_name?: string | null;
};
const SCOPES = ["proxy:messages", "proxy:responses"];
const inputCls =
  "w-full rounded-lg border border-ink/20 bg-ink/5 px-3.5 py-2.5 text-sm text-ink outline-none";

export default function ApiKeys() {
const teamId = useSelectedTeamId();
 const [workspaceName, setWorkspaceName] = useState("Personal");
  const [keys, setKeys] = useState<Key[]>([]);
  const [view, setView] = useState<"active" | "archived">("active");
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState(SCOPES);
  const [issued, setIssued] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const activeKeys = keys.filter((key) => !key.revoked_at);
  const archivedKeys = keys.filter((key) => Boolean(key.revoked_at));
  const visibleKeys = view === "active" ? activeKeys : archivedKeys;
  const load = async () => setKeys(await apiFetch<Key[]>("/keys"));
  useEffect(() => {
if (teamId) void apiFetch<{ team: { name: string } }>(`/teams/${encodeURIComponent(teamId)}`).then((result) => setWorkspaceName(result.team.name)).catch(() => setWorkspaceName("Team"));
    load()
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [teamId]);

  const create = async () => {
    if (!name.trim() || !scopes.length || busy) return;
    setBusy(true);
    setError("");
    try {
      const key = await apiFetch<Key & { key: string }>("/keys", {
        method: "POST",
        body: { name: name.trim(), scopes },
      });
      const { key: secret, ...entry } = key;
      setKeys((old) => [entry, ...old]);
      setView("active");
      setIssued(secret);
      setName("");
      setOpen(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to create key");
    } finally {
      setBusy(false);
    }
  };
  const revoke = async (id: string) => {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await apiFetch(`/keys/${id}`, { method: "DELETE" });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to revoke key");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-xl text-sm text-ink-3">Showing keys for <strong className="text-ink">{workspaceName}</strong>. Requests are billed to this workspace. Team keys belong to each member; personal keys stay in Personal.</p>
        <GradientButton
          onClick={() => {
            setIssued(null);
            setOpen(true);
          }}
        >
          <Plus size={15} /> Create key
        </GradientButton>
      </div>
      <div className="flex" role="group" aria-label="API key views">
        <div className="flex rounded-lg border border-ink/10 p-1 text-sm">
          {(["active", "archived"] as const).map((option) => (
            <button
              key={option}
              type="button"
              aria-pressed={view === option}
              aria-controls="api-key-list"
              onClick={() => setView(option)}
              className={cn(
                "rounded-md px-4 py-2 transition",
                view === option
                  ? "bg-ink font-semibold text-paper"
                  : "text-ink-3 hover:text-ink",
              )}
            >
              {option === "active" ? "Active" : "Archived"}
              {!loading && ` (${option === "active" ? activeKeys.length : archivedKeys.length})`}
            </button>
          ))}
        </div>
      </div>
      {error && (
        <p role="alert" className="text-sm text-red-500">
          {error}
        </p>
      )}
      {loading && <p className="text-sm text-ink-3">Loading keys…</p>}
      <div id="api-key-list" role="region" aria-label={view === "active" ? "Active API keys" : "Archived API keys"}>
        {!loading && !visibleKeys.length && !error && (
          <Card className="p-6">
            <p className="text-sm text-ink-3">
              {view === "archived"
                ? "No revoked API keys."
                : keys.length
                  ? "No active API keys. Revoked keys are in Archived."
                  : "No API keys yet. Create one to connect a tool and start collecting your usage."}
            </p>
          </Card>
        )}
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        {visibleKeys.map((k) => (
          <Card key={k.id} hairline className="p-5">
            <div className="flex justify-between gap-3">
              <div>
                <h3 className="font-semibold text-ink">{k.name}</h3>
{k.owner_name && <p className="mt-1 text-xs text-ink-3">Owned by {k.owner_name}</p>}
                <p className="mt-1 text-xs text-ink-3">
                  Created {new Date(k.created_at).toLocaleString()}
                </p>
                {k.revoked_at && (
                  <p className="mt-1 text-xs text-ink-3">
                    Revoked {new Date(k.revoked_at).toLocaleString()}
                  </p>
                )}
              </div>
              <Badge tone={k.revoked_at ? "red" : "green"}>
                {k.revoked_at ? "revoked" : "active"}
              </Badge>
            </div>
            <code className="mt-4 block rounded-lg bg-ink/5 p-3 text-xs text-ink-2">
              {k.prefix}••••••••
            </code>
            <p className="mt-3 text-xs text-ink-3">{k.scopes.join(" · ")}</p>
            <div className="mt-4 flex items-center justify-between text-xs text-ink-3">
              <span>
                {k.requests.toLocaleString()} requests ·{" "}
                {k.last_used_at
                  ? `Last used ${new Date(k.last_used_at).toLocaleString()}`
                  : "Never used"}
              </span>
              {!k.revoked_at && (
                <GhostButton disabled={busy} onClick={() => revoke(k.id)}>
                  {busy ? "Working…" : "Revoke"}
                </GhostButton>
              )}
            </div>
          </Card>
        ))}
        </div>
      </div>
      <Card className="p-6">
        <SectionHeader eyebrow="Connect a tool" title="Account proxy access" />
        <p className="text-sm text-ink-3">Base URL</p>
        <div className="mt-2 flex items-center gap-2">
          <code className="text-sm text-ink">{PROXY_URL}</code>
          <CopyButton text={PROXY_URL} />
        </div>
        <p className="mt-4 text-sm leading-relaxed text-ink-3">
          Send your account key in <code>X-Horizon-Proxy-Token</code>. Keep your
          provider API key or provider login in its usual header. The
          ContextShrink desktop app sets this up for you. Use the responses scope
          for OpenAI Responses and the messages scope for Messages, Chat
          Completions or Gemini. For clients that append <code>/v1</code>, use
          the base URL above; otherwise append <code>/v1</code>.
        </p>
      </Card>
      <Modal open={open} onClose={() => setOpen(false)} title="Create API key">
        {error && (
          <p role="alert" className="mb-3 text-sm text-red-500">
            {error}
          </p>
        )}
        <label className="mb-2 block text-xs text-ink-3">Key name</label>
        <input
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          maxLength={100}
          className={inputCls}
          placeholder="e.g. Claude Code laptop"
        />
        <p className="mt-5 mb-2 text-xs text-ink-3">Allowed endpoints</p>
        <div className="flex gap-2">
          {SCOPES.map((s) => (
            <label key={s} className="text-xs text-ink">
              <input
                type="checkbox"
                checked={scopes.includes(s)}
                onChange={(e) =>
                  setScopes((old) =>
                    e.target.checked ? [...old, s] : old.filter((x) => x !== s),
                  )
                }
              />{" "}
              {s}
            </label>
          ))}
        </div>
        <div className="mt-6 flex justify-end gap-3">
          <GhostButton onClick={() => setOpen(false)}>Cancel</GhostButton>
          <GradientButton onClick={create}>
            <KeyRound size={14} /> {busy ? "Creating…" : "Create key"}
          </GradientButton>
        </div>
      </Modal>
      <Modal
        open={issued !== null}
        onClose={() => setIssued(null)}
        title="Copy your new key"
      >
        <p className="mb-4 text-sm text-ink-3">
          This is the only time the complete key is shown. Store it in your
          tool’s secret settings.
        </p>
        <div className="flex items-center gap-2">
          <code className="break-all text-xs text-ink">{issued}</code>
          <CopyButton text={issued ?? ""} />
        </div>
        <div className="mt-6 flex justify-end">
          <GradientButton onClick={() => setIssued(null)}>Done</GradientButton>
        </div>
      </Modal>
    </div>
  );
}
