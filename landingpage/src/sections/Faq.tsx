import { useState } from "react";
import { motion } from "framer-motion";
import { Plus } from "lucide-react";
import { EASE, Kicker } from "../components/ui";
import { cn } from "../lib/utils";

const ITEMS = [
  {
    q: "Will compression change my agent's answers?",
    a: "ContextShrink is conservative by design: bulky, low-relevance content (logs, dumps, old tool output) is summarized and shelved with a marker; instructions, code you're editing and the live conversation stay intact. Anything compressed can be pulled back verbatim with one retrieve call.",
  },
  {
    q: "Doesn't compression destroy prompt caching?",
    a: "Not here. Cache preservation is a first-class constraint: stable prefixes are frozen, breakpoints are placed deliberately, and compression that would invalidate a warm cache is skipped. In our measured sessions the prompt-cache hit rate stayed around 90%.",
  },
  {
    q: "Where does my code go?",
    a: "Through our proxy, then straight to your AI provider. Requests are encrypted in transit, compressed in memory, and forwarded to the model you chose. Your prompts and responses never go into our logs or analytics: usage records hold the model, token counts and savings. Content is held only briefly so the service works, such as the original of anything we shorten (about 30 minutes) so the model can fetch it back. Our Privacy Policy has the details.",
  },
  {
    q: "Which agents and SDKs are supported?",
    a: "Anything that speaks the Anthropic Messages API, the OpenAI Chat Completions API, or Google Vertex — plus first-class wrap scripts for Claude Code, OpenCode, Codex, Cursor and more. LangChain, CrewAI and MCP integrations are included.",
  },
  {
    q: "How do I know it's actually saving money?",
    a: "Every request lands in a durable savings ledger: original tokens, delivered tokens, cache savings and a counterfactual cost estimate. The dashboard and Prometheus metrics make the number impossible to ignore.",
  },
];

function Item({ q, a, open, onToggle }: { q: string; a: string; open: boolean; onToggle: () => void }) {
  return (
    <div className="border-b border-line-2">
      <button onClick={onToggle} className="flex w-full items-center gap-4 py-5 text-left">
        <span className="flex-1 font-display text-lg font-semibold tracking-tight">{q}</span>
        <motion.span
          animate={{ rotate: open ? 45 : 0 }}
          transition={{ duration: 0.25, ease: EASE }}
        >
          <Plus size={18} className="text-ember" />
        </motion.span>
      </button>
      <div className={cn("faq-answer", open && "open")}>
        <div>
          <p className="max-w-2xl pb-6 text-sm leading-relaxed text-ink-2">{a}</p>
        </div>
      </div>
    </div>
  );
}

export function Faq() {
  const [openIdx, setOpenIdx] = useState<number>(0);
  return (
    <section id="faq" className="scroll-mt-20">
      <div className="mx-auto max-w-4xl px-4 py-20 sm:px-8 lg:py-24">
        <motion.div
          initial={{ opacity: 0, y: 14 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-80px" }}
          transition={{ duration: 0.6, ease: EASE }}
          className="text-center"
        >
          <Kicker className="!justify-center">04 — faq</Kicker>
          <h2 className="mt-3 font-display text-4xl font-semibold tracking-tight sm:text-5xl">
            Fair questions, <span className="italic text-ember">straight answers.</span>
          </h2>
        </motion.div>
        <div className="mt-12 border-t border-line-2">
          {ITEMS.map((item, i) => (
            <Item
              key={item.q}
              q={item.q}
              a={item.a}
              open={openIdx === i}
              onToggle={() => setOpenIdx(openIdx === i ? -1 : i)}
            />
          ))}
        </div>
      </div>
    </section>
  );
}
