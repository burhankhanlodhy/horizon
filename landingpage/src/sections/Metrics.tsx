import { motion } from "framer-motion";
import { CountUp, EASE } from "../components/ui";
import { fmtStat } from "../lib/utils";

const ROWS = [
  { label: "avg context compression", value: 63.8, suffix: "%" },
  { label: "tokens saved in evals", value: 222, suffix: "M+" },
  { label: "cache hit rate kept", value: 92.4, suffix: "%" },
  { label: "median proxy overhead", value: 4, suffix: " ms" },
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
            receipt no. 0042 from our eval suite
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
            <span className="uppercase tracking-wide text-ember">answers changed</span>
            <span
              className="mx-3 flex-1 border-b-2 border-dotted border-line-2"
              style={{ transform: "translateY(-4px)" }}
              aria-hidden
            />
            <span className="tabular-nums text-base font-semibold text-ember">0</span>
          </div>
          <p className="mt-6 text-center text-[11px] text-ink-3">
            thank you for not paying for tokens
          </p>
        </motion.div>
      </div>
    </section>
  );
}
