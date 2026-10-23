/** ContextShrink Code API: device approval, account, billing and usage counts. */

import { apiFetch } from "./api";

export type Tool = "claude" | "codex";
export type PlanState = "trial" | "active" | "past_due" | "canceled" | "none";

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

export interface Plan {
  state: PlanState;
  trial_ends_at: string | null;
  current_period_end: string | null;
  cancel_at_period_end: boolean;
  entitled: boolean;
}

/** GET /code/account */
export interface Account {
  plan: Plan;
  devices: Device[];
  device_limit: number;
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

export function getAccount(): Promise<Account> {
  return apiFetch<Account>("/code/account");
}

export function disconnectDevice(id: string): Promise<void> {
  return apiFetch<void>(`/code/devices/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export function getUsageSummary(): Promise<UsageSummary> {
  return apiFetch<UsageSummary>("/code/usage/summary");
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
