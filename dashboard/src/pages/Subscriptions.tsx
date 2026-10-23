import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { motion } from "framer-motion";
import { ArrowUpRight, Check, Sparkles } from "lucide-react";
import {
  Badge,
  Card,
  EASE,
  GhostButton,
  GradientButton,
  Modal,
  ProgressRing,
  SectionHeader,
} from "../components/ui";
import { PLANS, useAccount, type Plan } from "../lib/account";
import {
  openPortal,
  renewSubscription,
  startCheckout,
  useBilling,
} from "../lib/billing";
import { useBillingEstimate } from "../lib/usage";
import { cn, fmtUsd } from "../lib/utils";
import { apiFetch } from "../lib/api";
import { useSelectedTeamId } from "../lib/workspace";

type TeamBillingEstimate = { team_id: string; seat_count: number | null; seat_price_usd: number | null; seats_usd: number | null; savings_usd: number | null; savings_fee_usd: number | null; total_usd: number | null; period_start: string; period_end: string; members?: { membership_id: string | null; user_id: string | null; name: string; role: string | null; savings_usd: number; removed: boolean }[] };
type SeatPreview = { current_seat_count: number; seat_count: number; prorated_amount_usd: number; next_invoice_amount_usd: number; effective_at: string | null; currency: "USD" };
type TeamBillingData = { team: { id: string; name: string; role: string; status: string; seat_count: number; pending_seat_count?: number | null; member_count: number; reserved_count: number }; members: { membership_id: string; email: string; name?: string | null; role: string }[]; invitations: unknown[] };

function TeamBillingPanel({ teamId }: { teamId: string }) {
  const [teamCheckoutParams, setTeamCheckoutParams] = useSearchParams();
  const [data, setData] = useState<TeamBillingData | null>(null);
  const [estimate, setEstimate] = useState<TeamBillingEstimate | null>(null);
  const [seatCount, setSeatCount] = useState(3);
  const [seatPreview, setSeatPreview] = useState<SeatPreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const owner = data?.team.role === "owner";
  const canViewTeamBilling = data?.team.role === "owner" || data?.team.role === "admin";
  async function load() {
    setError("");
    try {
      const [detail, billing] = await Promise.all([
        apiFetch<TeamBillingData>(`/teams/${encodeURIComponent(teamId)}`),
        apiFetch<TeamBillingEstimate>(`/teams/${encodeURIComponent(teamId)}/billing/estimate`),
      ]);
      setData(detail); setEstimate(billing); setSeatCount(Math.max(3, detail.team.seat_count));
    } catch (e) { setError(e instanceof Error ? e.message : "Unable to load Team billing"); }
  }
  useEffect(() => { void load(); }, [teamId]);
  useEffect(() => {
    const result = teamCheckoutParams.get("team_checkout");
    if (!result) return;
    setTeamCheckoutParams({}, { replace: true });
    if (result === "cancelled") { setNotice("Team checkout was cancelled. No Team subscription was activated."); return; }
    if (result === "success") { setNotice("Checkout completed. Team access activates after Stripe confirms payment."); void load(); }
  }, [teamCheckoutParams]);
  useEffect(() => {
    if (!data || seatCount === data.team.seat_count) { setSeatPreview(null); setPreviewLoading(false); return; }
    let active = true;
    setPreviewLoading(true);
    void apiFetch<SeatPreview>(`/teams/${encodeURIComponent(teamId)}/billing/seats/preview?seat_count=${seatCount}`)
      .then((preview) => { if (active) setSeatPreview(preview); })
      .catch((e) => { if (active) { setSeatPreview(null); setError(e instanceof Error ? e.message : "Unable to preview seat cost"); } })
      .finally(() => { if (active) setPreviewLoading(false); });
    return () => { active = false; };
  }, [teamId, data?.team.seat_count, seatCount]);
  async function checkout() {
    setBusy(true); setError("");
    try { const result = await apiFetch<{ checkout_url: string }>(`/teams/${encodeURIComponent(teamId)}/billing/checkout`, { method: "POST", body: { seat_count: data?.team.seat_count ?? 3 } }); window.location.assign(result.checkout_url); }
    catch (e) { setError(e instanceof Error ? e.message : "Unable to start Team checkout"); setBusy(false); }
  }
  async function portal() {
    setBusy(true); setError("");
    try { const result = await apiFetch<{ portal_url: string }>(`/teams/${encodeURIComponent(teamId)}/billing/portal`, { method: "POST" }); window.location.assign(result.portal_url); }
    catch (e) { setError(e instanceof Error ? e.message : "Unable to open Team billing"); setBusy(false); }
  }
  async function updateSeats() {
    if (!data || !estimate || !seatPreview || seatCount === data.team.seat_count || busy) return;
    const minSeats = Math.max(3, data.team.member_count + data.team.reserved_count);
    if (seatCount < minSeats) { setError(`At least ${minSeats} seats are required for current members and reservations.`); return; }
    const prompt = seatPreview.effective_at
      ? `Schedule Team seats to decrease from ${data.team.seat_count} to ${seatCount} at renewal? Next invoice estimate: ${fmtUsd(seatPreview.next_invoice_amount_usd)}.`
      : `Increase Team seats from ${data.team.seat_count} to ${seatCount}? Estimated current-cycle proration: ${fmtUsd(seatPreview.prorated_amount_usd)}. Next invoice estimate: ${fmtUsd(seatPreview.next_invoice_amount_usd)}.`;
    if (!window.confirm(prompt)) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await apiFetch<{ seat_count: number; pending_seat_count: number | null; effective_at: string | null; prorated_amount_usd: number | null; next_invoice_amount_usd: number }>(`/teams/${encodeURIComponent(teamId)}/billing/seats`, { method: "PATCH", body: { seat_count: seatCount } });
      setNotice(result.effective_at ? `Seat change scheduled for ${new Date(result.effective_at).toLocaleDateString()}. Next invoice estimate: ${fmtUsd(result.next_invoice_amount_usd)}.` : `Seats updated. Current-cycle prorated amount: ${result.prorated_amount_usd === null ? "calculated by Stripe" : fmtUsd(result.prorated_amount_usd)}; next invoice estimate: ${fmtUsd(result.next_invoice_amount_usd)}.`);
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : "Unable to update seats"); }
    finally { setBusy(false); }
  }
