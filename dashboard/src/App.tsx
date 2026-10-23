import { useEffect, useState } from "react";
import { Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";
import { AuroraBackground, FullPageCenter, Shell } from "./components/layout";
import Profile from "./pages/Profile";
import ApiKeys from "./pages/ApiKeys";
import Usage from "./pages/Usage";
import Subscriptions from "./pages/Subscriptions";
import Documentation from "./pages/Documentation";
import Downloads from "./pages/Downloads";
import SignOut from "./pages/SignOut";
import Login from "./pages/Login";
import Signup from "./pages/Signup";
import Messages from "./pages/Messages";
import Team, { AcceptTeamInvitation } from "./pages/Team";
import SsoCallback from "./pages/SsoCallback";
import { ForgotPassword, ResetPassword, VerifyEmail } from "./pages/AccountEmail";
import { refreshUser, type SessionUser } from "./lib/auth";
import { AccountProvider, useAccount } from "./lib/account";

type SessionState = "checking" | "authenticated" | "unauthenticated" | "error";

/** The API enforces admin access; this only keeps others off the page. */
function AdminOnly({ children }: { children: JSX.Element }) {
  const { user } = useAccount();
  return user.is_admin ? children : <Navigate to="/" replace />;
}

function RequireAuth() {
  const location = useLocation();
  const [sessionState, setSessionState] = useState<SessionState>("checking");
  const [attempt, setAttempt] = useState(0);
  const [user, setUser] = useState<SessionUser | null>(null);

  useEffect(() => {
    let active = true;
    setSessionState("checking");

    refreshUser().then(
      (user) => {
        if (active) {
          setUser(user);
          setSessionState(user ? "authenticated" : "unauthenticated");
        }
      },
      () => {
        if (active) setSessionState("error");
      },
    );

    return () => {
      active = false;
    };
  }, [attempt]);

  if (sessionState === "checking") {
    return (
      <div className="relative z-10 grid h-screen place-items-center px-6">
        <div
          className="paper-panel flex items-center gap-3 rounded-lg px-5 py-4 text-sm text-ink-2"
          role="status"
        >
          <span className="live-dot" aria-hidden="true" />
          Checking your session…
        </div>
      </div>
    );
  }

  if (sessionState === "error") {
    return (
      <div className="relative z-10 grid h-screen place-items-center px-6">
        <div
          className="paper-panel max-w-md rounded-lg p-6 text-center"
          role="alert"
        >
          <h1 className="font-display text-xl font-semibold text-ink">
            We couldn’t verify your session
          </h1>
          <p className="mt-2 text-sm leading-relaxed text-ink-3">
            The account service couldn’t verify your session. Check the
            connection and try again.
          </p>
          <button
            className="btn btn-ink mt-5"
            onClick={() => {
              setSessionState("checking");
              setAttempt((current) => current + 1);
            }}
          >
            Try again
          </button>
        </div>
      </div>
    );
  }

  if (sessionState === "unauthenticated") {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }

  return user ? (
    <AccountProvider initialUser={user}>
      <Outlet />
    </AccountProvider>
  ) : null;
}

export default function App() {
  return (
    <>
      <AuroraBackground />
      <Routes>
        <Route element={<RequireAuth />}>
          <Route element={<Shell />}>
            <Route index element={<Profile />} />
            <Route path="keys" element={<ApiKeys />} />
            <Route path="usage" element={<Usage />} />
            <Route path="subscriptions" element={<Subscriptions />} />
            <Route path="team" element={<Team />} />
            <Route path="team-invitations/accept" element={<AcceptTeamInvitation />} />
            <Route path="downloads" element={<Downloads />} />
            <Route path="docs" element={<Documentation />} />
            <Route path="messages" element={<AdminOnly><Messages /></AdminOnly>} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Route>
        <Route
          path="/login"
          element={
            <FullPageCenter>
              <Login />
            </FullPageCenter>
          }
        />
        <Route path="/sso/callback" element={<FullPageCenter><SsoCallback /></FullPageCenter>} />
        <Route
          path="/signup"
          element={
            <FullPageCenter>
              <Signup />
            </FullPageCenter>
          }
        />
        <Route
          path="/signout"
          element={
            <FullPageCenter>
              <SignOut />
            </FullPageCenter>
          }
        />
        <Route
          path="/forgot-password"
          element={
            <FullPageCenter>
              <ForgotPassword />
            </FullPageCenter>
          }
        />
        <Route
          path="/reset-password"
          element={
            <FullPageCenter>
              <ResetPassword />
            </FullPageCenter>
          }
        />
        <Route
          path="/verify-email"
          element={
            <FullPageCenter>
              <VerifyEmail />
            </FullPageCenter>
          }
        />
      </Routes>
    </>
  );
}
