import { useEffect, useState } from "react";
import { Ban, Plus } from "lucide-react";
import { Badge, Card, CopyButton, GhostButton, GradientButton } from "../components/ui";
import { apiFetch } from "../lib/api";

type Invite = {
  id: string;
  code: string;
  note: string;
  max_uses: number;
  uses: number;
  expires_at: string | null;
  revoked_at: string | null;
  created_at: string;
};

const inputCls =
  "w-full rounded-lg border border-ink/20 bg-ink/5 px-3 py-2 text-sm text-ink outline-none transition placeholder:text-ink-3 focus:border-ember/50 focus:ring-2 focus:ring-ember/20";

function signupLink(code: string) {
  return `${window.location.origin}/signup?invite=${encodeURIComponent(code)}`;
}

function status(i: Invite): { label: string; tone: "green" | "amber" | "slate" } {
  if (i.revoked_at) return { label: "revoked", tone: "slate" };
  if (i.expires_at && new Date(i.expires_at) <= new Date()) return { label: "expired", tone: "slate" };
  if (i.uses >= i.max_uses) return { label: "used up", tone: "amber" };
  return { label: "active", tone: "green" };
}

/** Beta invite codes: create, share and revoke. Admin accounts only. */
export default function Invites() {
  const [invites, setInvites] = useState<Invite[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [maxUses, setMaxUses] = useState("1");
  const [expiresDays, setExpiresDays] = useState("30");
  const [created, setCreated] = useState<Invite | null>(null);

  const load = async () => setInvites(await apiFetch<Invite[]>("/admin/invites"));

  useEffect(() => {
    load()
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  const create = async () => {
    if (busy) return;
    const uses = Number(maxUses);
    const days = expiresDays.trim() ? Number(expiresDays) : null;
    if (!Number.isInteger(uses) || uses < 1 || uses > 1000) {
      setError("Uses must be a whole number from 1 to 1000.");
      return;
    }
    if (days !== null && (!Number.isInteger(days) || days < 1 || days > 365)) {
      setError("Expiry must be 1 to 365 days, or empty for never.");
      return;
    }
    setBusy("create");
    setError("");
    try {
      const invite = await apiFetch<Invite>("/admin/invites", {
        method: "POST",
        body: { note: note.trim(), max_uses: uses, expires_in_days: days },
      });
      setCreated(invite);
      setNote("");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to create the invite");
    } finally {
      setBusy(null);
    }
  };

  const revoke = async (id: string) => {
    if (busy) return;
    setBusy(id);
    setError("");
    try {
      await apiFetch(`/admin/invites/${id}/revoke`, { method: "POST" });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to revoke the invite");
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="flex flex-col gap-6">
      <p className="max-w-xl text-sm text-ink-3">
        While the beta is invite-only, new accounts need one of these codes. Send
        people the signup link; each use creates one account.
      </p>

      <Card hairline className="p-5">
        <h3 className="font-semibold text-ink">New invite</h3>
        <div className="mt-4 grid gap-3 sm:grid-cols-[1fr_110px_130px_auto] sm:items-end">
          <label className="block">
            <span className="mb-1.5 block text-xs font-medium text-ink-3">Who is it for?</span>
            <input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              maxLength={200}
              placeholder="Sam, from the meetup"
              className={inputCls}
            />
          </label>
          <label className="block">
            <span className="mb-1.5 block text-xs font-medium text-ink-3">Uses</span>
            <input
              type="number"
              min={1}
              max={1000}
              value={maxUses}
              onChange={(e) => setMaxUses(e.target.value)}
              className={inputCls}
            />
          </label>
          <label className="block">
            <span className="mb-1.5 block text-xs font-medium text-ink-3">Expires (days)</span>
            <input
              type="number"
              min={1}
              max={365}
              value={expiresDays}
              onChange={(e) => setExpiresDays(e.target.value)}
              placeholder="never"
              className={inputCls}
            />
          </label>
          <GradientButton onClick={create} className="justify-center">
            <Plus size={14} /> {busy === "create" ? "Creating…" : "Create"}
          </GradientButton>
        </div>
        {created && (
          <div className="mt-4 flex flex-wrap items-center gap-3 rounded-lg border border-sage/30 bg-sage/10 px-4 py-3">
            <span className="text-sm text-ink-2">
              Created <span className="font-mono font-semibold text-ink">{created.code}</span>
            </span>
            <CopyButton text={signupLink(created.code)} label="Copy signup link" />
          </div>
        )}
      </Card>

      {error && (
        <p role="alert" className="text-sm text-red-500">
          {error}
        </p>
      )}
      {loading && <p className="text-sm text-ink-3">Loading invites…</p>}
      {!loading && !invites.length && !error && (
        <Card className="p-6">
          <p className="text-sm text-ink-3">No invites yet.</p>
        </Card>
      )}

      <div className="flex flex-col gap-3">
        {invites.map((i) => {
          const s = status(i);
          return (
            <Card key={i.id} hairline className="p-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-sm font-semibold text-ink">{i.code}</span>
                    <Badge tone={s.tone}>{s.label}</Badge>
                  </div>
                  <p className="mt-1 truncate text-xs text-ink-3">
                    {i.note || "No note"} · {i.uses}/{i.max_uses} used · created{" "}
                    {new Date(i.created_at).toLocaleDateString()}
                    {i.expires_at && ` · expires ${new Date(i.expires_at).toLocaleDateString()}`}
                  </p>
                </div>
                {s.label === "active" && (
                  <div className="flex items-center gap-2">
                    <CopyButton text={signupLink(i.code)} label="Copy link" />
                    <GhostButton onClick={() => revoke(i.id)} disabled={busy === i.id}>
                      <Ban size={14} /> {busy === i.id ? "Revoking…" : "Revoke"}
                    </GhostButton>
                  </div>
                )}
              </div>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
