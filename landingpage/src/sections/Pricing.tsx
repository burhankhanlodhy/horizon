import { motion } from "framer-motion";
import { Check } from "lucide-react";
import { Btn, DASHBOARD_URL, EASE, Kicker } from "../components/ui";
import { cn } from "../lib/utils";

const PLANS = [
  {
    name: "FREE",
    price: "$0",
    per: "forever",
    blurb: "Your first $20 in savings every month, on us",
    features: [
      "Compression on your first $20 saved each month",
      "All provider backends",
      "Savings ledger + dashboard",
      "Never blocked: requests pass through after your cap",
    ],
    cta: "Start free",
    highlight: false,
  },
  {
    name: "Pro",
    price: "5%",
    per: "of what you save/mo",
    blurb: "Free until you save $20. Then 5%, and you keep the rest",
    features: [
      "Unlimited compression, no monthly cap",
      "Priority routing + warm cache tuning",
      "Advanced savings analytics",
      "Save $200 → pay $10, keep $190",
    ],
    cta: "Go Pro",
    highlight: true,
  },
  {
    name: "Team",
    price: "5%",
    per: "of savings + $5/seat",
    blurb: "Shared proxy, shared savings, shared visibility.",
    blurb2: "(Minimum 3 seats)",
    features: [
      "Everything in Pro",
      "Team seats + shared keys",
      "Audit log & SSO-ready auth",
      "Priority support",
    ],
    cta: "Start a team",
    highlight: false,
  },
];

export function Pricing() {
  return (
    <section id="pricing" className="scroll-mt-20 border-b border-line">
      <div className="mx-auto max-w-[96rem] px-4 py-20 sm:px-8 lg:py-24">
        <motion.div
          initial={{ opacity: 0, y: 14 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-80px" }}
          transition={{ duration: 0.6, ease: EASE }}
          className="mx-auto max-w-2xl text-center"
        >
          <Kicker className="!justify-center">03. Pricing</Kicker>
          <h2 className="mt-3 font-display text-4xl font-semibold tracking-tight sm:text-5xl">
            Pays for itself. <span className="italic text-ember">Usually in week one.</span>
          </h2>
        </motion.div>

        <div className="mt-14 grid grid-cols-1 gap-6 md:grid-cols-3">
          {PLANS.map((p, i) => (
            <motion.div
              key={p.name}
              initial={{ opacity: 0, y: 20 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, margin: "-60px" }}
              transition={{ duration: 0.55, ease: EASE, delay: i * 0.08 }}
              className={cn(p.highlight && "md:-mt-4 md:mb-4")}
            >
              <div
                className={cn(
                  "relative flex h-full flex-col p-7",
                  p.highlight
                    ? "ink-card -rotate-1 bg-ink text-paper"
                    : "rounded-sm border border-line-2 bg-card",
                )}
              >
                {p.highlight && (
                  <span
                    className="absolute -top-3 right-6 rotate-2 border border-ink bg-ember px-2.5 py-1 font-mono text-[10px] font-semibold uppercase tracking-wider text-paper"
                    style={{ borderRadius: 3 }}
                  >
                    Most popular
                  </span>
                )}
                <div
                  className={cn(
                    "font-mono text-xs uppercase tracking-wider",
                    p.highlight ? "text-ink" : "text-ink-3",
                  )}
                >
                  {p.name}
                </div>
                <div className={cn("mt-3 font-display text-5xl font-semibold tracking-tight", p.highlight ? "text-ink" : "text-ink-2")}>
                  {p.price}
                  <span className={cn("ml-1 text-base font-normal", p.highlight ? "text-ink" : "text-ink-2")}>
                    {p.per}
                  </span>
                </div>
                <p className={cn("mt-2 text-sm", p.highlight ? "text-ink" : "text-ink-2")}>
                  {p.blurb}
                  {p.blurb2 && (
                    <span className="mt-1 block">
                      {p.blurb2}
                    </span>
                  )}
                </p>
                <ul className="mt-6 flex flex-1 flex-col gap-2.5">
                  {p.features.map((f) => (
                    <li key={f} className={cn("flex items-start gap-2 text-sm", p.highlight ? "text-ink" : "text-ink-2")}>
                      <Check
                        size={15}
                        className={cn(
                          "mt-0.5 shrink-0",
                          p.highlight ? "text-ink" : "text-sage",
                        )}
                      />
                      {f}
                    </li>
                  ))}
                </ul>
                <div className="mt-7">
                  {p.highlight ? (
                    <Btn variant="paper" href={`${DASHBOARD_URL}/signup`} className="w-full !justify-center">
                      {p.cta}
                    </Btn>
                  ) : (
                    <Btn variant="ghost" href={`${DASHBOARD_URL}/signup`} className="w-full !justify-center">
                      {p.cta}
                    </Btn>
                  )}
                </div>
              </div>
            </motion.div>
          ))}
        </div>
        <p className="mt-10 text-center font-mono text-xs text-ink-3">
          no savings, no bill. we never store your prompts.
        </p>
      </div>
    </section>
  );
}
