import { useCallback, useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { RequireAuth } from "../components/RequireAuth";
import { SiteFooter, SiteHeader } from "../components/SiteChrome";
import { Banner, Button, Card, CodeCommand, Kicker, Spinner, cx } from "../components/ui";
import { ApiError, isUnauthorized } from "../lib/api";
import { signOut, type SessionUser } from "../lib/auth";
import {
  cancelSubscription,
  disconnectDevice,
  getAccount,
  getInvoices,
  getUsageSummary,
  openBillingPortal,
  resumeSubscription,
  startCheckout,
  type Account as AccountInfo,
  type Device,
  type FreeWeek,
  type Invoice,
  type Plan,
  type UsageSummary,
  type UsageWindow,
} from "../lib/code";
import { DEVICE_LIMIT, INSTALL_COMMANDS, PRICE_MONTHLY_USD } from "../lib/constants";
import {
  daysUntil,
  fmtCount,
  fmtDate,
  fmtFullCount,
  fmtMoney,
  fmtPct,
  fmtRelative,
  platformLabel,
  toIso,
  toolLabel,
} from "../lib/format";
import { useNavigateToLogin } from "../lib/useNavigateToLogin";

export default function Account() {
  return (
    <div className="flex min-h-screen flex-col">
      <SiteHeader />
      <main className="flex-1 px-4 pb-16 pt-8 sm:px-6 sm:pt-12">
        <div className="mx-auto max-w-4xl">
          <RequireAuth>{(user) => <AccountView user={user} />}</RequireAuth>
        </div>
      </main>
      <SiteFooter />
    </div>
  );
}

function AccountView({ user }: { user: SessionUser }) {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const checkout = params.get("checkout");
  const goToLogin = useNavigateToLogin("/account");

  const [account, setAccount] = useState<AccountInfo | null>(null);
  const [accountError, setAccountError] = useState<string | null>(null);
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [usageError, setUsageError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<null | "checkout" | "portal" | "signout" | string>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [invoices, setInvoices] = useState<Invoice[] | null>(null);
  const [invoicesError, setInvoicesError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    const [acc, use, inv] = await Promise.allSettled([getAccount(), getUsageSummary(), getInvoices()]);
    setNow(Date.now());
    if (acc.status === "fulfilled") {
      setAccount(acc.value);
      setAccountError(null);
    } else {
      if (isUnauthorized(acc.reason)) return goToLogin();
      setAccountError(messageOf(acc.reason, "Could not load your account."));
    }
    if (use.status === "fulfilled") {
      setUsage(use.value);
      setUsageError(null);
    } else {
      if (isUnauthorized(use.reason)) return goToLogin();
      setUsageError(messageOf(use.reason, "Could not load usage."));
    }
    if (inv.status === "fulfilled") {
      setInvoices(inv.value);
      setInvoicesError(null);
    } else {
      if (isUnauthorized(inv.reason)) return goToLogin();
      setInvoicesError(messageOf(inv.reason, "Could not load invoices."));
    }
    setLoading(false);
  }, [goToLogin]);

  useEffect(() => {
    void load();
  }, [load]);

  const subscribe = async () => {
    setBusy("checkout");
    setActionError(null);
    try {
      await startCheckout();
    } catch (e) {
      if (isUnauthorized(e)) return goToLogin();
      setActionError(messageOf(e, "Could not open checkout. Try again."));
      setBusy(null);
    }
  };

  const openPortal = async () => {
    setBusy("portal");
    setActionError(null);
    try {
      await openBillingPortal();
    } catch (e) {
      if (isUnauthorized(e)) return goToLogin();
      setActionError(messageOf(e, "Could not open billing. Try again."));
      setBusy(null);
    }
  };

  const cancelSub = async (): Promise<void> => {
    setBusy("cancel");
    setActionError(null);
    setNotice(null);
    const end = fmtDate(account?.plan.current_period_end);
    try {
      await cancelSubscription();
      await load();
      setNotice(`Subscription cancelled. It stays active until ${end || "the end of this billing period"}.`);
    } catch (e) {
      if (isUnauthorized(e)) return goToLogin();
      setActionError(messageOf(e, "Could not cancel your subscription. Try again."));
    } finally {
      setBusy(null);
    }
  };

  const resumeSub = async () => {
    setBusy("resume");
    setActionError(null);
    setNotice(null);
    const end = fmtDate(account?.plan.current_period_end);
    try {
      await resumeSubscription();
      await load();
      setNotice(`Subscription resumed. It renews ${end || "at the end of this billing period"}.`);
    } catch (e) {
      if (isUnauthorized(e)) return goToLogin();
      setActionError(messageOf(e, "Could not resume your subscription. Try again."));
    } finally {
      setBusy(null);
    }
  };

  const disconnect = async (device: Device) => {
    if (!window.confirm(`Disconnect ${device.name}? It stops compression on that computer.`)) {
      return;
    }
    setBusy(`device:${device.id}`);
    setActionError(null);
    try {
      await disconnectDevice(device.id);
      await load();
    } catch (e) {
      if (isUnauthorized(e)) return goToLogin();
      setActionError(messageOf(e, "Could not disconnect that computer."));
    } finally {
      setBusy(null);
    }
  };

  const logOut = async () => {
    setBusy("signout");
    await signOut();
    navigate("/", { replace: true });
  };

  const dismissCheckout = () => {
    const next = new URLSearchParams(params);
    next.delete("checkout");
    setParams(next, { replace: true });
  };

  return (
    <div className="flex flex-col gap-8">
      <header className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0">
          <Kicker>Your account</Kicker>
          <h1 className="mt-3 font-display text-3xl font-bold text-ink sm:text-4xl">Plan and computers</h1>
          <p className="mt-2 truncate text-sm text-ink-3">Signed in as {user.email}</p>
        </div>
        <div className="flex gap-2">
          <Button variant="ghost" onClick={() => void load()} disabled={loading} className="!py-2 text-[12px]">
            {loading ? "Refreshing…" : "Refresh"}
          </Button>
          <Button
            variant="ghost"
            onClick={() => void logOut()}
            disabled={busy === "signout"}
            className="!py-2 text-[12px]"
          >
            Sign out
          </Button>
        </div>
      </header>

      {checkout === "success" && (
        <Banner tone="success" className="flex items-start justify-between gap-3">
          <span>
            Thanks. Your subscription is starting. It can take a few seconds to show here. Refresh if
            the plan has not changed yet.
          </span>
          <DismissButton onClick={dismissCheckout} />
        </Banner>
      )}
      {checkout === "cancelled" && (
        <Banner tone="info" className="flex items-start justify-between gap-3">
          <span>Checkout was cancelled. Nothing was charged.</span>
          <DismissButton onClick={dismissCheckout} />
        </Banner>
      )}

      {notice && (
        <Banner tone="success" className="flex items-start justify-between gap-3">
          <span>{notice}</span>
          <DismissButton onClick={() => setNotice(null)} />
        </Banner>
      )}

      {actionError && <Banner tone="error">{actionError}</Banner>}

      {accountError && !account ? (
        <Banner tone="error">{accountError}</Banner>
      ) : account ? (
        <BillingCard
          plan={account.plan}
          now={now}
          busy={busy}
          onUpgrade={() => void subscribe()}
          onPortal={() => void openPortal()}
          onCancel={cancelSub}
          onResume={() => void resumeSub()}
        />
      ) : (
        <Spinner label="Loading your plan" />
      )}

      <InvoicesCard invoices={invoices} error={invoicesError} />

      <UsageCard usage={usage} error={usageError} />

      {account && (
        <DevicesCard
          account={account}
          busy={busy}
          onDisconnect={(d) => void disconnect(d)}
        />
      )}

      <Card>
        <h2 className="font-display text-lg font-semibold text-ink">Add another computer</h2>
        <p className="mt-1 text-sm leading-relaxed text-ink-3">
          Run one of these in a terminal on the computer you want to connect. It opens this site to approve
          the device. Windows only for now.
        </p>
        <div className="mt-5 grid gap-4 md:grid-cols-2">
          {INSTALL_COMMANDS.map((c) => (
            <CodeCommand key={c.id} label={c.label} command={c.command} />
          ))}
        </div>
      </Card>
    </div>
  );
}

function messageOf(e: unknown, fallback: string): string {
  if (e instanceof ApiError || e instanceof Error) return e.message || fallback;
  return fallback;
}

function DismissButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label="Dismiss message"
      className="shrink-0 text-lg leading-none text-ink-3 hover:text-ink"
    >
      ×
    </button>
  );
}

