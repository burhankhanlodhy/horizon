import { Link } from "react-router-dom";
import { SiteFooter, SiteHeader } from "../components/SiteChrome";
import { ButtonLink, CodeCommand, Kicker, cx } from "../components/ui";
import { DEVICE_LIMIT, INSTALL_COMMANDS, PRICE_LINE, USAGE_CLAIM } from "../lib/constants";

const SECTION = "scroll-mt-20 px-4 py-16 sm:px-6 sm:py-24";
const INNER = "mx-auto max-w-6xl";

export default function Landing() {
  return (
    <div className="flex min-h-screen flex-col">
      <SiteHeader anchors />
      <main className="flex-1">
        <Hero />
        <HowItWorks />
        <Privacy />
        <Install />
        <Pricing />
        <Faq />
        <ClosingCta />
      </main>
      <SiteFooter />
    </div>
  );
}

function Hero() {
  return (
    <section className="px-4 pb-16 pt-12 sm:px-6 sm:pb-24 sm:pt-20">
      <div className={cx(INNER, "grid gap-12 md:grid-cols-[1.15fr_0.85fr] md:items-center")}>
        <div className="min-w-0">
          <Kicker>ContextShrink Code</Kicker>
          <h1 className="mt-5 font-display text-[2.35rem] font-bold leading-[1.04] tracking-tight text-ink sm:text-6xl">
            Get more usage from the Claude and ChatGPT plans you already pay for.
          </h1>
          <p className="mt-6 max-w-xl text-base leading-relaxed text-ink-2 sm:text-lg">
            ContextShrink trims what Claude Code and Codex resend every turn, so each 5-hour window goes
            further.
          </p>
          <div className="mt-8 flex flex-col gap-3 sm:flex-row sm:flex-wrap">
            <ButtonLink to="/signup" className="w-full sm:w-auto">
              Start your free trial
            </ButtonLink>
            <ButtonLink href="#install" variant="ghost" className="w-full sm:w-auto">
              Install commands
            </ButtonLink>
          </div>
          <p className="mt-5 text-sm text-ink-3">7 days free. No card needed. Windows first.</p>
          {USAGE_CLAIM && <p className="mt-2 font-mono text-sm text-ember-2">{USAGE_CLAIM}</p>}
        </div>

        <TerminalPreview />
      </div>
    </section>
  );
}

/** Decorative sample of the install flow. The real steps are in the install section. */
function TerminalPreview() {
  return (
    <div className="codeblock min-w-0 p-5 sm:p-6" aria-hidden="true">
      <div className="mb-4 flex items-center gap-1.5">
        <span className="h-2.5 w-2.5 rounded-full bg-code-ink/30" />
        <span className="h-2.5 w-2.5 rounded-full bg-code-ink/30" />
        <span className="h-2.5 w-2.5 rounded-full bg-code-ink/30" />
      </div>
      <div className="flex flex-col gap-2.5 font-mono text-[13px] leading-relaxed sm:text-sm">
        <span>
          <span className="opacity-60">$ </span>npx contextshrink-claude
        </span>
        <span className="opacity-70">Opening your browser to approve this computer…</span>
        <span className="text-sage">✓ Approved. Free trial: 7 days left.</span>
        <span className="text-sage">✓ Engine running. Claude Code is connected.</span>
        <span className="mt-1">
          <span className="opacity-60">$ </span>claude
        </span>
        <span className="opacity-70">Compression on. Requests go straight to your provider.</span>
      </div>
    </div>
  );
}

