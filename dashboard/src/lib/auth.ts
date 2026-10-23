/**
 * Session auth against the control-plane API. The token lives in
 * localStorage; the user record is cached alongside it so the shell renders
 * instantly and /auth/me can re-verify when needed.
 */

import { ApiError, apiFetch, getToken, setToken } from "./api";
import { setSelectedTeamId } from "./workspace";

export interface SessionUser {
  id: string;
  name: string;
  email: string;
  created_at?: string;
  plan?: "free" | "pro" | "team";
  subscription_status?: string;
  /** Can read Contact form messages (ADMIN_EMAILS on the API). */
  is_admin?: boolean;
  email_verified?: boolean;
  /** True while unconfirmed accounts may not create proxy keys. */
  email_verification_required?: boolean;
  /** Team enforced by an SSO session; null means the session is not Team SSO. */
  sso_team_id?: string | null;
}

const USER_KEY = "cs_session_user";
const SSO_VERIFIER_KEY = "cs_sso_verifier";
const SSO_MODE_KEY = "cs_sso_link_mode";
const SSO_DESTINATION_KEY = "cs_sso_destination";
const SSO_SESSION_TEAM_KEY = "cs_sso_session_team";

function safeLocalPath(value: unknown): string {
  if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//")) return "/";
  try {
    const url = new URL(value, window.location.origin);
    return url.origin === window.location.origin ? `${url.pathname}${url.search}${url.hash}` : "/";
  } catch {
    return "/";
  }
}

export function postAuthPath(state: unknown, next?: string | null): string {
  if (next === "/dashboard") return "/dashboard";
  const destination = getPostAuthDestination(state);
  return safeLocalPath(typeof destination === "string"
    ? (destination === "/" && next ? next : destination)
    : `${destination.pathname}${destination.search}${destination.hash}`);
}

