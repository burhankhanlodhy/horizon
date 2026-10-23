/** Stripe billing: hosted Checkout and Customer Portal, plus invoice history. */

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "./api";

export interface SubscriptionInfo {
  plan: "free" | "pro" | "team";
  status: string;
  billing_mode: "stripe" | "stripe_not_configured";
  has_billing_account: boolean;
  has_subscription: boolean;
  cancel_at_period_end: boolean;
  /** When a scheduled cancellation takes effect. */
  cancel_at: string | null;
  current_period_end: string | null;
}

export interface Invoice {
  id: string;
  number: string | null;
  created: string;
  description: string;
  total: number;
  currency: string;
  status: string;
  url: string | null;
}

/** Leaves the dashboard for Stripe Checkout; the plan changes via webhook. */
export async function startCheckout(plan: "pro"): Promise<void> {
  const { url } = await apiFetch<{ url: string }>("/billing/checkout", {
    method: "POST",
    body: { plan },
  });
  window.location.assign(url);
}

/** Undoes a scheduled cancellation; the plan keeps renewing. */
export async function renewSubscription(): Promise<void> {
  await apiFetch("/billing/renew", { method: "POST" });
}

/** Opens the Stripe Customer Portal (card, invoices, cancellation). */
export async function openPortal(): Promise<void> {
  const { url } = await apiFetch<{ url: string }>("/billing/portal", {
    method: "POST",
  });
  window.location.assign(url);
}

export function useBilling(enabled = true) {
  const [info, setInfo] = useState<SubscriptionInfo | null>(null);
  const [invoices, setInvoices] = useState<Invoice[] | null>(null);
  const [error, setError] = useState("");
  const reload = useCallback(async () => {
    if (!enabled) return;
    try {
      const [sub, inv] = await Promise.all([
        apiFetch<SubscriptionInfo>("/subscription"),
        apiFetch<Invoice[]>("/billing/invoices"),
      ]);
      setInfo(sub);
      setInvoices(inv);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to load billing");
    }
  }, [enabled]);
  useEffect(() => {
    void reload();
  }, [reload]);
  return { info, invoices, error, reload };
}
