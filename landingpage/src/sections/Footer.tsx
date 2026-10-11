import { motion } from "framer-motion";
import { ArrowRight } from "lucide-react";
import { Btn, DASHBOARD_URL, EASE, LEGAL_LINKS, LogoMark, Wordmark } from "../components/ui";

export function Footer() {
  return (
    <footer>
      <section className="bg-ink text-paper">
        <div className="mx-auto max-w-[96rem] px-4 py-20 text-center sm:px-8 lg:py-28">
          <motion.div
            initial={{ opacity: 0, y: 18 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true, margin: "-80px" }}
            transition={{ duration: 0.6, ease: EASE }}
          >
            <p className="font-mono text-[11px] uppercase tracking-[0.25em] text-paper/50">
              ready when you are
            </p>
            <h2 className="mx-auto mt-4 max-w-2xl font-display text-4xl font-semibold tracking-tight sm:text-5xl">
              Stop paying for <span className="italic text-[#e0824f]">tokens you don't need.</span>
            </h2>
            <p className="mx-auto mt-4 max-w-md text-paper/60">
              Point your agent at ContextShrink and watch the savings ledger fill up.
            </p>
            <div className="mt-8 flex flex-wrap justify-center gap-4">
              <Btn variant="paper" href={`${DASHBOARD_URL}/signup`}>
                Create your account <ArrowRight size={15} />
              </Btn>
              <Btn variant="ghost-light" href={DASHBOARD_URL}>
                Open dashboard
              </Btn>
            </div>
          </motion.div>
        </div>
      </section>
      <div className="border-t border-line">
        <div className="mx-auto flex max-w-[96rem] flex-col items-center justify-between gap-6 px-4 py-8 sm:px-8 md:flex-row">
          <a href="#" className="flex items-center gap-2.5">
            <LogoMark size={26} />
            <div className="leading-tight">
              <Wordmark />
              <div className="font-mono text-[10px] tracking-wide text-ink-3">
                same answers, fewer tokens
              </div>
            </div>
          </a>
          <nav className="flex flex-wrap items-center justify-center gap-6 font-mono text-xs text-ink-2">
            <a href="#features" className="transition hover:text-ink">
              Features
            </a>
            <a href="#pricing" className="transition hover:text-ink">
              Pricing
            </a>
            <a href="#faq" className="transition hover:text-ink">
              FAQ
            </a>
            <a href={`${DASHBOARD_URL}/docs`} className="transition hover:text-ink">
              Documentation
            </a>
            <a href={`${DASHBOARD_URL}/login`} className="transition hover:text-ink">
              Sign in
            </a>
            {LEGAL_LINKS.map((l) => (
              <a
                key={l.href}
                href={l.href}
                target="_blank"
                rel="noopener noreferrer"
                className="transition hover:text-ink"
              >
                {l.label}
              </a>
            ))}
          </nav>
          <div className="font-mono text-xs text-ink-3">© 2026 ContextShrink</div>
        </div>
      </div>
    </footer>
  );
}
