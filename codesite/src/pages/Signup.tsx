import { useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { AuthShell, fieldLabelCls } from "../components/AuthShell";
import { Turnstile, turnstileEnabled } from "../components/Turnstile";
import { Banner, Button } from "../components/ui";
import { safeNext, signUp } from "../lib/auth";
import { LANDING_URL } from "../lib/api";
import { TRIAL_DAYS } from "../lib/constants";

export default function Signup() {
  const navigate = useNavigate();
  const location = useLocation();
  const next = safeNext(new URLSearchParams(location.search).get("next"));
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [agree, setAgree] = useState(false);
  const [turnstileToken, setTurnstileToken] = useState<string | null>(null);
  const [turnstileReset, setTurnstileReset] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;
    if (!name.trim() || !email.trim() || !password) {
      setError("Fill in every field to continue.");
      return;
    }
    if (!email.includes("@")) {
      setError("That email does not look right.");
      return;
    }
    if (password.length < 8) {
      setError("Password needs at least 8 characters.");
      return;
    }
    if (password !== confirm) {
      setError("Passwords do not match.");
      return;
    }
    if (!agree) {
      setError("Accept the terms to continue.");
      return;
    }
    if (turnstileEnabled && !turnstileToken) {
      setError("Complete the security check first.");
      return;
    }
    setError(null);
    setBusy(true);
    try {
      await signUp(name.trim(), email.trim(), password, turnstileToken);
      navigate(next, { replace: true });
    } catch (e) {
      setTurnstileReset((n) => n + 1);
      setError(e instanceof Error ? e.message : "Account creation failed. Try again.");
      setBusy(false);
    }
  };

  return (
    <AuthShell
      title="Create your account"
      intro={`Start your ${TRIAL_DAYS}-day free trial. No card needed.`}
      wide
    >
      <form onSubmit={submit} noValidate className="flex flex-col gap-4">
        <div>
          <label htmlFor="signup-name" className={fieldLabelCls}>
            Name
          </label>
          <input
            id="signup-name"
            name="name"
            autoComplete="name"
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Ada Lovelace"
            className="field"
          />
        </div>
        <div>
          <label htmlFor="signup-email" className={fieldLabelCls}>
            Email
          </label>
          <input
            id="signup-email"
            name="email"
            type="email"
            autoComplete="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@example.com"
            className="field"
          />
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <label htmlFor="signup-password" className={fieldLabelCls}>
              Password
            </label>
            <input
              id="signup-password"
              name="password"
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="8 or more characters"
              aria-describedby="signup-password-hint"
              className="field"
            />
            <p id="signup-password-hint" className="mt-1 text-xs text-ink-3">
              At least 8 characters.
            </p>
          </div>
          <div>
            <label htmlFor="signup-confirm" className={fieldLabelCls}>
              Confirm password
            </label>
            <input
              id="signup-confirm"
              name="confirm"
              type="password"
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              placeholder="Type it again"
              className="field"
            />
          </div>
        </div>

        <label className="flex cursor-pointer items-start gap-3 text-sm leading-relaxed text-ink-2">
          <input
            type="checkbox"
            checked={agree}
            onChange={(e) => setAgree(e.target.checked)}
            className="mt-1 h-4 w-4 shrink-0 accent-[var(--cs-ember)]"
          />
          <span>
            I agree to the{" "}
            <a
              href={`${LANDING_URL}/terms.html`}
              target="_blank"
              rel="noopener noreferrer"
              className="underline underline-offset-2 hover:text-ink"
            >
              Terms of Service
            </a>{" "}
            and{" "}
            <a
              href={`${LANDING_URL}/privacy.html`}
              target="_blank"
              rel="noopener noreferrer"
              className="underline underline-offset-2 hover:text-ink"
            >
              Privacy Policy
            </a>
            .
          </span>
        </label>

        {error && <Banner tone="error">{error}</Banner>}

        <Turnstile onToken={setTurnstileToken} resetKey={turnstileReset} />

        <Button type="submit" disabled={busy} className="mt-1 w-full">
          {busy ? "Creating account…" : "Create account"}
        </Button>
      </form>

      <p className="mt-6 text-center text-sm text-ink-3">
        Already have an account?{" "}
        <Link
          to={`/login${location.search}`}
          className="font-medium text-ember-2 underline-offset-2 hover:underline"
        >
          Sign in
        </Link>
      </p>
    </AuthShell>
  );
}
