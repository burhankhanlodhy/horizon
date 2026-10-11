import { useState, type ReactNode } from "react";
import {
  Link,
  NavLink,
  Outlet,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { AnimatePresence, motion } from "framer-motion";
import {
  BarChart3,
  BookOpen,
  CreditCard,
  Download,
  Gauge,
  Inbox,
  MailWarning,
  LineChart,
  KeyRound,
  LogOut,
  Menu,
  Shrink,
  UserRound,
  X,
} from "lucide-react";
import { cn, fmtUsd } from "../lib/utils";
import { EASE } from "./ui";
import { initials, useAccount } from "../lib/account";
import { PROXY_URL, apiFetch } from "../lib/api";
import { openPortal } from "../lib/billing";
import {
  useBillingEstimate,
  type CompressionEntitlement,
  type PaymentIssue,
} from "../lib/usage";

/** Account-level notices: an unpaid savings fee, or the Free compression cap. */
function AccountNotices() {
  const { estimate } = useBillingEstimate();
  const { user } = useAccount();
  const unverified = user.email_verification_required && user.email_verified === false;
  return (
    <>
      {unverified && <VerifyEmailNotice email={user.email} />}
      <BillingNotices estimate={estimate} />
    </>
  );
}

/** Unconfirmed address: proxy keys (and so the desktop app) wait for it. */
function VerifyEmailNotice({ email }: { email: string }) {
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const resend = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const out = await apiFetch<{ detail?: string }>("/auth/email/resend", { method: "POST" });
      setStatus(out.detail ?? `We've sent a new link to ${email}.`);
    } catch (e) {
      setStatus(e instanceof Error ? e.message : "Couldn't send the email.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="mb-6 flex flex-wrap items-center gap-3 rounded-lg border border-ember/40 bg-ember-soft px-4 py-3 text-sm text-ink">
      <MailWarning size={17} className="shrink-0 text-ember" />
      <p className="min-w-0 flex-1">
        <strong>Confirm your email to connect your tools.</strong> Open the link we sent
        to {email} to create API keys and use the desktop app.
        {status && <span className="mt-1 block text-ink-3">{status}</span>}
      </p>
      <button
        onClick={() => void resend()}
        className="font-semibold text-ember underline-offset-2 hover:underline"
      >
        {busy ? "Sending…" : "Resend link"}
      </button>
    </div>
  );
}

function BillingNotices({ estimate }: { estimate: ReturnType<typeof useBillingEstimate>["estimate"] }) {
  if (!estimate) return null;
  // A payment problem explains any cap too, so it replaces the upgrade notice.
  if (estimate.payment_issue)
    return <PaymentIssueNotice issue={estimate.payment_issue} />;
  return <CompressionCapNotice cap={estimate.compression} />;
}

function PaymentIssueNotice({ issue }: { issue: PaymentIssue }) {
  const pauseOn = new Date(issue.pause_at).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
  return (
    <div className="mb-6 flex flex-wrap items-center gap-3 rounded-lg border border-ember/40 bg-ember-soft px-4 py-3 text-sm text-ink">
      <CreditCard size={17} className="shrink-0 text-ember" />
      <p className="min-w-0 flex-1">
        {issue.paused ? (
          <>
            <strong>Pro features are paused.</strong> Your{" "}
            {fmtUsd(issue.amount_usd)} savings fee is unpaid, so compression is
            capped and Advanced Analytics is off until it is paid. Once it is
            paid, you can upgrade to Pro again.
          </>
        ) : (
          <>
            <strong>Your {fmtUsd(issue.amount_usd)} savings fee payment
            failed.</strong>{" "}
            Pay it or update your card by {pauseOn} to keep Pro.
          </>
        )}
      </p>
      {issue.invoice_url && (
        <a
          href={issue.invoice_url}
          target="_blank"
          rel="noreferrer"
          className="font-semibold text-ember underline-offset-2 hover:underline"
        >
          Pay invoice
        </a>
      )}
      <button
        onClick={() => void openPortal().catch(() => undefined)}
        className="font-semibold text-ember underline-offset-2 hover:underline"
      >
        Update card
      </button>
    </div>
  );
}

/** Free-plan cap notice: shown when close to, or past, the monthly cap. */
function CompressionCapNotice({ cap }: { cap: CompressionEntitlement }) {
  if (!cap.capped || cap.cycle_savings_usd === null) return null;
  const paused = !cap.compression_allowed;
  if (!paused && cap.cycle_savings_usd < cap.cap_usd * 0.75) return null;
  const resets = new Date(cap.cycle_end).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
  return (
    <div
      className={cn(
        "mb-6 flex flex-wrap items-center gap-3 rounded-lg border px-4 py-3 text-sm",
        paused
          ? "border-ember/40 bg-ember-soft text-ink"
          : "border-ink/15 bg-ink/5 text-ink-2",
      )}
    >
      <Gauge size={17} className="shrink-0 text-ember" />
      <p className="min-w-0 flex-1">
        {paused ? (
          <>
            <strong>Free compression cap reached.</strong> You've saved{" "}
            {fmtUsd(cap.cycle_savings_usd)} this month, so requests now pass
            through uncompressed until {resets}.
          </>
        ) : (
          <>
            You've saved {fmtUsd(cap.cycle_savings_usd)} of your{" "}
            {fmtUsd(cap.cap_usd)} Free compression allowance this month.
          </>
        )}
      </p>
      <Link
        to="/subscriptions"
        className="font-semibold text-ember underline-offset-2 hover:underline"
      >
        Get unlimited with Pro
      </Link>
    </div>
  );
}

export function AuroraBackground() {
  return <div className="paper-grid fixed inset-0" aria-hidden />;
}

const nav = [
  { to: "/", label: "Profile", icon: UserRound },
  { to: "/keys", label: "API Keys", icon: KeyRound },
  { to: "/usage", label: "Usage", icon: BarChart3 },
  { to: "/subscriptions", label: "Subscriptions", icon: CreditCard },
  { to: "/downloads", label: "Downloads", icon: Download },
  { to: "/docs", label: "Documentation", icon: BookOpen },
];

const pageMeta: Record<string, { title: string; subtitle: string }> = {
  "/": { title: "Profile", subtitle: "Your account and session overview" },
  "/keys": {
    title: "API Keys",
    subtitle: "Credentials that route through your proxy",
  },
  "/usage": {
    title: "Usage",
    subtitle: "Requests, compression and savings over time",
  },
  "/subscriptions": {
    title: "Subscriptions",
    subtitle: "Providers, quotas and billing",
  },
  "/downloads": {
    title: "Downloads",
    subtitle: "The ContextShrink desktop app for your computer",
  },
  "/docs": {
    title: "Documentation",
    subtitle: "Quickstarts, endpoints and wiring guides",
  },
  "/messages": {
    title: "Messages",
    subtitle: "Contact form messages from the website",
  },
  "/signout": { title: "Sign out", subtitle: "" },
};

function Logo() {
  return (
    <div className="flex items-center gap-3 px-2">
      <motion.div
        className="grid h-10 w-10 place-items-center rounded-lg bg-ink text-paper shadow-[4px_4px_0_0_rgba(29,23,18,0.9)]"
        whileHover={{ rotate: -8, scale: 1.06 }}
        transition={{ type: "spring", stiffness: 300, damping: 15 }}
      >
        <Shrink size={19} className="text-paper" strokeWidth={2.4} />
      </motion.div>
      <div>
        <div className="font-display text-[15px] font-bold tracking-tight text-ink">
          Context<span className="text-ember">Shrink</span>
        </div>
        <div className="text-[10.5px] font-medium tracking-wide text-ink-3">
          same answers, fewer tokens
        </div>
      </div>
    </div>
  );
}

function NavItems({ onNavigate }: { onNavigate?: () => void }) {
  const { advanced, user } = useAccount();
  const items = user.is_admin
    ? [...nav, { to: "/messages", label: "Messages", icon: Inbox }]
    : nav;
  return (
    <nav className="mt-8 flex flex-1 flex-col gap-1">
      {items.map(({ to, label, icon: Icon }) => (
        <NavLink key={to} to={to} end={to === "/"} onClick={onNavigate}>
          {({ isActive }) => (
            <div
              className={cn(
                "relative flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors",
                isActive ? "text-ink" : "text-ink-3 hover:text-ink",
              )}
            >
              {isActive && (
                <motion.span
                  layoutId="nav-pill"
                  className="absolute inset-0 rounded-lg border border-ink/20 bg-ember-soft"
                  transition={{ type: "spring", stiffness: 380, damping: 32 }}
                />
              )}
              <Icon
                size={17}
                className={cn("relative z-10", isActive && "text-ember")}
              />
              <span className="relative z-10">{label}</span>
              {isActive && (
                <motion.span
                  layoutId="nav-dot"
                  className="relative z-10 ml-auto h-1.5 w-1.5 rounded-full bg-ember"
                />
              )}
            </div>
          )}
        </NavLink>
      ))}
      {advanced && (
        <a href="/dashboard" onClick={onNavigate}>
          <div className="relative flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium text-ink-3 transition-colors hover:text-ink">
            <LineChart size={17} />
            <span>Advanced Analytics</span>
          </div>
        </a>
      )}
      <NavLink to="/signout" onClick={onNavigate}>
        {({ isActive }) => (
          <div
            className={cn(
              "relative mt-auto flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors",
              isActive ? "text-ember" : "text-ink-3 hover:text-ember",
            )}
          >
            <LogOut size={17} />
            Sign out
          </div>
        )}
      </NavLink>
    </nav>
  );
}

/** Where tools connect (see Documentation); a label, not a health check. */
function ProxyStatus() {
  return (
    <div className="paper-panel rounded-lg px-3 py-2.5">
      <div className="text-[11px] leading-tight">
        <div className="font-semibold text-ink">Proxy</div>
        <div className="truncate text-ink-3">{PROXY_URL.replace(/^https?:\/\//, "")}</div>
      </div>
    </div>
  );
}

export function Shell() {
  const { user } = useAccount();
  const location = useLocation();
  const [mobileOpen, setMobileOpen] = useState(false);
  const meta = pageMeta[location.pathname] ?? {
    title: "ContextShrink",
    subtitle: "",
  };

  return (
    <div className="relative z-10 flex h-screen">
      {/* desktop sidebar */}
      <aside className="paper-panel hidden w-[250px] shrink-0 flex-col rounded-none border-y-0 border-l-0 px-4 py-6 lg:flex">
        <Logo />
        <NavItems />
        <ProxyStatus />
      </aside>

      {/* mobile drawer */}
      <AnimatePresence>
        {mobileOpen && (
          <motion.div
            className="fixed inset-0 z-40 bg-ink/60 backdrop-blur-sm lg:hidden"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={() => setMobileOpen(false)}
          >
            <motion.aside
              className="paper-panel h-full w-[260px] rounded-none border-y-0 border-l-0 px-4 py-6"
              initial={{ x: -280 }}
              animate={{ x: 0 }}
              exit={{ x: -280 }}
              transition={{ duration: 0.3, ease: EASE }}
              onClick={(e) => e.stopPropagation()}
            >
              <div className="flex items-center justify-between">
                <Logo />
                <button
                  className="rounded-lg p-2 text-ink-3 hover:bg-ink/10"
                  onClick={() => setMobileOpen(false)}
                >
                  <X size={18} />
                </button>
              </div>
              <NavItems onNavigate={() => setMobileOpen(false)} />
              <ProxyStatus />
            </motion.aside>
          </motion.div>
        )}
      </AnimatePresence>

      {/* main column */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-4 px-6 pt-6 lg:px-10">
          <button
            className="paper-panel rounded-lg p-2.5 text-ink-3 lg:hidden"
            onClick={() => setMobileOpen(true)}
          >
            <Menu size={18} />
          </button>
          <div className="min-w-0 flex-1">
            <motion.h1
              key={meta.title}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.35, ease: EASE }}
              className="font-display text-[22px] font-semibold text-ink"
            >
              {meta.title}
            </motion.h1>
            {meta.subtitle && (
              <p className="text-[13px] text-ink-3">{meta.subtitle}</p>
            )}
          </div>
          <div className="paper-panel hidden items-center gap-2 rounded-lg px-3 py-2 text-xs text-ink-3 md:flex">
            <svg
              width="13"
              height="13"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
            >
              <circle cx="11" cy="11" r="7" />
              <path d="m21 21-4.3-4.3" />
            </svg>
            Search
            <kbd className="ml-4 rounded-md border border-ink/20 bg-ink/5 px-1.5 py-0.5 font-mono text-[10px] text-ink-3">
              ⌘K
            </kbd>
          </div>
          <motion.div
            whileHover={{ scale: 1.05 }}
            className="grid h-10 w-10 cursor-pointer place-items-center rounded-full bg-ink text-[13px] font-bold text-paper"
          >
            {initials(user.name)}
          </motion.div>
        </header>

        <main className="min-h-0 flex-1 overflow-y-auto px-6 pb-10 pt-6 lg:px-10">
          <div className="mx-auto max-w-[1180px]">
            <AccountNotices />
          </div>
          <motion.div
            key={location.pathname}
            initial={{ opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.32, ease: EASE }}
            className="mx-auto max-w-[1180px]"
          >
            <Outlet />
          </motion.div>
        </main>
      </div>
    </div>
  );
}

export function FullPageCenter({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  return (
    <div className="relative z-10 grid h-screen place-items-center px-6">
      <button
        className="btn-ghost absolute right-6 top-6 rounded-lg px-4 py-2 text-sm"
        onClick={() => navigate("/")}
      >
        ← Back to dashboard
      </button>
      {children}
    </div>
  );
}
