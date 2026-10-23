import { useCallback, useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { RequireAuth } from "../components/RequireAuth";
import { SiteFooter, SiteHeader } from "../components/SiteChrome";
import { Banner, Button, Card, CodeCommand, Kicker, Spinner, cx } from "../components/ui";
import { ApiError, isUnauthorized } from "../lib/api";
import { signOut, type SessionUser } from "../lib/auth";
import {
  disconnectDevice,
  getAccount,
  getUsageSummary,
  openBillingPortal,
  startCheckout,
  type Account as AccountInfo,
  type Device,
  type Plan,
  type UsageSummary,
  type UsageWindow,
} from "../lib/code";
import { DEVICE_LIMIT, INSTALL_COMMANDS, PRICE_MONTHLY_USD, TRIAL_DAYS } from "../lib/constants";
import {
  daysUntil,
  fmtCount,
  fmtDate,
  fmtFullCount,
  fmtPct,
  fmtRelative,
  platformLabel,
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
  const [now] = useState(() => Date.now());

  const load = useCallback(async () => {
    setLoading(true);
    const [acc, use] = await Promise.allSettled([getAccount(), getUsageSummary()]);
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

      {actionError && <Banner tone="error">{actionError}</Banner>}

      {accountError && !account ? (
        <Banner tone="error">{accountError}</Banner>
      ) : account ? (
        <PlanCard
          plan={account.plan}
          now={now}
          busy={busy}
          onSubscribe={() => void subscribe()}
          onPortal={() => void openPortal()}
        />
      ) : (
        <Spinner label="Loading your plan" />
      )}

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

/* ----------------------------------------------------------------- plan */

function planSummary(plan: Plan, now: number): { title: string; body: string; live: boolean } {
  const trialLeft = daysUntil(plan.trial_ends_at, now);
  switch (plan.state) {
    case "trial":
      return {
        title: "Free trial",
        live: plan.entitled,
        body:
          trialLeft === null
            ? `${TRIAL_DAYS}-day free trial. Subscribe for $${PRICE_MONTHLY_USD}/month any time.`
            : `${trialLeft === 1 ? "1 day" : `${trialLeft} days`} left, ends ${fmtDate(plan.trial_ends_at)}. Then $${PRICE_MONTHLY_USD}/month unless you subscribe. Cancel anytime.`,
      };
    case "active":
      return {
        title: "Subscribed",
        live: plan.entitled,
        body: plan.cancel_at_period_end
          ? `Your subscription ends on ${fmtDate(plan.current_period_end)}. Compression stays on until then.`
          : `$${PRICE_MONTHLY_USD}/month${plan.current_period_end ? `, renews ${fmtDate(plan.current_period_end)}` : ""}. Cancel anytime from billing.`,
      };
    case "past_due": {
      const graceEnd = plan.current_period_end
        ? new Date(new Date(plan.current_period_end).getTime() + 3 * 86_400_000).toISOString()
        : null;
      return {
        title: "Payment failed",
        live: plan.entitled,
        body: `Your last payment did not go through. Update your card in billing${
          graceEnd ? ` before ${fmtDate(graceEnd)}` : ""
        } to keep compression on.`,
      };
    }
    case "canceled":
      return {
        title: "Subscription ended",
        live: false,
        body: `Compression is off. Claude Code and Codex keep working as usual. Subscribe to turn compression back on.`,
      };
    default: {
      const ended = plan.trial_ends_at && trialLeft === 0;
      return {
        title: ended ? "Your trial has ended" : "No active plan",
        live: plan.entitled,
        body: ended
          ? `Compression is off, and Claude Code and Codex keep working as usual. Subscribe for $${PRICE_MONTHLY_USD}/month to turn it back on.`
          : `Compression is off, and Claude Code and Codex keep working as usual. Connect a computer to start your ${TRIAL_DAYS}-day free trial, or subscribe for $${PRICE_MONTHLY_USD}/month.`,
      };
    }
  }
}

function PlanCard({
  plan,
  now,
  busy,
  onSubscribe,
  onPortal,
}: {
  plan: Plan;
  now: number;
  busy: string | null;
  onSubscribe: () => void;
  onPortal: () => void;
}) {
  const summary = planSummary(plan, now);
  const hasSubscription = plan.state === "active" || plan.state === "past_due";
  const canSubscribe = !hasSubscription;
  const showPortal = hasSubscription || plan.state === "canceled";

  return (
    <Card labelledBy="plan-title">
      <div className="flex flex-col gap-6 md:flex-row md:items-start md:justify-between">
        <div className="min-w-0 max-w-xl">
          <div className="flex flex-wrap items-center gap-3">
            <h2 id="plan-title" className="font-display text-2xl font-semibold text-ink">
              {summary.title}
            </h2>
            <span
              className={cx(
                "rounded-[2px] border px-2 py-0.5 font-mono text-[11px] uppercase tracking-[0.12em]",
                summary.live
                  ? "border-sage text-sage"
                  : "border-line-2 text-ink-3",
              )}
            >
              {summary.live ? "Compression on" : "Compression off"}
            </span>
          </div>
          <p className="mt-3 text-[15px] leading-relaxed text-ink-2">{summary.body}</p>
        </div>

        <div className="flex w-full flex-col gap-2.5 md:w-auto md:min-w-[14rem]">
          {canSubscribe && (
            <Button onClick={onSubscribe} disabled={busy !== null} className="w-full">
              {busy === "checkout" ? "Opening checkout…" : `Subscribe for $${PRICE_MONTHLY_USD}/month`}
            </Button>
          )}
          {showPortal && (
            <Button
              variant={canSubscribe ? "ghost" : "ink"}
              onClick={onPortal}
              disabled={busy !== null}
              className="w-full"
            >
              {busy === "portal" ? "Opening billing…" : "Manage billing"}
            </Button>
          )}
          <p className="text-xs leading-relaxed text-ink-3">
            Cancel anytime. Up to {DEVICE_LIMIT} computers per account.
          </p>
        </div>
      </div>
    </Card>
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
