/** Control-plane API client. Point VITE_API_URL at the api container. */
import { getSelectedTeamId } from "./workspace";

export const API_URL: string =
  (import.meta.env.VITE_API_URL as string | undefined) ??
  (import.meta.env.DEV ? "http://127.0.0.1:8788" : "https://api.contextshrink.com");

/** Public proxy base URL users configure in their tools. */
export const PROXY_URL: string =
  (import.meta.env.VITE_PROXY_URL as string | undefined) ??
  "https://proxy.contextshrink.com";

const TOKEN_KEY = "cs_session_token";

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable */
  }
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function apiFetch<T>(
  path: string,
  opts: { method?: string; body?: unknown; signal?: AbortSignal } = {},
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  // Auth, team discovery, and invitation acceptance are deliberately unscoped.
  // Team routes carry an explicit team ID and authorize it server-side.
  const isUnscoped =
    (path.startsWith("/auth/") && path !== "/auth/me") ||
    path === "/teams" ||
    path.startsWith("/teams/") ||
    path.startsWith("/team-invitations/");
  const teamId = isUnscoped ? null : getSelectedTeamId();
  if (teamId) headers["X-ContextShrink-Team"] = teamId;

  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, {
      method: opts.method ?? "GET",
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      signal: opts.signal,
      cache: "no-store",
    });
  } catch {
    throw new ApiError(
      0,
      "Cannot reach the ContextShrink API. Is the control plane running?",
    );
  }

  if (!res.ok) {
    let message = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") message = body.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, message);
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}
