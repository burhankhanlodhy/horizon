import { useState } from "react";
import { motion } from "framer-motion";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Badge, Card, SectionHeader, StatCard } from "../components/ui";
import { compression, pct, useUsage } from "../lib/usage";
import { cn, dayLabel, fmtCompact, fmtUsd } from "../lib/utils";

const RANGES = [7, 14, 30] as const;

function ChartTip({ active, payload }: { active?: boolean; payload?: any[] }) {
  if (!active || !payload?.length) return null;
  const first = payload[0];
  const title = first?.payload?.date
    ? dayLabel(first.payload.date)
    : String(first?.payload?.name ?? first?.name ?? "");
  return (
    <div className="paper-panel rounded-lg px-3.5 py-2.5 text-xs">
      <div className="mb-1.5 font-semibold text-ink">{title}</div>
      {payload.map((p: any, i: number) => {
        const key = String(p.dataKey ?? p.name ?? "");
        const isShare = key === "share";
        const name = isShare
          ? String(p.payload?.name ?? "share")
          : key === "tokensSaved"
            ? "tokens saved"
            : key;
        const value = isShare
          ? `${Math.round((Number(p.value) || 0) * 100)}%`
          : fmtCompact(Number(p.value) || 0);
        const color = p.stroke ?? p.payload?.color ?? p.fill ?? "#c14d1b";
        return (
          <div key={i} className="flex items-center gap-2 py-0.5">
            <span
              className="h-2 w-2 rounded-full"
              style={{ background: color }}
            />
            <span className="text-ink-3">{name}</span>
            <span className="ml-auto pl-4 font-semibold text-ink">{value}</span>
          </div>
        );
      })}
    </div>
  );
}

function FunnelBar({
  label,
  sub,
  width,
  gradient,
  delay,
}: {
  label: string;
  sub: string;
  width: number;
  gradient: string;
  delay: number;
}) {
  return (
    <div>
      <div className="mb-1.5 flex items-baseline justify-between gap-4 text-xs">
        <span className="font-medium text-ink-2">{label}</span>
        <span className="truncate text-ink-3">{sub}</span>
      </div>
      <div className="h-9 overflow-hidden rounded-lg border border-ink/10 bg-ink/5">
        <motion.div
          className={cn(
            "flex h-full items-center rounded-lg bg-gradient-to-r px-3",
            gradient,
          )}
          initial={{ width: 0 }}
          animate={{ width: `${Math.min(100, Math.max(0, width))}%` }}
          transition={{ duration: 1.3, ease: [0.22, 1, 0.36, 1], delay }}
        >
          <span className="whitespace-nowrap text-sm font-bold text-paper">
            {width}%
          </span>
        </motion.div>
      </div>
    </div>
  );
}

function Figure({
  label,
  value,
  sub,
}: {
  label: string;
  value: string;
  sub: string;
}) {
  return (
    <div className="rounded-lg border border-ink/10 bg-ink/5 px-4 py-3">
      <div className="text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-3">
        {label}
      </div>
      <div className="mt-1 font-display text-2xl font-semibold text-ink">
        {value}
      </div>
      <div className="mt-0.5 text-xs text-ink-3">{sub}</div>
    </div>
  );
}

const signedUsd = (n: number) => (n < 0 ? `−${fmtUsd(-n)}` : fmtUsd(n));

