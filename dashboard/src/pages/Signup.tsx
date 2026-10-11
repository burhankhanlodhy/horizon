import { useState } from "react";
import { motion } from "framer-motion";
import { ArrowLeft, Shrink } from "lucide-react";
import { useLocation, useNavigate } from "react-router-dom";
import { EASE, GradientButton } from "../components/ui";
import { getPostAuthDestination, signUp } from "../lib/auth";

const LANDING_URL =
  (import.meta.env.VITE_LANDING_URL as string | undefined) ??
  (import.meta.env.DEV ? "http://127.0.0.1:5174" : "https://contextshrink.com");

const inputCls =
  "w-full rounded-lg border border-ink/20 bg-ink/5 px-3.5 py-2.5 text-sm text-ink outline-none transition placeholder:text-ink-3 focus:border-ember/50 focus:ring-2 focus:ring-ember/20";

const backLinkCls =
  "inline-flex items-center gap-1.5 rounded-lg border border-ink/10 bg-ink/5 px-3 py-1.5 text-xs text-ink-3 transition hover:border-ember/40 hover:text-ink";

export default function Signup() {
  const navigate = useNavigate();
  const location = useLocation();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [agree, setAgree] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (busy) return;
    if (!name.trim() || !email.trim() || !password) {
      setError("Fill in every field to continue.");
      return;
    }
    if (!email.includes("@")) {
      setError("That email doesn't look right.");
      return;
    }
    if (password.length < 8) {
      setError("Password needs at least 8 characters.");
      return;
    }
    if (password !== confirm) {
      setError("Passwords don't match.");
      return;
    }
    if (!agree) {
      setError("Please accept the terms to continue.");
      return;
    }
    setError(null);
    setBusy(true);
    try {
      await signUp(name.trim(), email.trim(), password);
      if (new URLSearchParams(location.search).get("next") === "/dashboard") {
        window.location.assign("/dashboard");
        return;
      }
      navigate(getPostAuthDestination(location.state), { replace: true });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Account creation failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.95, y: 18 }}
      animate={{ opacity: 1, scale: 1, y: 0 }}
      transition={{ duration: 0.5, ease: EASE }}
      className="ink-card w-[460px] max-w-full rounded-lg p-9"
    >
      <motion.div
        initial={{ rotate: 10, scale: 0.7 }}
        animate={{ rotate: 0, scale: 1 }}
        transition={{ type: "spring", stiffness: 260, damping: 15, delay: 0.15 }}
        className="mx-auto grid h-14 w-14 place-items-center rounded-lg bg-ink text-paper shadow-[4px_4px_0_0_rgba(29,23,18,0.9)]"
      >
        <Shrink size={22} className="text-paper" />
      </motion.div>

      <h1 className="mt-5 text-center font-display text-2xl font-bold text-ink">
        Create your account
      </h1>
      <p className="mt-1.5 text-center text-sm text-ink-3">
        Start shrinking context in minutes.
      </p>

      <div className="mt-6 flex flex-col gap-3.5">
        <label className="block">
          <span className="mb-1.5 block text-xs font-medium text-ink-3">Name</span>
          <input
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Ada Lovelace"
            className={inputCls}
          />
        </label>
        <label className="block">
          <span className="mb-1.5 block text-xs font-medium text-ink-3">Email</span>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@contextshrink.com"
            className={inputCls}
          />
        </label>
        <div className="grid grid-cols-2 gap-3">
          <label className="block">
            <span className="mb-1.5 block text-xs font-medium text-slate-400">Password</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="8+ chars"
              className={inputCls}
            />
          </label>
          <label className="block">
            <span className="mb-1.5 block text-xs font-medium text-slate-400">Confirm</span>
            <input
              type="password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              placeholder="repeat"
              className={inputCls}
            />
          </label>
        </div>

        <label className="mt-1 flex cursor-pointer items-start gap-2.5 text-xs text-slate-400">
          <input
            type="checkbox"
            checked={agree}
            onChange={(e) => setAgree(e.target.checked)}
            className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-indigo-500"
          />
          <span>
            I agree to the terms of service and acknowledge that ContextShrink
            processes my agents' requests locally on my own hardware.
          </span>
        </label>

        {error && (
          <motion.p
            initial={{ opacity: 0, y: -6 }}
            animate={{ opacity: 1, y: 0 }}
            className="rounded-lg border border-rose-400/25 bg-rose-400/10 px-3 py-2 text-xs text-rose-300"
          >
            {error}
          </motion.p>
        )}

        <GradientButton onClick={submit} className="mt-1 w-full justify-center">
          {busy ? "Creating account…" : "Create account"}
        </GradientButton>
        <p className="text-center text-[11px] leading-relaxed text-ink-3">
          By creating an account you agree to our{" "}
          <a
            href={`${LANDING_URL}/terms.html`}
            target="_blank"
            rel="noopener noreferrer"
            className="underline transition hover:text-ink"
          >
            Terms of Service
          </a>{" "}
          and{" "}
          <a
            href={`${LANDING_URL}/privacy.html`}
            target="_blank"
            rel="noopener noreferrer"
            className="underline transition hover:text-ink"
          >
            Privacy Policy
          </a>
          .
        </p>
      </div>

      <p className="mt-5 text-center text-xs text-slate-500">
        Already have an account?{" "}
        <button
          onClick={() => navigate(`/login${location.search}`, { state: location.state })}
          className="font-medium text-indigo-300 transition hover:text-indigo-200"
        >
          Sign in
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