function createVerifier(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

/** Start a Team SSO login or identity-link flow and retain its PKCE verifier. */
export async function beginSso(teamId: string, link: boolean, destination = "/"): Promise<void> {
  const verifier = createVerifier();
  try {
    sessionStorage.setItem(SSO_VERIFIER_KEY, verifier);
    sessionStorage.setItem(SSO_MODE_KEY, link ? "link" : "login");
    sessionStorage.setItem(SSO_DESTINATION_KEY, safeLocalPath(destination));
    const { authorization_url } = await apiFetch<{ authorization_url: string }>("/auth/sso/start", {
      method: "POST",
      body: { team_id: teamId.trim(), verifier, link },
    });
    window.location.assign(authorization_url);
  } catch (error) {
    clearSsoCallbackContext();
    throw error;
  }
}

export interface SsoExchangeResult {
  token?: string;
  user?: SessionUser;
  team_id: string;
  linked: boolean;
}

export function getSsoCallbackContext(): { mode: "link" | "login"; destination: string } | null {
  try {
    if (!sessionStorage.getItem(SSO_VERIFIER_KEY)) return null;
    return {
      mode: sessionStorage.getItem(SSO_MODE_KEY) === "link" ? "link" : "login",
      destination: safeLocalPath(sessionStorage.getItem(SSO_DESTINATION_KEY)),
    };
  } catch {
    return null;
  }
}

export async function exchangeSsoCode(code: string): Promise<SsoExchangeResult> {
  let verifier: string | null;
  try { verifier = sessionStorage.getItem(SSO_VERIFIER_KEY); }
  catch { throw new Error("This browser could not access the temporary SSO session. Start again from your Team’s sign-in page."); }
  if (!verifier) throw new Error("This SSO sign-in expired. Start again from your Team’s sign-in page.");
  const result = await apiFetch<SsoExchangeResult>("/auth/sso/exchange", {
    method: "POST",
    body: { code, verifier },
  });
  sessionStorage.removeItem(SSO_VERIFIER_KEY);
  return result;
}

export function clearSsoCallbackContext(): void {
  try {
    sessionStorage.removeItem(SSO_MODE_KEY);
    sessionStorage.removeItem(SSO_DESTINATION_KEY);
    sessionStorage.removeItem(SSO_VERIFIER_KEY);
  } catch {
    /* callback state is temporary and may be unavailable in restricted browsers */
  }
}

export function completeSsoLogin(result: SsoExchangeResult): void {
  if (!result.token || !result.user) throw new Error("The identity provider did not return a valid dashboard session.");
  setToken(result.token);
  const user = { ...result.user, sso_team_id: result.team_id };
  cacheUser(user);
  syncSsoSession(result.team_id);
}

function clearSsoSessionMarker(): void {
  try { localStorage.removeItem(SSO_SESSION_TEAM_KEY); } catch { /* storage may be unavailable */ }
}

export function getSsoSessionTeamId(): string | null {
  try { return localStorage.getItem(SSO_SESSION_TEAM_KEY); } catch { return null; }
}

function syncSsoSession(teamId: string | null | undefined): void {
  try {
    if (teamId) localStorage.setItem(SSO_SESSION_TEAM_KEY, teamId);
    else localStorage.removeItem(SSO_SESSION_TEAM_KEY);
  } catch { /* server still enforces the SSO Team context */ }
  if (teamId) setSelectedTeamId(teamId);
}

function cacheUser(user: SessionUser | null): void {
  try {
    if (user) localStorage.setItem(USER_KEY, JSON.stringify(user));
    else localStorage.removeItem(USER_KEY);
  } catch {
    /* storage unavailable */
  }
}

export function getUser(): SessionUser | null {
  try {
    const raw = localStorage.getItem(USER_KEY);
    return raw ? (JSON.parse(raw) as SessionUser) : null;
  } catch {
    return null;
  }
}

interface AuthResponse {
  token: string;
  user: SessionUser;
}

export async function signUp(
  name: string,
  email: string,
  password: string,
  turnstileToken?: string | null,
): Promise<SessionUser> {
  const out = await apiFetch<AuthResponse>("/auth/signup", {
    method: "POST",
    body: { name, email, password, turnstile_token: turnstileToken ?? undefined },
  });
  setToken(out.token);
  clearSsoSessionMarker();
  cacheUser(out.user);
  return out.user;
}

export async function signIn(
  email: string,
  password: string,
): Promise<SessionUser> {
  const out = await apiFetch<AuthResponse>("/auth/login", {
    method: "POST",
    body: { email, password },
  });
  setToken(out.token);
  clearSsoSessionMarker();
  cacheUser(out.user);
  return out.user;
}

export async function logout(): Promise<void> {
  try {
    await apiFetch("/auth/logout", { method: "POST" });
  } catch {
    /* server-side revoke is best-effort; clear locally regardless */
  }
  setToken(null);
  clearSsoSessionMarker();
  cacheUser(null);
}

/** Re-verify the cached session against the API (e.g. on app mount). */
export async function refreshUser(): Promise<SessionUser | null> {
  if (!getToken()) {
    clearSsoSessionMarker();
    cacheUser(null);
    return null;
  }

  try {
    const user = await apiFetch<SessionUser>("/auth/me");
    cacheUser(user);
    syncSsoSession(user.sso_team_id);
    return user;
  } catch (error) {
    if (
      error instanceof ApiError &&
      (error.status === 401 || error.status === 403)
    ) {
      setToken(null);
      clearSsoSessionMarker();
      cacheUser(null);
      return null;
    }
    throw error;
  }
}

/** Keep the originally requested dashboard route through login or signup. */
export function getPostAuthDestination(state: unknown):
  | string
  | {
      pathname: string;
      search: string;
      hash: string;
    } {
  const from = (
    state as {
      from?: { pathname?: string; search?: string; hash?: string };
    } | null
  )?.from;

  if (
    !from?.pathname ||
    !from.pathname.startsWith("/") ||
    from.pathname.startsWith("//")
  ) {
    return "/";
  }

  return {
    pathname: from.pathname,
    search: from.search ?? "",
    hash: from.hash ?? "",
  };
}
