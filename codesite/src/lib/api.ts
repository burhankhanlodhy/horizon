/** Control-plane API client. Same session token and Bearer scheme as the dashboard. */

export const API_URL: string =
  (import.meta.env.VITE_API_URL as string | undefined)?.trim() ||
  (import.meta.env.DEV ? "http://127.0.0.1:8788" : "https://api.contextshrink.com");

/** Marketing site, used for the legal pages that live there. */
export const LANDING_URL: string =
  (import.meta.env.VITE_LANDING_URL as string | undefined)?.trim() ||
  "https://contextshrink.com";

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
    /* storage unavailable (private window, blocked site data) */
  }
}

export class ApiError extends Error {
  status: number;
  /** The raw `detail` from the API body, for structured errors such as 409 device_limit. */
  detail: unknown;
  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.status = status;
    this.detail = detail;
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
      "Cannot reach ContextShrink right now. Check your connection and try again.",
    );
  }

  if (!res.ok) {
    let message = `Request failed (${res.status})`;
    let detail: unknown = undefined;
    try {
      const body = (await res.json()) as { detail?: unknown };
      detail = body?.detail;
      if (typeof detail === "string") message = detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, message, detail);
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** True when the API says the session is missing or expired. */
export function isUnauthorized(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}
