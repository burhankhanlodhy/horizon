/** ContextShrink Code API: device approval, account, billing and usage counts. */

import { apiFetch } from "./api";

export type Tool = "claude" | "codex";

/**
 * Plan state from GET /code/account. "free" means there is no live paid
 * subscription, whatever the free week is doing.
 */
export type PlanState = "free" | "active" | "past_due" | "canceled";

export type FreeWeekStatus = "available" | "active" | "used";

export interface Device {
  id: string;
  name: string;
  platform: string;
  tools?: string[];
  created_at?: string;
  last_seen_at: string | null;
}

/** What the browser is approving: GET /code/cli/request. */
export interface CliRequest {
  device_name: string;
  platform: string;
  tool: string;
  device_limit: {
    reached: boolean;
    devices: Device[];
  };
}

export interface FreeWeek {
  status: FreeWeekStatus;
  started_at: string | null;
  ends_at: string | null;
  /** When the next free week can start (next UTC month). */
  renews_at: string | null;
}

/**
 * The plan object, normalised. `free_week` is null when the API did not send
 * one (an older deployment), which the UI shows as "status unavailable".
 */
export interface Plan {
  state: PlanState;
  free_week: FreeWeek | null;
  current_period_end: string | null;
  cancel_at_period_end: boolean;
  entitled: boolean;
  price_usd: number;
}

/** GET /code/account */
export interface Account {
  plan: Plan;
  devices: Device[];
  device_limit: number;
}

/** One row of GET /code/invoices. Only the code subscription's invoices. */
export interface Invoice {
  id: string;
  number: string | null;
  /** Unix seconds (as Stripe sends them) or an ISO string. */
  created: number | string;
  /** Amount in the currency's minor units (cents for usd). */
  total: number;
  currency: string;
  status: string | null;
  url: string | null;
}

export interface UsageWindow {
  requests: number;
  tokens_before: number;
  tokens_after: number;
  tokens_saved: number;
  pct: number;
}

/** GET /code/usage/summary */
export interface UsageSummary {
  today: UsageWindow;
  last_7d: UsageWindow;
  last_30d: UsageWindow;
}

/** Shape of the 409 `detail` returned when a third device tries to connect. */
export interface DeviceLimitDetail {
  code: "device_limit";
  devices: Device[];
}

export function getCliRequest(code: string): Promise<CliRequest> {
  return apiFetch<CliRequest>(`/code/cli/request?code=${encodeURIComponent(code)}`);
}

export function approveCli(userCode: string): Promise<unknown> {
  return apiFetch<unknown>("/code/cli/approve", {
    method: "POST",
    body: { user_code: userCode },
  });
}

export async function getAccount(): Promise<Account> {
  const raw = await apiFetch<Omit<Account, "plan"> & { plan: unknown }>("/code/account");
  return { ...raw, plan: normalisePlan(raw.plan) };
}

/**
 * Accepts any plan object the API has sent. Older responses used the one-time
 * trial states ("trial", "none") and had no free_week. Those become "free"
 * with free_week null, so the page renders instead of crashing.
 */
export function normalisePlan(raw: unknown): Plan {
  const p = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const state: PlanState =
    p.state === "active" || p.state === "past_due" || p.state === "canceled" ? p.state : "free";
  return {
    state,
    free_week: normaliseFreeWeek(p.free_week),
    current_period_end: typeof p.current_period_end === "string" ? p.current_period_end : null,
    cancel_at_period_end: p.cancel_at_period_end === true,
    entitled: p.entitled === true,
    price_usd: typeof p.price_usd === "number" ? p.price_usd : 5,
  };
}

function normaliseFreeWeek(raw: unknown): FreeWeek | null {
  if (!raw || typeof raw !== "object") return null;
  const f = raw as Record<string, unknown>;
  if (f.status !== "available" && f.status !== "active" && f.status !== "used") return null;
  return {
    status: f.status,
    started_at: typeof f.started_at === "string" ? f.started_at : null,
    ends_at: typeof f.ends_at === "string" ? f.ends_at : null,
    renews_at: typeof f.renews_at === "string" ? f.renews_at : null,
  };
}

export function disconnectDevice(id: string): Promise<void> {
  return apiFetch<void>(`/code/devices/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export function getUsageSummary(): Promise<UsageSummary> {
  return apiFetch<UsageSummary>("/code/usage/summary");
}

/**
 * Cancels the subscription at the end of the paid period. The caller reloads
 * the account afterwards, so the response body is not needed.
 */
export function cancelSubscription(): Promise<unknown> {
  return apiFetch<unknown>("/code/subscription/cancel", { method: "POST" });
}

/** Undoes a pending cancellation. */
export function resumeSubscription(): Promise<unknown> {
  return apiFetch<unknown>("/code/subscription/resume", { method: "POST" });
}

/**
 * Invoice history for the code subscription, newest first. The API may send a
 * bare array or an object with an `invoices` array; both are accepted.
 */
export async function getInvoices(): Promise<Invoice[]> {
  const raw = await apiFetch<Invoice[] | { invoices?: Invoice[] }>("/code/invoices");
  const list = Array.isArray(raw) ? raw : Array.isArray(raw?.invoices) ? raw.invoices : [];
  return list;
}

/** Starts Stripe Checkout for the $5/month plan, then leaves the site. */
export async function startCheckout(): Promise<void> {
  const { url } = await apiFetch<{ url: string }>("/code/checkout", { method: "POST" });
  window.location.assign(url);
}

/** Opens the Stripe billing portal (card, invoices, cancellation). */
export async function openBillingPortal(): Promise<void> {
  const { url } = await apiFetch<{ url: string }>("/code/portal", { method: "POST" });
  window.location.assign(url);
}

/** Pulls a DeviceLimitDetail out of an ApiError's detail, or null. */
export function asDeviceLimit(detail: unknown): DeviceLimitDetail | null {
  if (
    detail &&
    typeof detail === "object" &&
    (detail as { code?: unknown }).code === "device_limit" &&
    Array.isArray((detail as { devices?: unknown }).devices)
  ) {
    return detail as DeviceLimitDetail;
  }
  return null;
}

/**
 * Normalises a user code typed or pasted by hand to the ABCD-EFGH form the
 * API expects. Returns null when it cannot be a code.
 */
export function normaliseUserCode(raw: string): string | null {
  const compact = raw.trim().toUpperCase().replace(/[\s-]/g, "");
  if (!/^[A-Z0-9]{8}$/.test(compact)) return null;
  return `${compact.slice(0, 4)}-${compact.slice(4)}`;
}
