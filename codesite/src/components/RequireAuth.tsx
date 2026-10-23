import { useEffect, useState, type ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { refreshUser, type SessionUser } from "../lib/auth";
import { Banner, Button, Spinner } from "./ui";

type State =
  | { phase: "checking" }
  | { phase: "signed-in"; user: SessionUser }
  | { phase: "signed-out" }
  | { phase: "error" };

/**
 * Gate for pages that need an account. Signed-out visitors go to /login (or
 * /signup when `signedOutTo="signup"`) with ?next= set to the page they asked
 * for, so they land back on it after sign-in or sign-up.
 */
export function RequireAuth({
  children,
  signedOutTo = "login",
}: {
  children: (user: SessionUser) => ReactNode;
  signedOutTo?: "login" | "signup";
}) {
  const location = useLocation();
  const [state, setState] = useState<State>({ phase: "checking" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let active = true;
    setState({ phase: "checking" });
    refreshUser().then(
      (user) => {
        if (!active) return;
        setState(user ? { phase: "signed-in", user } : { phase: "signed-out" });
      },
      () => {
        if (active) setState({ phase: "error" });
      },
    );
    return () => {
      active = false;
    };
  }, [attempt]);

  if (state.phase === "checking") {
    return (
      <div className="grid min-h-[60vh] place-items-center px-4">
        <Spinner label="Checking your session" />
      </div>
    );
  }

  if (state.phase === "signed-out") {
    const next = `${location.pathname}${location.search}`;
    return <Navigate to={`/${signedOutTo}?next=${encodeURIComponent(next)}`} replace />;
  }

  if (state.phase === "error") {
    return (
      <div className="mx-auto flex max-w-md flex-col gap-4 px-4 py-16">
        <Banner tone="error">
          We could not check your session. Your connection may be down.
        </Banner>
        <Button variant="ghost" onClick={() => setAttempt((n) => n + 1)}>
          Try again
        </Button>
      </div>
    );
  }

  return <>{children(state.user)}</>;
}
