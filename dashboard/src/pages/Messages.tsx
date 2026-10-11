import { useEffect, useState } from "react";
import { Check, Mail, RotateCcw } from "lucide-react";
import { Badge, Card, GhostButton } from "../components/ui";
import { apiFetch } from "../lib/api";
import { cn } from "../lib/utils";

type Message = {
  id: string;
  name: string;
  email: string;
  topic: string;
  message: string;
  created_at: string;
  handled_at: string | null;
};
type State = "open" | "handled" | "all";
const STATES: { id: State; label: string }[] = [
  { id: "open", label: "Open" },
  { id: "handled", label: "Handled" },
  { id: "all", label: "All" },
];

/** Contact form messages from the landing page. Admin accounts only. */
export default function Messages() {
  const [state, setState] = useState<State>("open");
  const [messages, setMessages] = useState<Message[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");

  const load = async (which: State) =>
    setMessages(await apiFetch<Message[]>(`/admin/contact-messages?state=${which}`));

  useEffect(() => {
    setLoading(true);
    setError("");
    load(state)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [state]);

  const mark = async (id: string, handled: boolean) => {
    if (busy) return;
    setBusy(id);
    setError("");
    try {
      await apiFetch(`/admin/contact-messages/${id}/handled`, {
        method: "POST",
        body: { handled },
      });
      await load(state);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to update the message");
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-xl text-sm text-ink-3">
          Messages sent through the Contact page on the website. Reply by email,
          then mark them handled.
        </p>
        <div className="flex gap-1 rounded-lg border border-ink/15 p-1">
          {STATES.map(({ id, label }) => (
            <button
              key={id}
              onClick={() => setState(id)}
              className={cn(
                "rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
                state === id ? "bg-ember-soft text-ink" : "text-ink-3 hover:text-ink",
              )}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
      {error && (
        <p role="alert" className="text-sm text-red-500">
          {error}
        </p>
      )}
      {loading && <p className="text-sm text-ink-3">Loading messages…</p>}
      {!loading && !messages.length && !error && (
        <Card className="p-6">
          <p className="text-sm text-ink-3">
            {state === "open" ? "No open messages." : "No messages here yet."}
          </p>
        </Card>
      )}
      <div className="flex flex-col gap-4">
        {messages.map((m) => (
          <Card key={m.id} hairline className="p-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h3 className="font-semibold text-ink">{m.name}</h3>
                <p className="mt-0.5 text-xs text-ink-3">
                  {m.email} · {new Date(m.created_at).toLocaleString()}
                </p>
              </div>
              <div className="flex items-center gap-2">
                <Badge tone="slate">{m.topic}</Badge>
                <Badge tone={m.handled_at ? "green" : "amber"}>
                  {m.handled_at ? "handled" : "open"}
                </Badge>
              </div>
            </div>
            <p className="mt-4 whitespace-pre-wrap break-words text-sm leading-relaxed text-ink-2">
              {m.message}
            </p>
            <div className="mt-4 flex flex-wrap justify-end gap-3">
              <a
                href={`mailto:${m.email}?subject=${encodeURIComponent("Re: your message to ContextShrink")}`}
                className="btn-ghost inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium"
              >
                <Mail size={14} /> Reply
              </a>
              <GhostButton onClick={() => mark(m.id, !m.handled_at)} disabled={busy === m.id}>
                {m.handled_at ? <RotateCcw size={14} /> : <Check size={14} />}
                {busy === m.id ? "Working…" : m.handled_at ? "Reopen" : "Mark handled"}
              </GhostButton>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}