function HowItWorks() {
  const steps = [
    {
      n: "01",
      title: "Install",
      body: "Run one command in a terminal: npx contextshrink-claude for Claude Code, or npx contextshrink-codex for Codex.",
    },
    {
      n: "02",
      title: "Approve",
      body: "Your browser opens. Sign in, check the computer name, and approve it with one click. Then go back to your terminal.",
    },
    {
      n: "03",
      title: "Keep working",
      body: "Use Claude Code or Codex as you always do. Compression runs on your computer, so each turn resends less context.",
    },
  ];
  return (
    <section id="how" className={SECTION}>
      <div className={INNER}>
        <Kicker>How it works</Kicker>
        <h2 className="mt-4 font-display text-3xl font-bold tracking-tight text-ink sm:text-4xl">
          Three steps. Then it stays out of your way.
        </h2>
        <ol className="mt-12 grid gap-6 md:grid-cols-3">
          {steps.map((s) => (
            <li key={s.n} className="ink-card rounded-lg p-6">
              <div className="font-mono text-xs tracking-[0.2em] text-ember-2">{s.n}</div>
              <h3 className="mt-3 font-display text-xl font-semibold text-ink">{s.title}</h3>
              <p className="mt-2 text-[15px] leading-relaxed text-ink-2">{s.body}</p>
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}

function Privacy() {
  return (
    <section id="privacy" className={cx(SECTION, "border-y border-line bg-paper-2/60")}>
      <div className={INNER}>
        <div className="grid gap-10 md:grid-cols-[1fr_1.05fr] md:gap-16">
          <div>
            <Kicker>Privacy</Kicker>
            <h2 className="mt-4 font-display text-3xl font-bold tracking-tight text-ink sm:text-4xl">
              Your prompts never leave your computer.
            </h2>
            <p className="mt-5 text-base leading-relaxed text-ink-2 sm:text-lg">
              Compression runs on your machine; requests go straight to Anthropic or OpenAI. We never see
              or store your prompts, code or Claude/ChatGPT login. We only receive counts.
            </p>
            <Link
              to="/privacy"
              className="mt-6 inline-block text-sm font-medium text-ember-2 underline-offset-4 hover:underline"
            >
              Read the plain-language privacy page
            </Link>
          </div>

          <div className="grid gap-5">
            <div className="paper-panel rounded-lg p-5 sm:p-6">
              <h3 className="font-mono text-[12px] uppercase tracking-[0.16em] text-sage">
                Stays on your computer
              </h3>
              <ul className="mt-4 space-y-2.5 text-[15px] text-ink-2">
                <li>Your prompts and the model's responses</li>
                <li>Your code and files</li>
                <li>Your Claude or ChatGPT login</li>
                <li>Model names and file paths</li>
              </ul>
            </div>
            <div className="paper-panel rounded-lg p-5 sm:p-6">
              <h3 className="font-mono text-[12px] uppercase tracking-[0.16em] text-ember-2">
                What we receive
              </h3>
              <ul className="mt-4 space-y-2.5 text-[15px] text-ink-2">
                <li>
                  Device registration: a one-way hash of the machine ID, plus the computer's name and
                  platform
                </li>
                <li>Your plan status: trial, subscribed, or not</li>
                <li>Hourly counts of requests and input tokens, before and after compression</li>
              </ul>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function Install() {
  return (
    <section id="install" className={SECTION}>
      <div className={INNER}>
        <Kicker>Install</Kicker>
        <h2 className="mt-4 font-display text-3xl font-bold tracking-tight text-ink sm:text-4xl">
          Run the command for your tool.
        </h2>
        <p className="mt-4 max-w-2xl text-base leading-relaxed text-ink-2">
          Both commands share one connection to your account, so adding the second tool is quick. Windows
          first. macOS and Linux are coming. You need Node.js, because the commands run through npx.
        </p>
        <div className="mt-10 grid gap-5 md:grid-cols-2">
          {INSTALL_COMMANDS.map((c) => (
            <CodeCommand key={c.id} label={c.label} command={c.command} />
          ))}
        </div>
      </div>
    </section>
  );
}

function Pricing() {
  const points = [
    "No card to start the trial.",
    "Works with Claude Code and Codex.",
    "When the trial or subscription ends, Claude and Codex keep working without compression.",
    `Subscribe, cancel, or swap computers from your account page. Up to ${DEVICE_LIMIT} computers.`,
  ];
  return (
    <section id="pricing" className={SECTION}>
      <div className={INNER}>
        <div className="grid gap-10 md:grid-cols-[1fr_1.1fr] md:items-start">
          <div>
            <Kicker>Pricing</Kicker>
            <h2 className="mt-4 font-display text-3xl font-bold tracking-tight text-ink sm:text-4xl">
              One plan. No surprises.
            </h2>
          </div>
          <div className="ink-card rounded-lg p-6 sm:p-8">
            <p className="font-display text-2xl font-semibold leading-snug text-ink sm:text-[28px]">
              {PRICE_LINE}
            </p>
            <ul className="mt-6 space-y-3 text-[15px] leading-relaxed text-ink-2">
              {points.map((p) => (
                <li key={p} className="flex gap-3">
                  <span aria-hidden="true" className="mt-2 h-1.5 w-1.5 shrink-0 bg-ember" />
                  <span>{p}</span>
                </li>
              ))}
            </ul>
            <ButtonLink to="/signup" className="mt-8 w-full sm:w-auto">
              Start your free trial
            </ButtonLink>
          </div>
        </div>
      </div>
    </section>
  );
}

const FAQS: Array<{ q: string; a: string }> = [
  {
    q: "What is sent to ContextShrink?",
    a: "Only counts. That is a one-way hash that identifies the computer, the computer's name and platform, your plan status, and hourly totals of requests and input tokens before and after compression. Never prompts, responses, code, file paths, model names, or your Claude or ChatGPT login.",
  },
  {
    q: "What happens when the trial ends?",
    a: "Claude Code and Codex keep working. ContextShrink stops compressing and passes your requests through unchanged, and nothing errors. Subscribe at any time to turn compression back on.",
  },
  {
    q: "How do I move to a new computer?",
    a: "On the old computer, run npx contextshrink-claude uninstall (or npx contextshrink-codex uninstall). Or open your account page and click Disconnect next to that computer. Then install on the new one. Each account can have up to 2 computers connected.",
  },
  {
    q: "Which computers are supported?",
    a: "Windows only for now. macOS and Linux are coming.",
  },
  {
    q: "Does this replace my Claude or ChatGPT plan?",
    a: "No. You keep your plan and your login. ContextShrink runs next to your tool on your computer and sends requests on to your provider. The only charge from us is the $5/month subscription, after the free trial.",
  },
  {
    q: "Can I cancel?",
    a: "Yes, at any time from your account page. Compression stays on until the period you paid for ends.",
  },
];

function Faq() {
  return (
    <section id="faq" className={SECTION}>
      <div className={INNER}>
        <div className="grid gap-10 md:grid-cols-[1fr_1.6fr] md:gap-16">
          <div>
            <Kicker>FAQ</Kicker>
            <h2 className="mt-4 font-display text-3xl font-bold tracking-tight text-ink sm:text-4xl">
              Questions, answered plainly.
            </h2>
          </div>
          <div className="divide-y divide-line border-y border-line">
            {FAQS.map((f) => (
              <details key={f.q} className="faq group py-5">
                <summary className="flex cursor-pointer items-start justify-between gap-6 text-left font-medium text-ink">
                  <span className="text-[16px] leading-snug sm:text-[17px]">{f.q}</span>
                  <span
                    aria-hidden="true"
                    className="faq-icon mt-0.5 shrink-0 font-mono text-xl leading-none text-ember transition-transform"
                  >
                    +
                  </span>
                </summary>
                <p className="mt-3 pr-8 text-[15px] leading-relaxed text-ink-2">{f.a}</p>
              </details>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}

function ClosingCta() {
  return (
    <section className="px-4 pb-20 sm:px-6 sm:pb-28">
      <div className={cx(INNER, "ink-card flex flex-col items-start gap-6 rounded-lg p-7 sm:flex-row sm:items-center sm:justify-between sm:p-10")}>
        <div>
          <h2 className="font-display text-2xl font-bold text-ink sm:text-3xl">Try it on your next session.</h2>
          <p className="mt-2 text-[15px] text-ink-2">7 days free, then $5/month. Cancel anytime.</p>
        </div>
        <ButtonLink to="/signup" className="w-full sm:w-auto">
          Start your free trial
        </ButtonLink>
      </div>
    </section>
  );
}
