/**
 * Session auth against the control-plane API. The token lives in
 * localStorage; the user record is cached alongside it so the shell renders
 * instantly and /auth/me can re-verify when needed.
 */

import { ApiError, apiFetch, getToken, setToken } from "./api";

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
  inviteCode?: string,
): Promise<SessionUser> {
  const out = await apiFetch<AuthResponse>("/auth/signup", {
    method: "POST",
    body: {
      name,
      email,
      password,
      turnstile_token: turnstileToken ?? undefined,
      invite_code: inviteCode || undefined,
    },
  });
  setToken(out.token);
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
  cacheUser(null);
}

/** Re-verify the cached session against the API (e.g. on app mount). */
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
    if (
      error instanceof ApiError &&
      (error.status === 401 || error.status === 403)
    ) {
      setToken(null);
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
