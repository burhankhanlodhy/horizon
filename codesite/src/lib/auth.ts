/**
 * Session auth against the control plane. Accounts are the same core.users
 * accounts as app.contextshrink.com. The token lives in localStorage under the
 * dashboard's key; the user record is cached so the header can render at once.
 */

import { ApiError, apiFetch, getToken, setToken } from "./api";

export interface SessionUser {
  id: string;
  name: string;
  email: string;
  email_verified?: boolean;
}

const USER_KEY = "cs_session_user";

function cacheUser(user: SessionUser | null): void {
  try {
    if (user) localStorage.setItem(USER_KEY, JSON.stringify(user));
    else localStorage.removeItem(USER_KEY);
  } catch {
    /* storage unavailable */
  }
}

export function getCachedUser(): SessionUser | null {
  try {
    const raw = localStorage.getItem(USER_KEY);
    return raw ? (JSON.parse(raw) as SessionUser) : null;
  } catch {
    return null;
  }
}

/** Cheap check for showing signed-in navigation. The API still decides. */
export function hasSession(): boolean {
  return Boolean(getToken());
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
  cacheUser(out.user);
  return out.user;
}

export async function signIn(email: string, password: string): Promise<SessionUser> {
  const out = await apiFetch<AuthResponse>("/auth/login", {
    method: "POST",
    body: { email, password },
  });
  setToken(out.token);
  cacheUser(out.user);
  return out.user;
}

export async function signOut(): Promise<void> {
  try {
    await apiFetch("/auth/logout", { method: "POST" });
  } catch {
    /* server-side revoke is best-effort; clear locally regardless */
  }
  clearSession();
}

/** Drop the local session without calling the API (used after a 401). */
export function clearSession(): void {
  setToken(null);
  cacheUser(null);
}

/** Re-verify the stored session. Resolves null when there is no valid session. */
export async function refreshUser(): Promise<SessionUser | null> {
  if (!getToken()) {
    cacheUser(null);
    return null;
  }
  try {
    const user = await apiFetch<SessionUser>("/auth/me");
    cacheUser(user);
    return user;
  } catch (error) {
    if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
      clearSession();
      return null;
    }
    throw error;
  }
}

/**
 * Where to go after sign-in or sign-up. Only same-site relative paths are
 * accepted, so ?next= cannot send people to another origin.
 */
export function safeNext(raw: string | null): string {
  if (!raw) return "/account";
  if (!raw.startsWith("/") || raw.startsWith("//") || raw.startsWith("/\\")) {
    return "/account";
  }
  return raw;
}