export default function Usage() {
  const [range, setRange] = useState<(typeof RANGES)[number]>(14);
  const { series, t, previous, pieData, sessions, error, loaded, loading } =
    useUsage(range);
  const savedPct = compression(t);
  const deliveredPct = t.tokensBefore
    ? (t.tokensAfter / t.tokensBefore) * 100
    : 0;

  return (
    <div className="flex flex-col gap-6">
      {/* range toggle */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="paper-panel inline-flex rounded-lg p-1">
          {RANGES.map((r) => (
            <button
              key={r}
              onClick={() => setRange(r)}
              className={cn(
                "relative rounded-md px-4 py-1.5 text-xs font-semibold transition",
                range === r ? "text-ink" : "text-ink-3 hover:text-ink",
              )}
            >
              {range === r && (
                <motion.span
                  layoutId="range-pill"
                  className="absolute inset-0 rounded-md bg-ember-soft"
                  transition={{ type: "spring", stiffness: 400, damping: 32 }}
                />
              )}
              <span className="relative z-10">{r}d</span>
            </button>
          ))}
        </div>
        <Badge tone={error ? "red" : loaded ? "green" : "slate"}>
          <span className="live-dot !h-1.5 !w-1.5" />{" "}
          {error
            ? "Refresh unavailable"
            : loading
              ? "Loading usage…"
              : "Live usage · UTC"}
        </Badge>
      </div>
      {error && (
        <p role="alert" className="text-sm text-ember">
          {error}
          {loaded
            ? " · Showing the last successful update."
            : " · Usage is unavailable."}
        </p>
      )}

      {/* stat row */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label={`Requests · ${range}d`}
          value={t.requests}
          format={loaded ? undefined : () => "—"}
          delta={pct(t.requests, previous.requests)}
          spark={series.map((d) => d.requests)}
        />
        <StatCard
          label="Tokens saved"
          value={t.tokensSaved}
          format={loaded ? fmtCompact : () => "—"}
          delta={pct(t.tokensSaved, previous.tokensSaved)}
          spark={series.map((d) => d.tokensSaved)}
          color="#5f7452"
        />
        <StatCard
          label="Est. savings"
          value={t.savingsUsd}
          format={loaded ? fmtUsd : () => "—"}
          delta={pct(t.savingsUsd, previous.savingsUsd)}
          spark={series.map((d) => d.savingsUsd)}
          color="#c14d1b"
        />
        <StatCard
          label="Avg saved / request"
          value={Math.round(t.tokensSaved / Math.max(1, t.requests))}
          format={loaded ? fmtCompact : () => "—"}
          spark={series.map((d) =>
            Math.round(d.tokensSaved / Math.max(1, d.requests)),
          )}
          color="#c14d1b"
        />
      </div>

      {/* chart + donut */}
      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[1.7fr_1fr]">
        <Card hairline className="p-6">
          <SectionHeader eyebrow="Traffic" title="Requests & tokens saved" />
          <div className="h-[300px]">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart
                data={series}
                margin={{ top: 8, right: 8, bottom: 0, left: -14 }}
              >
                <defs>
                  <linearGradient id="reqFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#c14d1b" stopOpacity={0.45} />
                    <stop offset="100%" stopColor="#c14d1b" stopOpacity={0} />
                  </linearGradient>
                  <linearGradient id="tokFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#5f7452" stopOpacity={0.35} />
                    <stop offset="100%" stopColor="#5f7452" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="rgba(29,23,18,0.05)" vertical={false} />
                <XAxis
                  dataKey="date"
                  tickFormatter={dayLabel}
                  tick={{ fill: "#8b7f6f", fontSize: 11 }}
                  axisLine={false}
                  tickLine={false}
                  minTickGap={28}
                />
                <YAxis
                  yAxisId="req"
                  tick={{ fill: "#8b7f6f", fontSize: 11 }}
                  axisLine={false}
                  tickLine={false}
                  tickFormatter={(v: number) => fmtCompact(v)}
                />
                <YAxis
                  yAxisId="tok"
                  orientation="right"
                  tick={{ fill: "#8b7f6f", fontSize: 11 }}
                  axisLine={false}
                  tickLine={false}
                  tickFormatter={(v: number) => fmtCompact(v)}
                />
                <Tooltip
                  content={<ChartTip />}
                  cursor={{ stroke: "rgba(29,23,18,0.15)" }}
                />
                <Area
                  yAxisId="tok"
                  type="monotone"
                  dataKey="tokensSaved"
                  stroke="#5f7452"
                  strokeWidth={2}
                  fill="url(#tokFill)"
                  animationDuration={1200}
                />
                <Area
                  yAxisId="req"
                  type="monotone"
                  dataKey="requests"
                  stroke="#c14d1b"
                  strokeWidth={2.5}
                  fill="url(#reqFill)"
                  animationDuration={1400}
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </Card>

        <Card hairline className="p-6">
          <SectionHeader eyebrow="Mix" title="Traffic by model" />
          <div className="relative mx-auto h-[210px] w-[210px]">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Tooltip content={<ChartTip />} />
                <Pie
                  data={pieData}
                  dataKey="share"
                  nameKey="name"
                  innerRadius={64}
                  outerRadius={92}
                  paddingAngle={4}
                  cornerRadius={6}
                  strokeWidth={0}
                  animationDuration={1300}
                >
                  {pieData.map((m) => (
                    <Cell key={m.name} fill={m.color} />
                  ))}
                </Pie>
              </PieChart>
            </ResponsiveContainer>
            <div className="pointer-events-none absolute inset-0 grid place-items-center text-center">
              <div>
                <div className="font-display text-xl font-bold text-ink">
                  {fmtCompact(t.requests)}
                </div>
                <div className="text-[10px] uppercase tracking-wider text-ink-3">
                  requests
                </div>
              </div>
            </div>
          </div>
          <div className="mt-4 flex flex-col gap-2.5">
            {loaded && !pieData.length && (
              <p className="text-xs text-ink-3">
                No recorded requests in this period.
              </p>
            )}
            {pieData.map((m) => (
              <div key={m.name} className="flex items-center gap-2.5 text-xs">
                <span
                  className="h-2.5 w-2.5 rounded-[4px]"
                  style={{ background: m.color }}
                />
                <span className="text-ink-2">{m.name}</span>
                <span className="ml-auto font-semibold text-ink">
                  {Math.round(m.share * 100)}%
                </span>
              </div>
            ))}
          </div>
        </Card>
      </div>

      {/* funnel */}
      <Card className="p-6">
        <SectionHeader eyebrow="Savings" title="Where every request lands" />
        <div className="flex flex-col gap-4">
          <FunnelBar
            label="Original context"
            sub={`${fmtCompact(t.tokensBefore)} tokens before compression`}
            width={t.tokensBefore ? 100 : 0}
            gradient="from-ember/70 to-ember/40"
            delay={0}
          />
          <FunnelBar
            label="Delivered to model"
            sub={`${fmtCompact(t.tokensAfter)} tokens after compression`}
            width={Number(deliveredPct.toFixed(1))}
            gradient="from-sage/70 to-sage/40"
            delay={0.15}
          />
          <FunnelBar
            label="Saved by Horizon"
            sub={`${fmtUsd(t.savingsUsd)} est. cost avoided`}
            width={Number(savedPct.toFixed(1))}
            gradient="from-ember/70 to-ember/40"
            delay={0.3}
          />
        </div>
      </Card>

      {/* cache keep-alive: shown once the proxy has pinged for this account */}
      {t.keepalivePings > 0 && (
        <Card className="p-6">
          <SectionHeader
            eyebrow="Keep-alive"
            title="Cache kept warm through pauses"
          />
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
            <Figure
              label="Rewrites avoided"
              value={fmtUsd(t.keepaliveAvoidedUsd)}
              sub={`${fmtCompact(t.keepaliveResumes)} sessions resumed warm`}
            />
            <Figure
              label="Spent on pings"
              value={fmtUsd(t.keepaliveSpendUsd)}
              sub={`${fmtCompact(t.keepalivePings)} pings, including sessions never resumed`}
            />
            <Figure
              label="Net"
              value={signedUsd(t.keepaliveAvoidedUsd - t.keepaliveSpendUsd)}
              sub="Already included in Est. savings"
            />
          </div>
        </Card>
      )}

      {/* sessions table */}
      <Card className="overflow-hidden">
        <div className="px-6 pb-1 pt-6">
          <SectionHeader
            eyebrow="Sessions"
            title="Recent proxy sessions"
            action={<Badge tone="slate">{sessions.length} recent</Badge>}
          />
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-y border-ink/10 text-[11px] uppercase tracking-wider text-ink-3">
                <th className="px-6 py-3 font-medium">Session</th>
                <th className="py-3 font-medium">Agent</th>
                <th className="py-3 text-right font-medium">Requests</th>
                <th className="py-3 text-right font-medium">Tokens saved</th>
                <th className="px-6 py-3 text-right font-medium">Duration</th>
              </tr>
            </thead>
            <tbody>
              {!sessions.length && (
                <tr>
                  <td colSpan={5} className="px-6 py-3.5 text-sm text-ink-3">
                    {loaded
                      ? "No recorded proxy activity in this period."
                      : loading
                        ? "Loading recorded activity…"
                        : "Activity is unavailable."}
                  </td>
                </tr>
              )}
              {sessions.map((s) => (
                <tr
                  key={s.id}
                  className="border-b border-ink/10 transition last:border-0 hover:bg-ink/5"
                >
                  <td
                    title={s.id}
                    className="px-6 py-3.5 font-mono text-xs text-ink-3"
                  >
                    {s.id.slice(0, 8)}
                  </td>
                  <td className="py-3.5">
                    <Badge
                      tone={s.agent === "Claude Code" ? "indigo" : "slate"}
                    >
                      {s.agent}
                    </Badge>
                  </td>
                  <td className="py-3.5 text-right tabular-nums text-ink-2">
                    {fmtCompact(s.requests)}
                  </td>
                  <td className="py-3.5 text-right font-semibold tabular-nums text-sage">
                    {fmtCompact(s.tokensSaved)}
                  </td>
                  <td className="px-6 py-3.5 text-right text-ink-3">
                    {s.minutes}m
                    {s.active && (
                      <span className="live-dot ml-2 inline-block !h-1.5 !w-1.5 align-middle" />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
