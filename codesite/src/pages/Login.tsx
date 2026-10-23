import { useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { AuthShell, fieldLabelCls } from "../components/AuthShell";
import { Banner, Button } from "../components/ui";
import { signIn, safeNext } from "../lib/auth";

const DASHBOARD_URL = "https://app.contextshrink.com";

export default function Login() {
  const navigate = useNavigate();
  const location = useLocation();
  const next = safeNext(new URLSearchParams(location.search).get("next"));
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;
    if (!email.trim() || !password) {
      setError("Enter your email and password.");
      return;
    }
    setError(null);
    setBusy(true);
    try {
      await signIn(email.trim(), password);
      navigate(next, { replace: true });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Sign in failed. Try again.");
      setBusy(false);
    }
  };

  const signupTo = `/signup${location.search}`;

  return (
    <AuthShell
      title="Sign in"
      intro="Use your ContextShrink account. The same account works on app.contextshrink.com."
    >
      <form onSubmit={submit} noValidate className="flex flex-col gap-4">
        <div>
          <label htmlFor="login-email" className={fieldLabelCls}>
            Email
          </label>
          <input
            id="login-email"
            name="email"
            type="email"
            autoComplete="email"
            autoFocus
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@example.com"
            className="field"
          />
        </div>
        <div>
          <label htmlFor="login-password" className={fieldLabelCls}>
            Password
          </label>
          <input
            id="login-password"
            name="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Your password"
            className="field"
          />
        </div>

        <a
          href={`${DASHBOARD_URL}/forgot-password`}
          className="-mt-1 self-end text-xs text-ink-3 underline-offset-2 transition hover:text-ink hover:underline"
        >
          Forgot password?
        </a>

        {error && <Banner tone="error">{error}</Banner>}

        <Button type="submit" disabled={busy} className="mt-1 w-full">
          {busy ? "Signing in…" : "Sign in"}
        </Button>
      </form>

      <p className="mt-6 text-center text-sm text-ink-3">
        New here?{" "}
        <Link to={signupTo} className="font-medium text-ember-2 underline-offset-2 hover:underline">
          Create an account
        </Link>
      </p>
    </AuthShell>
  );
}