/* --------------------------------------------------------------- billing */

/** The free-week status line. Null means the API did not send free_week. */
function freeWeekText(fw: FreeWeek | null, now: number): string {
  if (!fw) return "Free week status is not available right now. Refresh to check again.";
  if (fw.status === "active") {
    const left = daysUntil(fw.ends_at, now);
    const lead =
      left === null || left === 0
        ? "Free week active."
        : `Free week active: ${left} ${left === 1 ? "day" : "days"} left.`;
    const ends = fw.ends_at ? ` It ends ${fmtDate(fw.ends_at)}.` : "";
    return `${lead}${ends} Then compression pauses until next month.`;
  }
  if (fw.status === "available") {
    return "Free week available: starts the first time you use ContextShrink this month.";
  }
  return `This month's free week is used. Next one from ${fmtDate(fw.renews_at, true) || "next month"}.`;
}

function BillingCard({
  plan,
  now,
  busy,
  onUpgrade,
  onPortal,
  onCancel,
  onResume,
}: {
  plan: Plan;
  now: number;
  busy: string | null;
  onUpgrade: () => void;
  onPortal: () => void;
  onCancel: () => Promise<void>;
  onResume: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const price = plan.price_usd || PRICE_MONTHLY_USD;
  const periodEnd = fmtDate(plan.current_period_end);
  const subscribed = plan.state === "active";
  const pending = subscribed && plan.cancel_at_period_end;
  const freeTier = plan.state === "free" || plan.state === "canceled";

  let title: string;
  let body: string;
  if (pending) {
    title = "Pro, cancelling";
    body = periodEnd
      ? `Cancels on ${periodEnd}. Compression stays on until then.`
      : "Cancels at the end of this billing period. Compression stays on until then.";
  } else if (subscribed) {
    title = "Pro";
    body = periodEnd ? `$${price}/month, next charge ${periodEnd}.` : `$${price}/month.`;
  } else if (plan.state === "past_due") {
    title = "Payment failed";
    body = "Your last payment did not go through. Update your card to keep compression on.";
  } else if (plan.state === "canceled") {
    title = "Subscription ended";
    body = `Compression is off unless your free week is active. Upgrade to $${price}/month to turn it back on.`;
  } else {
    title = "Free plan";
    body = `Free: one week every month. Pro: $${price}/month for unlimited use.`;
  }

  const confirmCancel = async () => {
    await onCancel();
    setConfirming(false);
  };

  return (
    <Card labelledBy="billing-title">
      <div className="flex flex-col gap-6 md:flex-row md:items-start md:justify-between">
        <div className="min-w-0 max-w-xl">
          <div className="flex flex-wrap items-center gap-3">
            <h2 id="billing-title" className="font-display text-2xl font-semibold text-ink">
              {title}
            </h2>
            <span
              className={cx(
                "rounded-[2px] border px-2 py-0.5 font-mono text-[11px] uppercase tracking-[0.12em]",
                plan.entitled ? "border-sage text-sage" : "border-line-2 text-ink-3",
              )}
            >
              {plan.entitled ? "Compression on" : "Compression off"}
            </span>
          </div>
          <p className="mt-3 text-[15px] leading-relaxed text-ink-2">{body}</p>

          {freeTier && (
            <div className="paper-panel mt-5 rounded-[2px] p-4">
              <div className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-3">Free week</div>
              <p className="mt-2 text-sm leading-relaxed text-ink-2">{freeWeekText(plan.free_week, now)}</p>
            </div>
          )}
        </div>

        <div className="flex w-full flex-col gap-2.5 md:w-auto md:min-w-[14rem]">
          {freeTier && (
            <Button onClick={onUpgrade} disabled={busy !== null} className="w-full">
              {busy === "checkout" ? "Opening checkout…" : `Upgrade to $${price}/month`}
            </Button>
          )}
          {subscribed && !pending && (
            <Button
              variant="ghost"
              onClick={() => setConfirming(true)}
              disabled={busy !== null || confirming}
              className="w-full"
            >
              Cancel subscription
            </Button>
          )}
          {pending && (
            <Button onClick={onResume} disabled={busy !== null} className="w-full">
              {busy === "resume" ? "Resuming…" : "Resume subscription"}
            </Button>
          )}
          <Button
            variant={plan.state === "past_due" ? "ink" : "ghost"}
            onClick={onPortal}
            disabled={busy !== null}
            className="w-full"
          >
            {busy === "portal" ? "Opening billing…" : "Update card"}
          </Button>
        </div>
      </div>

      {confirming && subscribed && !pending && (
        <div
          role="group"
          aria-label="Confirm cancellation"
          className="mt-6 flex flex-col gap-4 rounded-[2px] border border-ember bg-ember-soft p-4 sm:p-5"
        >
          <p className="text-sm leading-relaxed text-ink">
            Cancel your subscription?{" "}
            <span className="font-medium">
              {periodEnd ? `Stays active until ${periodEnd}.` : "Stays active until the end of this billing period."}
            </span>{" "}
            You are not charged again after that. Claude Code and Codex keep working either way.
          </p>
          <div className="flex flex-col gap-2.5 sm:flex-row">
            <Button variant="ghost" onClick={() => setConfirming(false)} disabled={busy !== null} className="sm:w-auto">
              Keep subscription
            </Button>
            <Button variant="danger" onClick={() => void confirmCancel()} disabled={busy !== null} className="sm:w-auto">
              {busy === "cancel" ? "Cancelling…" : "Yes, cancel subscription"}
            </Button>
          </div>
        </div>
      )}
    </Card>
  );
}

/* --------------------------------------------------------------- invoices */

const INVOICE_STATUS: Record<string, string> = {
  paid: "Paid",
  open: "Open",
  draft: "Draft",
  void: "Void",
  uncollectible: "Unpaid",
};

function InvoicesCard({ invoices, error }: { invoices: Invoice[] | null; error: string | null }) {
  return (
    <Card labelledBy="invoices-title">
      <div className="flex flex-col gap-1 sm:flex-row sm:items-end sm:justify-between">
        <h2 id="invoices-title" className="font-display text-lg font-semibold text-ink">
          Invoices
        </h2>
        <p className="text-xs text-ink-3">Pro subscription invoices.</p>
      </div>

      {error && !invoices && <Banner tone="error" className="mt-5">{error}</Banner>}

      {!invoices && !error && (
        <div className="mt-5">
          <Spinner label="Loading invoices" />
        </div>
      )}

      {invoices && invoices.length === 0 && (
        <p className="mt-5 text-sm leading-relaxed text-ink-3">
          No invoices yet. Your $5/month invoices appear here after the first charge.
        </p>
      )}

      {invoices && invoices.length > 0 && (
        <ul className="mt-5 divide-y divide-line border-y border-line">
          {invoices.map((inv) => (
            <InvoiceRow key={inv.id} invoice={inv} />
          ))}
        </ul>
      )}
    </Card>
  );
}

function InvoiceRow({ invoice: inv }: { invoice: Invoice }) {
  const date = fmtDate(toIso(inv.created));
  const status = inv.status ? (INVOICE_STATUS[inv.status] ?? inv.status) : "Unknown";
  return (
    <li className="flex flex-col gap-2 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
      <div className="min-w-0">
        <div className="font-medium text-ink">{date || "Date unknown"}</div>
        {inv.number && <div className="truncate text-xs text-ink-3">Invoice {inv.number}</div>}
      </div>
      <div className="flex items-center justify-between gap-4 sm:justify-end sm:gap-5">
        <span className="font-medium text-ink">{fmtMoney(inv.total, inv.currency)}</span>
        <span className="font-mono text-[11px] uppercase tracking-[0.12em] text-ink-3">{status}</span>
        {inv.url ? (
          <a
            href={inv.url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-sm font-medium text-ember-2 underline-offset-2 hover:underline"
          >
            View<span className="sr-only"> invoice {inv.number ?? ""}</span>
          </a>
        ) : (
          <span className="text-sm text-ink-3">No link</span>
        )}
      </div>
    </li>
  );
}

/* ---------------------------------------------------------------- usage */

const WINDOWS: Array<{ key: keyof UsageSummary; label: string }> = [
  { key: "today", label: "Today" },
  { key: "last_7d", label: "Last 7 days" },
  { key: "last_30d", label: "Last 30 days" },
];

function UsageCard({ usage, error }: { usage: UsageSummary | null; error: string | null }) {
  return (
    <Card labelledBy="usage-title">
      <div className="flex flex-col gap-1 sm:flex-row sm:items-end sm:justify-between">
        <h2 id="usage-title" className="font-display text-lg font-semibold text-ink">
          Usage saved
        </h2>
        <p className="text-xs text-ink-3">Counts only. Sent from your computers every few minutes.</p>
      </div>

      {error && !usage && <Banner tone="error" className="mt-4">{error}</Banner>}

      {!usage && !error && (
        <div className="mt-5">
          <Spinner label="Loading usage" />
        </div>
      )}

      {usage && (
        <div className="mt-5 grid gap-4 md:grid-cols-3">
          {WINDOWS.map((w) => (
            <UsageTile key={w.key} label={w.label} window={usage[w.key]} />
          ))}
        </div>
      )}
    </Card>
  );
}

function UsageTile({ label, window: w }: { label: string; window: UsageWindow }) {
  const empty = w.requests === 0 || w.tokens_before === 0;
  return (
    <div className="paper-panel flex flex-col gap-2 rounded-[2px] p-4">
      <div className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-3">{label}</div>
      {empty ? (
        <>
          <div className="font-display text-2xl font-semibold text-ink-3">No usage yet</div>
          <p className="text-xs leading-relaxed text-ink-3">
            Usage appears here after a Claude Code or Codex request runs through ContextShrink.
          </p>
        </>
      ) : (
        <>
          <div className="font-display text-3xl font-semibold text-ember-2">
            <span className="sr-only">Saved </span>
            {fmtPct(w.pct)}
          </div>
          <p className="text-sm text-ink-2">
            <span className="font-medium text-ink">{fmtCount(w.tokens_saved)}</span> tokens saved
          </p>
          <p className="text-xs leading-relaxed text-ink-3">
            of {fmtCount(w.tokens_before)} input tokens · {fmtFullCount(w.requests)} requests
          </p>
        </>
      )}
    </div>
  );
}

/* -------------------------------------------------------------- devices */

function toolsText(tools?: string[]): string {
  return tools && tools.length > 0 ? tools.map(toolLabel).join(", ") : "No tools yet";
}

function DevicesCard({
  account,
  busy,
  onDisconnect,
}: {
  account: AccountInfo;
  busy: string | null;
  onDisconnect: (d: Device) => void;
}) {
  const limit = account.device_limit || DEVICE_LIMIT;
  return (
    <Card labelledBy="devices-title">
      <div className="flex flex-col gap-1 sm:flex-row sm:items-end sm:justify-between">
        <h2 id="devices-title" className="font-display text-lg font-semibold text-ink">
          Computers
        </h2>
        <p className="text-sm text-ink-3">
          {account.devices.length} of {limit} connected
        </p>
      </div>

      {account.devices.length === 0 ? (
        <p className="mt-5 text-sm leading-relaxed text-ink-3">
          No computers connected yet. Use one of the install commands below to connect this computer.
        </p>
      ) : (
        <ul className="mt-5 flex flex-col gap-3">
          {account.devices.map((d) => (
            <li
              key={d.id}
              className="paper-panel flex flex-col gap-3 rounded-[2px] p-4 sm:flex-row sm:items-center sm:justify-between"
            >
              <div className="min-w-0">
                <div className="truncate font-medium text-ink">{d.name}</div>
                <div className="mt-0.5 text-xs leading-relaxed text-ink-3">
                  {platformLabel(d.platform)} · {toolsText(d.tools)} · last seen{" "}
                  {fmtRelative(d.last_seen_at)}
                </div>
              </div>
              <Button
                variant="danger"
                onClick={() => onDisconnect(d)}
                disabled={busy !== null}
                aria-label={`Disconnect ${d.name}`}
                className="!py-2 text-[12px] sm:shrink-0"
              >
                {busy === `device:${d.id}` ? "Disconnecting…" : "Disconnect"}
              </Button>
            </li>
          ))}
        </ul>
      )}

      <p className="mt-5 text-xs leading-relaxed text-ink-3">
        Disconnecting stops compression on that computer and frees its slot. To remove its settings, run{" "}
        <code className="font-mono">npx contextshrink-claude uninstall</code> (or the Codex command) on that
        computer.
      </p>
    </Card>
  );
}
