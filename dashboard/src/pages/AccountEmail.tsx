import { useEffect, useRef, useState, type ReactNode } from "react";
import { motion } from "framer-motion";
import { ArrowLeft, KeyRound, MailCheck } from "lucide-react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { EASE, GradientButton } from "../components/ui";
import { apiFetch, getToken } from "../lib/api";

const inputCls =
  "w-full rounded-lg border border-ink/20 bg-ink/5 px-3.5 py-2.5 text-sm text-ink outline-none transition placeholder:text-ink-3 focus:border-ember/50 focus:ring-2 focus:ring-ember/20";
const backLinkCls =
  "inline-flex items-center gap-1.5 rounded-lg border border-ink/10 bg-ink/5 px-3 py-1.5 text-xs text-ink-3 transition hover:border-ember/40 hover:text-ink";

function AuthCard({
  icon,
  title,
  subtitle,
  children,
}: {
  icon: ReactNode;
  title: string;
  subtitle: string;
  children: ReactNode;
}) {
  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.95, y: 18 }}
      animate={{ opacity: 1, scale: 1, y: 0 }}
      transition={{ duration: 0.5, ease: EASE }}
      className="ink-card w-[420px] max-w-full rounded-lg p-9"
    >
      <div className="mx-auto grid h-14 w-14 place-items-center rounded-lg bg-ink text-paper shadow-[4px_4px_0_0_rgba(29,23,18,0.9)]">
        {icon}
      </div>
      <h1 className="mt-5 text-center font-display text-2xl font-bold text-ink">{title}</h1>
      <p className="mt-1.5 text-center text-sm text-ink-3">{subtitle}</p>
      <div className="mt-7 flex flex-col gap-4">{children}</div>
      <div className="mt-6 flex justify-center border-t border-white/5 pt-5">
        <Link to="/login" className={backLinkCls}>
          <ArrowLeft size={13} /> Back to sign in
        </Link>
      </div>
    </motion.div>
  );
}

function Notice({ tone, children }: { tone: "ok" | "error"; children: ReactNode }) {
  return (
    <p
      role={tone === "error" ? "alert" : "status"}
      className={
        tone === "error"
          ? "rounded-lg border border-ember/25 bg-ember-soft px-3 py-2 text-xs text-ember"
          : "rounded-lg border border-sage/25 bg-sage-soft px-3 py-2 text-xs text-sage"
      }
    >
      {children}
    </p>
  );
}

/** "Forgot password": always the same answer, whether or not the address exists. */
export function ForgotPassword() {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    if (busy) return;
    if (!email.trim()) {
      setError("Enter the email address you signed up with.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await apiFetch("/auth/password/forgot", { method: "POST", body: { email: email.trim() } });
      setSent(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong. Try again.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthCard
      icon={<KeyRound size={22} className="text-paper" />}
      title="Forgot your password?"
      subtitle="We'll email you a link to choose a new one."
    >
      {sent ? (
        <Notice tone="ok">
          If an account uses {email.trim()}, a reset link is on its way. It works for 30
          minutes. Check your spam folder if it doesn't arrive.
        </Notice>
      ) : (
        <>
          <label className="block">
            <span className="mb-1.5 block text-xs font-medium text-ink-3">Email</span>
            <input
              autoFocus
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className={inputCls}
              onKeyDown={(e) => e.key === "Enter" && submit()}
            />
          </label>
          {error && <Notice tone="error">{error}</Notice>}
          <GradientButton onClick={submit} className="mt-1 w-full justify-center">
            {busy ? "Sending…" : "Email me a reset link"}
          </GradientButton>
        </>
      )}
    </AuthCard>
  );
}

/** Opened from the reset email: /reset-password?token=… */
export function ResetPassword() {
  const [params] = useSearchParams();
  const token = params.get("token") ?? "";
  const navigate = useNavigate();
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(
    token ? null : "This link is missing its code. Open the link from your email again.",
  );

  const submit = async () => {
    if (busy || !token) return;
    if (password.length < 8) {
      setError("Use at least 8 characters.");
      return;
    }
    if (password !== confirm) {
      setError("The two passwords don't match.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await apiFetch("/auth/password/reset", { method: "POST", body: { token, password } });
      setDone(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't reset your password.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthCard
      icon={<KeyRound size={22} className="text-paper" />}
      title="Choose a new password"
      subtitle="You'll be signed out everywhere else."
    >
      {done ? (
        <>
          <Notice tone="ok">Your password is changed. Sign in with it now.</Notice>
          <GradientButton onClick={() => navigate("/login")} className="w-full justify-center">
            Sign in
          </GradientButton>
        </>
      ) : (
        <>
          <label className="block">
            <span className="mb-1.5 block text-xs font-medium text-ink-3">New password</span>
            <input
              autoFocus
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className={inputCls}
            />
          </label>
          <label className="block">
            <span className="mb-1.5 block text-xs font-medium text-ink-3">Confirm it</span>
            <input
              type="password"
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              className={inputCls}
              onKeyDown={(e) => e.key === "Enter" && submit()}
            />
          </label>
          {error && <Notice tone="error">{error}</Notice>}
          {error?.includes("expired") && (
            <Link to="/forgot-password" className="text-center text-xs text-ink-3 underline">
              Send a new reset link
            </Link>
          )}
          <GradientButton onClick={submit} className="mt-1 w-full justify-center">
            {busy ? "Saving…" : "Set new password"}
          </GradientButton>
        </>
      )}
    </AuthCard>
  );
}

/** Opened from the confirmation email: /verify-email?token=… */
export function VerifyEmail() {
  const [params] = useSearchParams();
  const token = params.get("token") ?? "";
  const [state, setState] = useState<"working" | "done" | "error">(token ? "working" : "error");
  const [error, setError] = useState(
    token ? "" : "This link is missing its code. Open the link from your email again.",
  );
  const started = useRef(false);

  useEffect(() => {
    if (!token || started.current) return;
    started.current = true; // StrictMode runs effects twice; the token is single-use.
    apiFetch("/auth/email/verify", { method: "POST", body: { token } })
      .then(() => setState("done"))
      .catch((e) => {
        setError(e instanceof Error ? e.message : "Couldn't confirm your email.");
        setState("error");
      });
  }, [token]);

  const signedIn = Boolean(getToken());
  return (
    <AuthCard
      icon={<MailCheck size={22} className="text-paper" />}
      title="Confirm your email"
      subtitle="One click and you can connect your tools."
    >
      {state === "working" && <p className="text-center text-sm text-ink-3">Confirming…</p>}
      {state === "done" && (
        <>
          <Notice tone="ok">Your email is confirmed. You can create API keys and use the desktop app.</Notice>
          <GradientButton
            onClick={() => window.location.assign(signedIn ? "/" : "/login")}
            className="w-full justify-center"
          >
            {signedIn ? "Go to your dashboard" : "Sign in"}
          </GradientButton>
        </>
      )}
      {state === "error" && <Notice tone="error">{error}</Notice>}
    </AuthCard>
  );
}
