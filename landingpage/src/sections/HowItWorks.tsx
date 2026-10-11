import { motion } from "framer-motion";
import { Bot, Cloud, Shrink } from "lucide-react";
import { CodeBlock, EASE, Kicker } from "../components/ui";
import { cn } from "../lib/utils";

function DiagramNode({
  icon: Icon,
  label,
  sub,
  highlight = false,
}: {
  icon: typeof Bot;
  label: string;
  sub: string;
  highlight?: boolean;
}) {
  return (
    <div
      className={cn(
        "flex w-full flex-col items-center gap-1.5 px-4 py-5 text-center sm:w-44",
        highlight ? "ink-card" : "rounded-sm border border-line-2 bg-paper",
      )}
    >
      <Icon size={22} className={highlight ? "text-ember" : "text-ink"} />
      <div className="font-display text-sm font-semibold">{label}</div>
      <div className="font-mono text-[10.5px] text-ink-3">{sub}</div>
    </div>
  );
}

function Connector({ delay }: { delay: number }) {
  return (
    <div className="relative hidden h-px flex-1 border-t border-dashed border-line-2 sm:block">
      <motion.span
        className="absolute -top-[3px] h-1.5 w-1.5 rounded-full bg-ember"
        animate={{ left: ["0%", "100%"], opacity: [0, 1, 1, 0] }}
        transition={{ duration: 1.8, repeat: Infinity, ease: "easeInOut", delay }}
      />
    </div>
  );
}

export function HowItWorks() {
  return (
    <section id="how-it-works" className="scroll-mt-20 border-b border-line">
      <div className="mx-auto max-w-[96rem] px-4 py-20 sm:px-8 lg:py-24">
        <motion.div
          initial={{ opacity: 0, y: 14 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-80px" }}
          transition={{ duration: 0.6, ease: EASE }}
        >
          <Kicker>02. How it works</Kicker>
          <h2 className="mt-3 max-w-md font-display text-4xl font-semibold tracking-tight sm:text-5xl">
            One proxy. <span className="italic text-ember">Three</span> moving parts.
          </h2>
        </motion.div>

        <motion.div
          initial={{ opacity: 0, y: 18 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-60px" }}
          transition={{ duration: 0.6, ease: EASE, delay: 0.15 }}
          className="mt-12 flex flex-col items-center gap-0 rounded-sm border border-line-2 bg-card p-6 sm:flex-row sm:items-center sm:gap-3 sm:p-8"
        >
          <DiagramNode
            icon={Bot}
            label="Your agent"
            sub="Claude Code, OpenCode, Cursor…"
          />
          <div className="my-2 h-8 border-l border-dashed border-line-2 sm:hidden" />
          <Connector delay={0} />
          <DiagramNode icon={Shrink} label="ContextShrink" sub="compress · cache · ledger" highlight />
          <div className="my-2 h-8 border-l border-dashed border-line-2 sm:hidden" />
          <Connector delay={0.9} />
          <DiagramNode icon={Cloud} label="Provider" sub="Anthropic, OpenAI, Vertex…" />
        </motion.div>

        <motion.p
          initial={{ opacity: 0, y: 12 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-60px" }}
          transition={{ duration: 0.5, ease: EASE, delay: 0.25 }}
          className="mx-auto mt-6 max-w-2xl border-l-2 border-ember pl-4 text-sm leading-relaxed text-ink-2"
        >
          Requests flow through ContextShrink: bulky content is compressed and stored, cache
          prefixes stay frozen, and the savings ledger records every token saved. Your agent can
          retrieve the full original with one MCP call.
        </motion.p>

        <div className="mt-8 grid grid-cols-1 gap-5 md:grid-cols-2">
          <motion.div
            initial={{ opacity: 0, y: 18 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true, margin: "-40px" }}
            transition={{ duration: 0.5, ease: EASE, delay: 0.1 }}
          >
            <p className="mb-3 font-mono text-[11px] uppercase tracking-[0.14em] text-ink-3">
              step 1: Sign up for a Plan
            </p>
            <CodeBlock code="Sign up for a Plan of your Choice" />
          </motion.div>
          <motion.div
            initial={{ opacity: 0, y: 18 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true, margin: "-40px" }}
            transition={{ duration: 0.5, ease: EASE, delay: 0.2 }}
          >
            <p className="mb-3 font-mono text-[11px] uppercase tracking-[0.14em] text-ink-3">
              step 2: connect your agent
            </p>
            <CodeBlock code="Download the Installer and Start using any coding agent " />
          </motion.div>
        </div>
      </div>
    </section>
  );
}