async function cancelScheduledSeatReduction() {
if (!data || data.team.pending_seat_count == null || busy) return;
const renewal = estimate?.period_end ? new Date(estimate.period_end).toLocaleDateString() : "the next renewal";
if (!window.confirm(`Cancel the scheduled reduction to ${data.team.pending_seat_count} seats? The current ${data.team.seat_count} seats will remain at ${renewal}.`)) return;
setBusy(true); setError(""); setNotice("");
try {
await apiFetch(`/teams/${encodeURIComponent(teamId)}/billing/seats`, { method: "PATCH", body: { seat_count: data.team.seat_count } });
setNotice(`Scheduled seat reduction canceled. ${data.team.seat_count} seats will remain at the ${renewal} renewal.`);
await load();
} catch (e) { setError(e instanceof Error ? e.message : "Unable to cancel scheduled seat reduction"); }
finally { setBusy(false); }
}
  return <div className="flex flex-col gap-6">
    <Card hairline className="p-6"><SectionHeader eyebrow="Team subscription" title={data?.team.name ?? "Team billing"} action={<Badge tone={data?.team.status === "active" ? "green" : "slate"}>{data?.team.status ?? "Loading"}</Badge>} />
{estimate && canViewTeamBilling && <>
        <div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {[["Purchased seats", `${estimate.seat_count ?? 0} × ${fmtUsd(estimate.seat_price_usd ?? 5)}`], ["Monthly seats", fmtUsd(estimate.seats_usd ?? 0)], ["Cycle savings", fmtUsd(estimate.savings_usd ?? 0)], ["One Team savings fee", fmtUsd(estimate.savings_fee_usd ?? 0)], ["Estimated total", fmtUsd(estimate.total_usd ?? 0)]].map(([label, value]) => <div key={label} className="rounded-lg border border-ink/10 bg-ink/5 p-4"><div className="text-xs text-ink-3">{label}</div><div className="mt-1 font-display text-xl font-bold text-ink">{value}</div></div>)}
        </div>
        <p className="mt-3 text-xs text-ink-3">Seat charges recur monthly. The savings fee is calculated once from eligible Team savings for {new Date(estimate.period_start).toLocaleDateString()}–{new Date(estimate.period_end).toLocaleDateString()}; estimates exclude applicable tax or adjustments. Initial checkout charges seats upfront; savings fees follow the completed usage cycle.</p>
      </>}
      {estimate && !canViewTeamBilling && <div className="mt-5 rounded-lg border border-ink/10 bg-ink/5 p-4"><div className="text-xs text-ink-3">Your eligible Team savings this cycle</div><div className="mt-1 font-display text-2xl font-bold text-ink">{fmtUsd(estimate.savings_usd ?? 0)}</div><p className="mt-2 text-xs text-ink-3">Your contribution is included in one Team savings fee. Billing totals and seat costs are managed by the Team owner.</p></div>}
      {estimate && canViewTeamBilling && <p className="mt-3 text-xs text-ink-3">The 5% savings fee is waived when combined eligible savings are $20 or less; above $20 it applies once to the full Team total. Initial checkout charges seats upfront; the estimate reflects the current cycle and excludes applicable tax or adjustments.</p>}
      {!estimate && <p className="mt-4 text-sm text-ink-3">Team estimate unavailable.</p>}
      {error && <p role="alert" className="mt-3 text-sm text-ember">{error}</p>}{notice && <p role="status" className="mt-3 text-sm text-sage">{notice}</p>}
      {owner && data?.team.pending_seat_count != null && <div className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-ink/10 bg-ink/5 p-4"><p className="text-sm text-ink-2">Seat reduction to {data.team.pending_seat_count} seats is scheduled for renewal{estimate?.period_end ? ` on ${new Date(estimate.period_end).toLocaleDateString()}` : ""}. Until then, {data.team.seat_count} seats remain active.</p><GhostButton disabled={busy} onClick={() => void cancelScheduledSeatReduction()}>Cancel scheduled reduction</GhostButton></div>}
      {owner && data && <div className="mt-5 flex flex-wrap items-end gap-3 border-t border-ink/10 pt-5"><label className="text-xs text-ink-3">Purchased seats<input type="number" min={Math.max(3,data.team.member_count+data.team.reserved_count)} value={seatCount} onChange={(event)=>setSeatCount(Math.max(3,Number(event.target.value)||3))} className="mt-1 block w-28 rounded-lg border border-ink/15 bg-card px-3 py-2 text-sm text-ink" /></label><GradientButton disabled={busy || previewLoading || seatCount===data.team.seat_count || !seatPreview} onClick={()=>void updateSeats()}>{busy?"Updating…":previewLoading?"Calculating…":"Review seat change"}</GradientButton><span className="text-xs text-ink-3">Each added seat adds {fmtUsd(estimate?.seat_price_usd ?? 5)} per month. Reductions take effect at renewal.</span>{seatPreview && <p className="w-full text-xs text-ink-3">{seatPreview.effective_at ? `Scheduled for ${new Date(seatPreview.effective_at).toLocaleDateString()}; next invoice estimate ${fmtUsd(seatPreview.next_invoice_amount_usd)}.` : `Estimated current-cycle proration ${fmtUsd(seatPreview.prorated_amount_usd)}; next invoice estimate ${fmtUsd(seatPreview.next_invoice_amount_usd)}.`}</p>}</div>}
      {owner && <div className="mt-5 flex flex-wrap gap-3">{data?.team.status === "active" ? <GhostButton disabled={busy} onClick={()=>void portal()}>Payment methods and invoices <ArrowUpRight size={14}/></GhostButton> : <GradientButton disabled={busy} onClick={()=>void checkout()}>{busy?"Opening Stripe…":"Complete Team checkout"} <ArrowUpRight size={14}/></GradientButton>}</div>}
    </Card>
    {data && <Card className="p-6"><SectionHeader eyebrow="Savings attribution" title="Current cycle contributions" /><div className="mt-4 divide-y divide-ink/5">{(estimate?.members ?? []).map((member,index)=><div key={member.membership_id ?? `service-${index}`} className="flex justify-between py-3 text-sm"><span className="text-ink">{member.name}{member.removed ? " · former member" : ""}</span><span className="font-medium text-ink">{fmtUsd(member.savings_usd)}</span></div>)}</div><p className="mt-2 text-xs text-ink-3">{canViewTeamBilling ? "The 5% fee is charged once on combined eligible Team savings after the $20 cycle waiver." : "This is your contribution to the Team cycle; consolidated billing is managed by the owner."}</p></Card>}
  </div>;
}

