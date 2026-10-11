import { useState } from "react";
import { motion } from "framer-motion";
import { ArrowLeft, LogIn } from "lucide-react";
import { useLocation, useNavigate } from "react-router-dom";
import { EASE, GradientButton } from "../components/ui";
import { getPostAuthDestination, signIn } from "../lib/auth";

const LANDING_URL =
  (import.meta.env.VITE_LANDING_URL as string | undefined) ??
  (import.meta.env.DEV ? "http://127.0.0.1:5174" : "https://contextshrink.com");

const inputCls =
  "w-full rounded-lg border border-ink/20 bg-ink/5 px-3.5 py-2.5 text-sm text-ink outline-none transition placeholder:text-ink-3 focus:border-ember/50 focus:ring-2 focus:ring-ember/20";

const backLinkCls =
  "inline-flex items-center gap-1.5 rounded-lg border border-ink/10 bg-ink/5 px-3 py-1.5 text-xs text-ink-3 transition hover:border-ember/40 hover:text-ink";

export default function Login() {
  const navigate = useNavigate();
  const location = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (busy) return;
    if (!email.trim() || !password) {
      setError("Enter your email and password.");
      return;
    }
    setError(null);
    setBusy(true);
    try {
      await signIn(email.trim(), password);
      if (new URLSearchParams(location.search).get("next") === "/dashboard") {
        window.location.assign("/dashboard");
        return;
      }
      navigate(getPostAuthDestination(location.state), { replace: true });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Sign in failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.95, y: 18 }}
      animate={{ opacity: 1, scale: 1, y: 0 }}
      transition={{ duration: 0.5, ease: EASE }}
      className="ink-card w-[420px] max-w-full rounded-lg p-9"
    >
      <motion.div
        initial={{ rotate: -10, scale: 0.7 }}
        animate={{ rotate: 0, scale: 1 }}
        transition={{ type: "spring", stiffness: 260, damping: 15, delay: 0.15 }}
        className="mx-auto grid h-14 w-14 place-items-center rounded-lg bg-ink text-paper shadow-[4px_4px_0_0_rgba(29,23,18,0.9)]"
      >
        <LogIn size={22} className="text-paper" />
      </motion.div>

      <h1 className="mt-5 text-center font-display text-2xl font-bold text-ink">
        Welcome back
      </h1>
      <p className="mt-1.5 text-center text-sm text-ink-3">
        Sign in to your ContextShrink dashboard.
      </p>

      <div className="mt-7 flex flex-col gap-4">
        <label className="block">
          <span className="mb-1.5 block text-xs font-medium text-ink-3">Email</span>
          <input
            autoFocus
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@contextshrink.com"
            className={inputCls}
            onKeyDown={(e) => e.key === "Enter" && submit()}
          />
        </label>
        <label className="block">
          <span className="mb-1.5 block text-xs font-medium text-ink-3">Password</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="••••••••"
            className={inputCls}
            onKeyDown={(e) => e.key === "Enter" && submit()}
          />
        </label>
        <button
          type="button"
          onClick={() => navigate("/forgot-password")}
          className="-mt-2 self-end text-xs text-ink-3 underline-offset-2 transition hover:text-ink hover:underline"
        >
          Forgot password?
        </button>

        {error && (
          <motion.p
            initial={{ opacity: 0, y: -6 }}
            animate={{ opacity: 1, y: 0 }}
            className="rounded-lg border border-ember/25 bg-ember-soft px-3 py-2 text-xs text-ember"
          >
            {error}
          </motion.p>
        )}

        <GradientButton onClick={submit} className="mt-1 w-full justify-center">
          {busy ? "Signing in…" : "Sign in"}
        </GradientButton>
      </div>

      <p className="mt-5 text-center text-xs text-slate-500">
        No account yet?{" "}
        <button
          onClick={() => navigate(`/signup${location.search}`, { state: location.state })}
          className="font-medium text-indigo-300 transition hover:text-indigo-200"
        >
          Create one
        </button>
      </p>

      <div className="mt-6 flex justify-center border-t border-white/5 pt-5">
        <a href={LANDING_URL} className={backLinkCls}>
          <ArrowLeft size={13} /> Back to contextshrink.com
        </a>
      </div>
    </motion.div>
  );
}
