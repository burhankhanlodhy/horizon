import { motion } from "framer-motion";
import { BookOpenCheck, Database, Gauge, GitBranch, Lock, Wrench } from "lucide-react";
import { EASE, Idx, Kicker } from "../components/ui";

const FEATURES = [
  {
    icon: Gauge,
    title: "Transparent compression",
    body: "Bulky tool output, logs, docs and diffs are compressed en route to the model. Your agent keeps its workflow and the context just gets smaller.",
  },
  {
    icon: Database,
    title: "Cache-preserving by design",
    body: "Prefixes are frozen and breakpoints are managed so provider prompt caches stay warm. Compression that would nuke your cache hit rate doesn't ship.",
  },
  {
    icon: GitBranch,
    title: "Retrieve on demand",
    body: "Compressed content isn't lost but it's shelved. Every summary carries a marker; the original is one MCP tool call away (horizon_retrieve).",
  },
  {
    icon: Wrench,
    title: "Provider-agnostic",
    body: "Anthropic, OpenAI, Vertex, Bedrock, LiteLLM, GitHub Copilot subscriptions. One proxy, every upstream, zero agent rewrites.",
  },
  {
    icon: BookOpenCheck,
    title: "Savings you can audit",
    body: "A durable ledger records every request, token saved and dollar avoided, with Prometheus metrics and a live dashboard.",
  },
  {
    icon: Lock,
    title: "Yours, entirely",
    body: "Secure by default. Privacy by design. Process and forget. No storage, no logs.",
  },
];

export function Features() {
  return (
    <section id="features" className="scroll-mt-20 border-b border-line">
      <div className="mx-auto max-w-[96rem] px-4 py-20 sm:px-8 lg:py-24">
        <div className="flex flex-col gap-6 md:flex-row md:items-end md:justify-between">
          <motion.div
            initial={{ opacity: 0, y: 14 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true, margin: "-80px" }}
            transition={{ duration: 0.6, ease: EASE }}
          >
            <Kicker>01. What it does</Kicker>
            <h2 className="mt-3 max-w-md font-display text-4xl font-semibold tracking-tight sm:text-5xl">
              Everything shrinks. <span className="italic text-ember">Nothing breaks.</span>
            </h2>
          </motion.div>
          <motion.p
            initial={{ opacity: 0, y: 14 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true, margin: "-80px" }}
            transition={{ duration: 0.6, ease: EASE, delay: 0.1 }}
            className="max-w-sm text-ink-2"
          >
            A proxy layer that respects how coding agents actually work, including caches, tools, multi-turn
            state and all.
          </motion.p>
        </div>

        <div className="mt-14 border-t border-line-2">
          {FEATURES.map((f, i) => (
            <motion.div
              key={f.title}
              initial={{ opacity: 0, y: 14 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, margin: "-40px" }}
              transition={{ duration: 0.45, ease: EASE, delay: i * 0.04 }}
              className="group -mx-3 grid grid-cols-[2.5rem_1fr] items-baseline gap-x-4 border-b border-line-2 px-3 py-6 transition-colors hover:bg-card md:grid-cols-[3.5rem_17rem_1fr_2rem] md:gap-x-6"
            >
              <Idx n={i + 1} />
              <h3 className="font-display text-xl font-semibold tracking-tight">{f.title}</h3>
              <p className="col-span-2 mt-2 max-w-2xl text-sm leading-relaxed text-ink-2 md:col-span-1 md:mt-0">
                {f.body}
              </p>
              <f.icon
                size={18}
                className="hidden -translate-x-1 text-ember opacity-0 transition-all group-hover:translate-x-0 group-hover:opacity-100 md:block"
              />
            </motion.div>
          ))}
        </div>
      </div>
    </section>
  );
}
