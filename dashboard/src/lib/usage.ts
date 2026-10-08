import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, apiFetch } from "./api";

interface LedgerTotals {
  requests: number;
  tokens_saved: number;
  savings_usd: number;
  tokens_before: number;
  tokens_after: number;
  /** Cache keep-alive: pings sent, what they cost, and the rewrites they avoided. */
  keepalive_pings?: number;
  keepalive_spend_usd?: number;
  keepalive_resumes?: number;
  keepalive_avoided_usd?: number;
}
interface UsageResponse {
  days: number;
  as_of: string;
  start: string;
  totals: LedgerTotals;
  previous: LedgerTotals;
  series: (LedgerTotals & { date: string })[];
  models: { name: string; requests: number }[];
  providers: { name: string }[];
  sessions: (LedgerTotals & {
    id: string;
    agent: string;
    minutes: number;
    started_at: string;
    last_activity: string;
  })[];
}
export interface BillingEstimate {
  plan: "free" | "pro" | "team";
  period_start: string;
  period_end: string;
  period_source: "subscription" | "calendar_month";
  estimated_savings_usd: number;
  savings_fee_threshold_usd: number;
  savings_fee_rate: number;
  seat_count: number;
  estimates: Record<"free" | "pro" | "team", {
    seat_fee: number;
    savings_fee: number;
    total: number;
  }>;
  estimated_total_usd: number;
  currency: "USD";
  compression: CompressionEntitlement;
  payment_issue: PaymentIssue | null;
}

/** Oldest savings-fee invoice whose payment failed and is still unpaid. */
export interface PaymentIssue {
  amount_usd: number;
  invoice_url: string | null;
  failed_at: string;
  /** Pro features pause at this time if still unpaid. */
  pause_at: string;
  paused: boolean;
}

/** Free-plan compression allowance for the current UTC month. */
export interface CompressionEntitlement {
  plan: "free" | "pro" | "team";
  capped: boolean;
  cap_usd: number;
  cycle_savings_usd: number | null;
  cycle_start: string;
  cycle_end: string;
  compression_allowed: boolean;
}

export function useBillingEstimate() {
  const [estimate, setEstimate] = useState<BillingEstimate | null>(null);
  const [error, setError] = useState("");
  const navigate = useNavigate();
  useEffect(() => {
    const controller = new AbortController();
    let pending = false;
    async function load() {
      if (pending) return;
      pending = true;
      try {
        const data = await apiFetch<BillingEstimate>("/billing/estimate", {
          signal: controller.signal,
        });
        if (!controller.signal.aborted) {
          setEstimate(data);
          setError("");
        }
      } catch (e) {
        if (controller.signal.aborted) return;
        if (e instanceof ApiError && e.status === 401)
          navigate("/login", { replace: true });
        else setError(e instanceof Error ? e.message : "Unable to load billing estimate");
      } finally {
        pending = false;
      }
    }
    void load();
    const timer = window.setInterval(() => {
      if (!document.hidden) void load();
    }, 30000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [navigate]);
  return { estimate, error, loading: !estimate && !error };
}
const colors = ["#c14d1b", "#5f7452", "#8b7f6f", "#4a4137"];
const mapped = (r?: LedgerTotals) => ({
  requests: Number(r?.requests ?? 0),
  tokensSaved: Number(r?.tokens_saved ?? 0),
  savingsUsd: Number(r?.savings_usd ?? 0),
  tokensBefore: Number(r?.tokens_before ?? 0),
  tokensAfter: Number(r?.tokens_after ?? 0),
  keepalivePings: Number(r?.keepalive_pings ?? 0),
  keepaliveSpendUsd: Number(r?.keepalive_spend_usd ?? 0),
  keepaliveResumes: Number(r?.keepalive_resumes ?? 0),
  keepaliveAvoidedUsd: Number(r?.keepalive_avoided_usd ?? 0),
});
export const pct = (current: number, previous: number) =>
  previous > 0 ? ((current - previous) / previous) * 100 : undefined;
export const compression = (t: {
  tokensBefore: number;
  tokensAfter: number;
}) =>
  t.tokensBefore > 0
    ? Math.max(0, ((t.tokensBefore - t.tokensAfter) / t.tokensBefore) * 100)
    : 0;

export function useUsage(days: number) {
  const [response, setResponse] = useState<UsageResponse | null>(null);
  const [error, setError] = useState("");
  const navigate = useNavigate();
  useEffect(() => {
    const controller = new AbortController();
    let pending = false;
    setResponse(null);
    setError("");
    async function load() {
      if (pending) return;
      pending = true;
      try {
        const data = await apiFetch<UsageResponse>(
          `/usage/summary?days=${days}`,
          { signal: controller.signal },
        );
        if (!controller.signal.aborted) {
          setResponse(data);
          setError("");
        }
      } catch (e) {
        if (controller.signal.aborted) return;
        if (e instanceof ApiError && e.status === 401)
          navigate("/login", { replace: true });
        else
          setError(
            e instanceof Error ? e.message : "Unable to load account usage",
          );
      } finally {
        pending = false;
      }
    }
    void load();
    const timer = window.setInterval(() => {
      if (!document.hidden) void load();
    }, 10000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [days, navigate]);
  const data = response?.days === days ? response : null;
  return useMemo(() => {
    const t = mapped(data?.totals);
    const series = data
      ? Array.from({ length: days }, (_, i) => {
          const date = new Date(data.start);
          date.setUTCDate(date.getUTCDate() + i);
          const key = date.toISOString().slice(0, 10);
          return {
            date: key,
            ...mapped(data.series.find((row) => row.date === key)),
          };
        })
      : [];
    return {
      t,
      previous: mapped(data?.previous),
      series,
      pieData: (data?.models ?? []).map((m, i) => ({
        name: m.name,
        share: t.requests ? Number(m.requests) / t.requests : 0,
        color: colors[i % colors.length],
      })),
      providers: (data?.providers ?? []).map((p, i) => ({
        name: p.name,
        color: colors[i % colors.length],
      })),
      sessions: (data?.sessions ?? []).map((s) => ({
        id: s.id,
        agent: s.agent,
        requests: Number(s.requests),
        tokensSaved: Number(s.tokens_saved),
        minutes: Math.round(Number(s.minutes)),
        active: false,
      })),
      error,
      loaded: Boolean(data),
      loading: !data && !error,
    };
  }, [data, days, error]);
}
