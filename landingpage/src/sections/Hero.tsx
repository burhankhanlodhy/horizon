import { useEffect, useRef, useState } from "react";
import { animate, motion, useInView } from "framer-motion";
import { ArrowRight, Shrink, Zap } from "lucide-react";
import { Btn, DASHBOARD_URL, EASE, Squiggle } from "../components/ui";
import { fmtFull } from "../lib/utils";

const ORIGINAL = 12400;
const SHRUNK = 4480;

function TokenCount({ from, to, delay }: { from: number; to: number; delay: number }) {
  const ref = useRef<HTMLSpanElement>(null);
  const inView = useInView(ref, { once: true, margin: "-40px" });
  const [v, setV] = useState(from);

  useEffect(() => {
    if (!inView) return;
    const controls = animate(from, to, {
      duration: 2.1,
      ease: EASE,
      delay,
      onUpdate: (x) => setV(Math.round(x)),
    });
    return () => controls.stop();
  }, [inView, from, to, delay]);

  return (
    <span ref={ref} className="tabular-nums">
      {fmtFull(v)} tok
    </span>
  );
}

function TerminalDemo() {
  return (
    <motion.div
      initial={{ opacity: 0, y: 24, rotate: 2.5 }}
      animate={{ opacity: 1, y: 0, rotate: 1.5 }}
      transition={{ duration: 0.8, ease: EASE, delay: 0.3 }}
      className="term relative w-full max-w-md"
    >
      {/* title bar */}
      <div className="flex items-center gap-2 border-b border-[#efe7d6]/10 px-4 py-3">
        <div className="term-dots flex items-center gap-1.5">
          <span style={{ backgroundColor: "#e0653a" }} />
          <span style={{ backgroundColor: "#d9b23c" }} />
          <span style={{ backgroundColor: "#7d8f65" }} />
        </div>
        <span className="ml-2 font-mono text-[11px] text-[#efe7d6]/50">contextshrink · proxy</span>
      </div>

      <div className="p-5 font-mono text-[12.5px]">
        {/* original */}
        <div className="flex items-baseline justify-between text-[#efe7d6]/60">
          <span className="text-[10.5px] uppercase tracking-[0.14em]">original context</span>
          <TokenCount from={ORIGINAL} to={ORIGINAL} delay={0} />
        </div>
        <div className="mt-2 space-y-1.5 rounded-sm border border-[#efe7d6]/15 p-3">
          {[92, 78, 85, 64].map((w, i) => (
            <div key={i} className="h-2 rounded-sm bg-[#efe7d6]/20" style={{ width: `${w}%` }} />
          ))}
        </div>

        {/* connector */}
        <div className="my-3 flex items-center justify-center gap-2 text-[#e0824f]">
          <span className="h-px flex-1 bg-[#efe7d6]/15" />
          <Shrink size={15} />
          <span className="text-[10.5px] uppercase tracking-[0.14em]">compress</span>
          <span className="h-px flex-1 bg-[#efe7d6]/15" />
        </div>

        {/* delivered — NOW IT ACTUALLY ANIMATES 100% → 36% */}
        <div className="flex items-baseline justify-between text-[#e0824f]">
          <span className="text-[10.5px] uppercase tracking-[0.14em]">delivered to the model</span>
          <TokenCount from={ORIGINAL} to={SHRUNK} delay={0.9} />
        </div>
        <motion.div
          initial={{ width: "100%" }}
          whileInView={{ width: "36%" }}
          viewport={{ once: true }}
          transition={{ duration: 1.2, ease: EASE, delay: 0.9 }}
          className="mt-2 overflow-hidden rounded-sm border border-[#e0824f]/40 bg-[#e0824f]/10 p-3"
        >
          <div className="space-y-1.5 whitespace-nowrap">
            <div className="h-2 w-40 rounded-sm bg-[#e0824f]/50" />
            <div className="h-2 w-28 rounded-sm bg-[#e0824f]/50" />
          </div>
        </motion.div>

        {/* stamp */}
        <motion.div
          initial={{ opacity: 0, scale: 0.8, rotate: -6 }}
          whileInView={{ opacity: 1, scale: 1, rotate: -2 }}
          viewport={{ once: true }}
          transition={{ type: "spring", stiffness: 240, damping: 14, delay: 1.9 }}
          className="mt-4 inline-flex items-center gap-1.5 rounded-sm border border-dashed border-[#a3b18a]/60 px-2.5 py-1 text-[11px] text-[#a3b18a]"
        >
          <Zap size={11} /> 63.8% smaller with the answer unchanged
        </motion.div>
      </div>
    </motion.div>
  );
}



export function Hero() {
  return (
    <section className="relative overflow-hidden border-b border-line">
      <div className="paper-grid absolute inset-0" aria-hidden />
      <div className="relative mx-auto grid max-w-[96rem] grid-cols-1 items-center gap-14 px-4 pb-20 pt-14 sm:px-8 lg:grid-cols-12 lg:pb-28 lg:pt-24">
        <div className="lg:col-span-7">
          <motion.p
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, ease: EASE }}
            className="flex items-center gap-2.5 font-mono text-[11px] uppercase tracking-[0.2em] text-ink-2"
          >
            <span className="inline-block h-2 w-2 bg-ember" />
            THE CHEAPEST TOKEN IS THE ONE YOU DON'T SEND
          </motion.p>
          <motion.h1
            initial={{ opacity: 0, y: 18 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.7, ease: EASE, delay: 0.1 }}
            className="mt-5 font-display text-[clamp(2.9rem,6.5vw,4.8rem)] font-semibold leading-[1.02] tracking-tight"
          >
            Your context,
            <br />
            <span className="relative inline-block text-ember">
              <Squiggle>shrunk.</Squiggle>
            </span>
          </motion.h1>
          <motion.p
            initial={{ opacity: 0, y: 14 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.7, ease: EASE, delay: 0.22 }}
            className="mt-6 max-w-xl text-lg leading-relaxed text-ink-2"
          >
            ContextShrink sits between your AI agent and the model while compressing bulky context,
            preserving prompt caches, and showing you every token you didn't pay for. Same answers.
            Dramatically smaller bills.
          </motion.p>
          <motion.div
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.7, ease: EASE, delay: 0.34 }}
            className="mt-8 flex flex-wrap items-center gap-4"
          >
            <Btn variant="ink" href={`${DASHBOARD_URL}/signup`}>
              Get started free <ArrowRight size={15} />
            </Btn>
            <Btn variant="ghost" href="#how-it-works">
              See how it works
            </Btn>
          </motion.div>
          <motion.p
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ duration: 0.7, delay: 0.5 }}
            className="mt-5 font-mono text-xs text-ink-3"
          >
            * No vendor lock-in. Just lower bills.
          </motion.p>
        </div>
        <div className="flex justify-center lg:col-span-5 lg:justify-end">
          <TerminalDemo />
        </div>
      </div>
    </section>
  );
}