export default function Subscriptions() {
  const { plan, changePlan, reloadAccount, user } = useAccount();
  const navigate = useNavigate();
  const [teamEnabled, setTeamEnabled] = useState(false);
const teamId = useSelectedTeamId();
  useEffect(() => { void apiFetch("/teams").then(() => setTeamEnabled(true)).catch(() => setTeamEnabled(false)); }, []);
  const { estimate, error: estimateError, loading: estimateLoading } =
    useBillingEstimate();
  const { info, invoices, reload: reloadBilling } = useBilling(!teamId);
  const [params, setParams] = useSearchParams();
  const current = PLANS.find((p) => p.id === plan)!;
  const estimatePlanName =
    PLANS.find((p) => p.id === estimate?.plan)?.name ?? current.name;
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<Plan>(plan);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  // Returning from Stripe Checkout: Pro is granted by the webhook, so poll
  // briefly until the account reflects it.
  const checkout = params.get("checkout");
  useEffect(() => {
    if (!checkout) return;
    setParams({}, { replace: true });
    if (checkout !== "success") {
      setNotice("Checkout cancelled. Your plan has not changed.");
      return;
    }
    setNotice("Payment method saved. Activating Pro…");
    let cancelled = false;
    void (async () => {
      for (let attempt = 0; attempt < 20 && !cancelled; attempt++) {
        const fresh = await reloadAccount().catch(() => null);
        if (fresh?.plan === "pro") {
          setNotice("You're on Pro: unlimited compression is active.");
          void reloadBilling();
          return;
        }
        await new Promise((r) => setTimeout(r, 3000));
      }
      if (!cancelled)
        setNotice(
          "Your payment method is saved. Pro activation is taking longer than usual; refresh in a minute.",
        );
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [checkout]);

  function manage(next: Plan = plan) {
    setSelected(next);
    setError("");
    setNotice("");
    setOpen(true);
  }
  const cancelDate = info?.cancel_at_period_end
    ? (info.cancel_at ?? info.current_period_end)
    : null;
  const cancelsOn = cancelDate
    ? new Date(cancelDate).toLocaleDateString()
    : null;
  // An unpaid savings fee must be paid before upgrading again.
  const unpaidFee = estimate?.payment_issue ?? null;
  const upgradeBlocked = plan !== "pro" && Boolean(unpaidFee);
  const upgrading = selected === "pro" && plan !== "pro" && !upgradeBlocked;
  const renewing = selected === plan && plan !== "free" && Boolean(cancelsOn);
  const cancelling =
    selected === "free" && Boolean(info?.has_subscription) && !cancelsOn;
  async function renew() {
    if (saving) return;
    setSaving(true);
    setError("");
    try {
      await renewSubscription();
      await reloadBilling();
      setOpen(false);
      setNotice(`${current.name} renewed. It will keep renewing each month.`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to renew");
    } finally {
      setSaving(false);
    }
  }
  async function portal() {
    setError("");
    try {
      await openPortal();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to open billing");
    }
  }
  async function save() {
    if (renewing) return renew();
    if (saving) return;
    setSaving(true);
    setError("");
    try {
      if (selected === "team") {
        if (teamEnabled) { setOpen(false); navigate("/team"); return; }
        setError("Team workspaces are not enabled yet.");
        return;
      }
      if (selected === "free" && cancelsOn) {
        setError(`Already cancelled: ${current.name} ends on ${cancelsOn}.`);
        return;
      }
      if (upgrading) {
        await startCheckout("pro"); // leaves the page
        return;
      }
      if (cancelling) {
        await openPortal(); // cancellation bills the final savings fee
        return;
      }
      await changePlan(selected);
      setOpen(false);
      setNotice(
        `Your subscription is now ${PLANS.find((p) => p.id === selected)!.name}.`,
      );
    } catch (e) {
      setError(
        e instanceof Error ? e.message : "Unable to change subscription",
      );
    } finally {
      setSaving(false);
    }
  }
  if (teamId) return <TeamBillingPanel teamId={teamId} />;
  return (
    <div className="flex flex-col gap-6">
      {/* plan banner */}
      <motion.div
        initial={{ opacity: 0, y: 18 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5, ease: EASE }}
      >
        <Card hairline className="relative overflow-hidden p-7">
          <div className="flex flex-wrap items-center gap-8">
            <div className="min-w-0 flex-1">
              <Badge tone="indigo" className="mb-3">
                <Sparkles size={11} /> current plan
              </Badge>
              <h2 className="font-display text-2xl font-bold text-ink">
                Horizon <span className="text-ember">{current.name}</span>
              </h2>
              <p className="mt-1.5 max-w-lg text-sm text-ink-3">
                {current.description}
              </p>
              {cancelsOn && (
                <p className="mt-2 text-sm text-ember">
                  {current.name} ends on {cancelsOn}. Your final savings fee is
                  billed then.{" "}
                  <button
                    onClick={() => void renew()}
                    disabled={saving}
                    className="font-semibold underline underline-offset-2 hover:text-ember-2 disabled:opacity-60"
                  >
                    {saving ? "Renewing…" : `Renew ${current.name}`}
                  </button>
                </p>
              )}
              <div className="mt-4 flex flex-wrap gap-2">
                {current.features.map((f) => (
                  <span
                    key={f}
                    className="inline-flex items-center gap-1.5 rounded-full border border-ink/10 bg-ink/5 px-3 py-1 text-[11px] text-ink-2"
                  >
                    <Check size={11} className="text-sage" /> {f}
                  </span>
                ))}
              </div>
            </div>
            <div className="text-center">
              <div className="font-display text-4xl font-bold text-ink">
                {current.name}
                <span className="text-base font-medium text-ink-3"> plan</span>
              </div>
              <GradientButton className="mt-4" onClick={() => manage()}>
                Manage billing <ArrowUpRight size={14} />
              </GradientButton>
            </div>
          </div>
        </Card>
      </motion.div>
      {notice && (
        <p role="status" className="text-sm text-sage">
          {notice}
        </p>
      )}

      <Card hairline className="p-6">
        <SectionHeader
          eyebrow="Billing cycle"
          title="Estimated bill"
          action={<Badge tone={estimateError ? "red" : estimate ? "green" : "slate"}>{estimateError ? "Unavailable" : estimate ? "Account estimate" : "Loading…"}</Badge>}
        />
        {estimate ? (
          <>
            <div className={cn(
              "grid grid-cols-1 gap-4 sm:grid-cols-2",
              estimate.plan === "pro" && "md:grid-cols-3",
              estimate.plan === "team" && "xl:grid-cols-4",
            )}>
              <div className="rounded-lg border border-ink/10 bg-ink/5 p-4">
                <div className="text-xs text-ink-3">Est. savings this cycle</div>
                <div className="mt-1 font-display text-2xl font-bold text-ink">{fmtUsd(estimate.estimated_savings_usd)}</div>
              </div>
              <div className="rounded-lg border border-ink/10 bg-ink/5 p-4">
                <div className="text-xs text-ink-3">Current plan estimate · {estimatePlanName}</div>
                <div className="mt-1 font-display text-2xl font-bold text-ink">{fmtUsd(estimate.estimated_total_usd)}</div>
              </div>
              {estimate.plan !== "free" && (
                <div className="rounded-lg border border-ink/10 bg-ink/5 p-4">
                  <div className="text-xs text-ink-3">Savings fee · {estimatePlanName}</div>
                  <div className="mt-1 font-display text-2xl font-bold text-ink">{fmtUsd(estimate.estimates[estimate.plan].savings_fee)}</div>
                </div>
              )}
              {estimate.plan === "team" && (
                <div className="rounded-lg border border-ink/10 bg-ink/5 p-4">
                  <div className="text-xs text-ink-3">Team seats · {estimate.seat_count} × $5</div>
                  <div className="mt-1 font-display text-2xl font-bold text-ink">{fmtUsd(estimate.estimates.team.seat_fee)}</div>
                </div>
              )}
            </div>
            <p className="mt-3 text-xs text-ink-3">
              {estimate.plan === "free"
                ? "Free has no subscription or savings fees. "
                : `The ${estimate.savings_fee_rate * 100}% savings fee is waived when cycle savings are ${fmtUsd(estimate.savings_fee_threshold_usd)} or less. Above ${fmtUsd(estimate.savings_fee_threshold_usd)}, it is ${estimate.savings_fee_rate * 100}% of the full estimated savings. `}
              {estimate.plan === "team" && "Team also includes $5 per account seat each month. "}
              Estimates cover {new Date(estimate.period_start).toLocaleDateString()}–{new Date(estimate.period_end).toLocaleDateString()}{estimate.plan !== "free" && " and are billed at cycle end"}.
            </p>
          </>
        ) : (
          <p className="text-sm text-ink-3">{estimateError || (estimateLoading ? "Loading your account billing estimate…" : "Billing estimate unavailable.")}</p>
        )}
      </Card>

      {/* provider subscriptions */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        {PLANS.map((s, i) => (
          <motion.div
            key={s.id}
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, ease: EASE, delay: 0.1 + i * 0.09 }}
          >
            <Card hairline className="flex h-full flex-col p-6">
              <div className="flex items-start justify-between">
                <div
                  className={cn(
                    "grid h-11 w-11 place-items-center rounded-lg bg-gradient-to-br font-display text-sm font-bold text-paper",
                    s.accent,
                  )}
                >
                  {s.name.slice(0, 1)}
                </div>
                <Badge tone={s.id === plan ? "green" : "slate"}>
                  {s.id === plan
                    ? (user.subscription_status ?? "active")
                    : "available"}
                </Badge>
              </div>
              <div className="mt-4">
                <div className="text-sm font-semibold text-ink">
                  Horizon {s.name}
                </div>
                <div className="text-xs text-ink-3">{s.id === "team" && teamEnabled ? "Three or more seats, one shared savings estimate and a Team billing workspace." : s.description}</div>
              </div>
              <div className="my-5 flex justify-center">
                <ProgressRing
                  key={`${s.id}-${plan}`}
                  pct={s.id === plan ? 100 : 0}
                  size={116}
                >
                  <div className="text-center">
                    <div className="font-display text-xl font-bold text-ink">
                      {s.name}
                    </div>
                    <div className="text-[9px] uppercase tracking-wider text-ink-3">
                      {s.id === plan ? "current" : "available"}
                    </div>
                  </div>
                </ProgressRing>
              </div>
              <div className="mt-auto flex items-center justify-between border-t border-ink/10 pt-4 text-xs text-ink-3">
                <span>
                  {s.id === "free" ? "Usage overview" : "Advanced Analytics"}
                </span>
                <button
                  onClick={() => s.id === "team" && teamEnabled ? navigate("/team") : manage(s.id)}
                  className="font-medium text-ember transition hover:text-ember-2"
                >
                  Manage →
                </button>
              </div>
            </Card>
          </motion.div>
        ))}
      </div>

      {/* billing history */}
      <Card className="overflow-hidden">
        <div className="px-6 pt-6">
          <SectionHeader
            eyebrow="Billing"
            title="Payment history"
            action={
              <Badge tone="slate">
                {invoices === null
                  ? "Loading…"
                  : `${invoices.length} invoice${invoices.length === 1 ? "" : "s"}`}
              </Badge>
            }
          />
        </div>
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-y border-ink/10 text-[11px] uppercase tracking-wider text-ink-3">
              <th className="px-6 py-3 font-medium">Date</th>
              <th className="py-3 font-medium">Item</th>
              <th className="py-3 text-right font-medium">Amount</th>
              <th className="px-6 py-3 text-right font-medium">Status</th>
            </tr>
          </thead>
          <tbody>
            {invoices && invoices.length > 0 ? (
              invoices.map((inv) => (
                <tr key={inv.id} className="border-b border-ink/5 last:border-0">
                  <td className="px-6 py-3.5 text-ink-2">
                    {new Date(inv.created).toLocaleDateString()}
                  </td>
                  <td className="py-3.5 text-ink">
                    {inv.url ? (
                      <a
                        href={inv.url}
                        target="_blank"
                        rel="noreferrer"
                        className="hover:text-ember"
                      >
                        {inv.description}
                      </a>
                    ) : (
                      inv.description
                    )}
                  </td>
                  <td className="py-3.5 text-right font-medium text-ink">
                    {fmtUsd(inv.total)}
                  </td>
                  <td className="px-6 py-3.5 text-right">
                    <Badge tone={inv.status === "paid" ? "green" : "slate"}>
                      {inv.status}
                    </Badge>
                  </td>
                </tr>
              ))
            ) : (
              <tr>
                <td colSpan={4} className="px-6 py-3.5 text-ink-3">
                  {invoices === null
                    ? "Loading invoices…"
                    : "No invoices yet. Pro has no base fee; a savings fee invoice appears only for months where you save more than $20."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </Card>

      {info?.has_billing_account && (
        <div className="flex justify-center">
          <GhostButton onClick={() => void portal()}>
            Payment method &amp; invoices <ArrowUpRight size={14} />
          </GhostButton>
        </div>
      )}
      <Modal
        open={open}
        onClose={() => {
          if (!saving) setOpen(false);
        }}
        title="Manage billing"
      >
        <p className="mb-4 text-sm text-ink-3">
          Free never needs a payment method. Pro has no base fee: you add a card
          in Stripe Checkout and are charged only 5% of a month's savings when
          they exceed $20. Cancel any time from the billing portal; Pro stays
          active until the end of the period.
        </p>
        {upgradeBlocked && unpaidFee && (
          <p className="mb-4 rounded-md border border-ember/30 bg-ember-soft px-3 py-2 text-sm text-ink">
            Pro is unavailable until your {fmtUsd(unpaidFee.amount_usd)}{" "}
            savings fee is paid.{" "}
            {unpaidFee.invoice_url && (
              <a
                href={unpaidFee.invoice_url}
                target="_blank"
                rel="noreferrer"
                className="font-semibold text-ember underline underline-offset-2"
              >
                Pay invoice
              </a>
            )}
          </p>
        )}
        {cancelsOn && (
          <p className="mb-4 rounded-md border border-ember/30 bg-ember-soft px-3 py-2 text-sm text-ink">
            {current.name} is cancelled and ends on {cancelsOn}. Keep{" "}
            {current.name} selected and choose Renew to keep it.
          </p>
        )}
        <label
          className="mb-2 block text-xs font-semibold text-ink-2"
          htmlFor="subscription-plan"
        >
          Subscription
        </label>
        <select
          id="subscription-plan"
          value={selected}
          disabled={saving}
          onChange={(e) => setSelected(e.target.value as Plan)}
          className="w-full rounded-lg border border-ink/20 bg-card px-3 py-2 text-sm text-ink"
        >
          {PLANS.map((p) => (
            <option
              key={p.id}
              value={p.id}
              disabled={(p.id === "team" && !teamEnabled) || (p.id === "pro" && upgradeBlocked)}
            >
              {p.id === "team"
                ? teamEnabled ? `${p.name} · create or manage a Team` : `${p.name} (coming soon)`
                : p.id === "pro" && upgradeBlocked
                  ? `${p.name} (pay outstanding fee first)`
                  : p.name}
            </option>
          ))}
        </select>
        {error && (
          <p role="alert" className="mt-3 text-sm text-ember">
            {error}
          </p>
        )}
        <div className="mt-5 flex justify-end gap-3">
          <GhostButton disabled={saving} onClick={() => setOpen(false)}>
            Cancel
          </GhostButton>
          <GradientButton
            disabled={
              saving ||
              (selected === plan &&
                user.subscription_status === "active" &&
                !renewing)
            }
            onClick={() => void save()}
          >
            {saving
              ? upgrading || cancelling
                ? "Opening Stripe…"
                : renewing
                  ? "Renewing…"
                  : "Saving…"
              : upgrading
                ? "Continue to checkout"
                : renewing
                  ? `Renew ${current.name}`
                  : cancelling
                    ? "Cancel in billing portal"
                    : "Save subscription"}
          </GradientButton>
        </div>
      </Modal>
    </div>
  );
}
