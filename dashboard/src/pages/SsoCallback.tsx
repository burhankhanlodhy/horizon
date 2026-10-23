import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Card, SectionHeader } from "../components/ui";
import {
  clearSsoCallbackContext,
  completeSsoLogin,
  exchangeSsoCode,
  getSsoCallbackContext,
} from "../lib/auth";
import { setSelectedTeamId } from "../lib/workspace";

export default function SsoCallback() {
  const navigate = useNavigate();
  const processed = useRef(false);
  const [message, setMessage] = useState("Completing your Team sign-in…");
  const [error, setError] = useState("");

  useEffect(() => {
    if (processed.current) return;
    processed.current = true;
    const params = new URLSearchParams(window.location.hash.slice(1));
    const code = params.get("code");
    const providerError = params.get("error");
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);

    if (providerError || !code) {
      clearSsoCallbackContext();
      setError(providerError ? "Your company sign-in was cancelled or failed. Try again or use your password." : "The SSO response was incomplete. Start again from your Team sign-in page.");
      return;
    }

    const context = getSsoCallbackContext();
    if (!context) {
      clearSsoCallbackContext();
      setError("This SSO sign-in expired. Start again from your Team’s sign-in page.");
      return;
    }

    void exchangeSsoCode(code).then((result) => {
      if (context.mode === "link") {
        if (!result.linked || result.token) throw new Error("The company identity could not be linked to this account.");
        setSelectedTeamId(result.team_id);
        clearSsoCallbackContext();
        navigate(`/team?id=${encodeURIComponent(result.team_id)}`, { replace: true });
        return;
      }
      completeSsoLogin(result);
      setSelectedTeamId(result.team_id);
      clearSsoCallbackContext();
      if (context.destination === "/dashboard") window.location.assign("/dashboard");
      else navigate(context.destination, { replace: true });
    }).catch((e) => {
      clearSsoCallbackContext();
      setError(e instanceof Error ? e.message : "Team SSO sign-in failed.");
    });
  }, [navigate]);

  return (
    <Card hairline className="mx-auto w-full max-w-lg p-7 text-center">
      <SectionHeader eyebrow="Team sign-in" title={error ? "Sign-in could not be completed" : "Completing SSO"} />
      <p role={error ? "alert" : "status"} className={`mt-4 text-sm ${error ? "text-ember" : "text-ink-3"}`}>
        {error || message}
      </p>
      {error && <a href="/login" className="mt-5 inline-block text-sm font-medium text-ember underline">Return to sign in</a>}
    </Card>
  );
}
