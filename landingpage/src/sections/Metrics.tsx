import { motion } from "framer-motion";
import { CountUp, EASE } from "../components/ui";
import { fmtStat } from "../lib/utils";

// Measured, not projected: one OpenCode session reviewing and fixing a 34k-line
// Python codebase through the proxy (Oct 2026, gpt-6.1-sol, max profile).
// Update these only from a new measured run.
const ROWS = [
  { label: "new context removed", value: 29.5, suffix: "%" },
  { label: "bill saved", value: 17.3, suffix: "%" },
  { label: "prompt-cache hit rate kept", value: 91.1, suffix: "%" },
  { label: "avg proxy overhead", value: 66, suffix: " ms" },
];

export function Metrics() {
  return (
    <section className="border-b border-line bg-card">
      <div className="mx-auto max-w-[96rem] px-4 py-20 sm:px-8 lg:py-24">
        <motion.div
          initial={{ opacity: 0, y: 20, rotate: -1.2 }}
          whileInView={{ opacity: 1, y: 0, rotate: -0.6 }}
          viewport={{ once: true, margin: "-80px" }}
          transition={{ duration: 0.6, ease: EASE }}
          className="receipt mx-auto max-w-xl p-7 sm:p-9"
          style={{ borderRadius: 3 }}
        >
          <div className="receipt-edge -mx-7 -mt-7 sm:-mx-9 sm:-mt-9" />
          <p className="mt-6 text-center text-[11px] uppercase tracking-[0.25em] text-ink-3">
            *** savings ledger ***
          </p>
          <p className="mt-1 text-center text-[11px] text-ink-3">
            receipt no. 0042 — a real review-and-fix session
          </p>
          <div className="my-5 border-t border-dashed border-line-2" />
          <dl className="space-y-3.5">
            {ROWS.map((r) => (
              <div key={r.label} className="flex items-baseline text-sm">
                <dt className="uppercase tracking-wide text-ink-2">{r.label}</dt>
                <span
                  className="mx-3 flex-1 border-b-2 border-dotted border-line-2"
                  style={{ transform: "translateY(-4px)" }}
                  aria-hidden
                />
                <dd className="tabular-nums text-base font-semibold text-ink">
                  <CountUp value={r.value} format={(v) => fmtStat(v)} />
                  {r.suffix}
                </dd>
              </div>
            ))}
          </dl>
          <div className="my-5 border-t border-dashed border-line-2" />
          <div className="flex items-baseline text-sm">
            <span className="uppercase tracking-wide text-ember">tests passing after</span>
            <span
              className="mx-3 flex-1 border-b-2 border-dotted border-line-2"
              style={{ transform: "translateY(-4px)" }}
              aria-hidden
            />
            <span className="tabular-nums text-base font-semibold text-ember">157/157</span>
          </div>
          <p className="mt-6 text-center text-[11px] text-ink-3">
            thank you for not paying for tokens
          </p>
        </motion.div>
      </div>
    </section>
  );
}
