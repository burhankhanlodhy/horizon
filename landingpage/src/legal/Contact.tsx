import { useState, type FormEvent } from "react";
import { ArrowRight, CheckCircle2 } from "lucide-react";
import { API_URL } from "../components/ui";
import { LegalPage } from "./LegalPage";

const TOPICS = [
  { id: "general", label: "General question" },
  { id: "support", label: "Help with setup or the desktop app" },
  { id: "billing", label: "Billing and plans" },
  { id: "privacy", label: "Privacy or data request" },
  { id: "security", label: "Report a security issue" },
] as const;

const fieldCls =
  "w-full rounded-md border border-line bg-paper px-3.5 py-2.5 text-ink outline-none transition focus:border-ink";
const labelCls = "mb-1.5 block font-mono text-xs uppercase tracking-[0.15em] text-ink-3";

type Status = "idle" | "sending" | "sent" | "error";

export function Contact() {
  const [form, setForm] = useState({
    name: "",
    email: "",
    topic: "general",
    message: "",
    website: "",
  });
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState("");
  const set = (key: keyof typeof form) => (value: string) =>
    setForm((f) => ({ ...f, [key]: value }));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (status === "sending") return;
    setStatus("sending");
    setError("");
    try {
      const res = await fetch(`${API_URL}/contact`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      });
      if (res.ok) {
        setStatus("sent");
        return;
      }
      let message = "We couldn't send your message. Please try again.";
      if (res.status === 429) {
        message = "You've sent several messages just now. Please wait a few minutes and try again.";
      } else if (res.status === 422) {
        message = "Please check your email address, and write at least a sentence or two.";
      }
      setError(message);
      setStatus("error");
    } catch {
      setError("We couldn't reach our server. Check your connection and try again.");
      setStatus("error");
    }
  };

  return (
    <LegalPage
      title="Contact us"
      intro={
        <p>
          Questions about ContextShrink, help getting set up, billing, or a privacy
          request: send us a message and we&rsquo;ll reply by email.
        </p>
      }
    >
      {status === "sent" ? (
        <div className="flex items-start gap-3 rounded-md border border-line bg-sage-soft p-6">
          <CheckCircle2 className="mt-0.5 shrink-0 text-sage" size={20} />
          <div>
            <p className="font-semibold text-ink">Thanks, your message is on its way.</p>
            <p className="mt-1 text-ink-2">
              We&rsquo;ll reply to {form.email} as soon as we can.
            </p>
          </div>
        </div>
      ) : (
        <form onSubmit={submit} className="flex flex-col gap-5" noValidate={false}>
          <div className="grid grid-cols-1 gap-5 sm:grid-cols-2">
            <div>
              <label htmlFor="name" className={labelCls}>
                Name
              </label>
              <input
                id="name"
                required
                maxLength={100}
                autoComplete="name"
                value={form.name}
                onChange={(e) => set("name")(e.target.value)}
                className={fieldCls}
              />
            </div>
            <div>
              <label htmlFor="email" className={labelCls}>
                Email
              </label>
              <input
                id="email"
                type="email"
                required
                maxLength={254}
                autoComplete="email"
                value={form.email}
                onChange={(e) => set("email")(e.target.value)}
                className={fieldCls}
              />
            </div>
          </div>
          <div>
            <label htmlFor="topic" className={labelCls}>
              Topic
            </label>
            <select
              id="topic"
              value={form.topic}
              onChange={(e) => set("topic")(e.target.value)}
              className={fieldCls}
            >
              {TOPICS.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.label}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="message" className={labelCls}>
              Message
            </label>
            <textarea
              id="message"
              required
              minLength={10}
              maxLength={5000}
              rows={7}
              value={form.message}
              onChange={(e) => set("message")(e.target.value)}
              className={fieldCls}
            />
            <p className="mt-1.5 text-right font-mono text-[11px] text-ink-3">
              {form.message.length} / 5000
            </p>
          </div>
          {/* Hidden from people; form-filling bots complete it and are dropped. */}
          <div aria-hidden className="absolute -left-[9999px] h-px w-px overflow-hidden">
            <label htmlFor="website">Leave this empty</label>
            <input
              id="website"
              tabIndex={-1}
              autoComplete="off"
              value={form.website}
              onChange={(e) => set("website")(e.target.value)}
            />
          </div>
          {error && (
            <p role="alert" className="text-sm text-ember">
              {error}
            </p>
          )}
          <div className="flex flex-wrap items-center justify-between gap-4">
            <p className="max-w-sm text-sm text-ink-3">
              We use your details only to answer you. See our{" "}
              <a href="/privacy.html" className="text-ember underline">
                Privacy Policy
              </a>
              .
            </p>
            <button
              type="submit"
              disabled={status === "sending"}
              className="inline-flex items-center gap-2 rounded-md border-[1.5px] border-ink bg-ink px-5 py-2.5 font-semibold text-paper transition hover:bg-ember disabled:opacity-60"
            >
              {status === "sending" ? "Sending…" : "Send message"} <ArrowRight size={15} />
            </button>
          </div>
        </form>
      )}
    </LegalPage>
  );
}
