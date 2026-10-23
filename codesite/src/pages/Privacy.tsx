import type { ReactNode } from "react";
import { LANDING_URL } from "../lib/api";
import { SiteFooter, SiteHeader } from "../components/SiteChrome";
import { ButtonLink, Kicker } from "../components/ui";

export default function Privacy() {
  return (
    <div className="flex min-h-screen flex-col">
      <SiteHeader />
      <main className="flex-1 px-4 pb-20 pt-10 sm:px-6 sm:pt-16">
        <article className="mx-auto max-w-3xl">
          <Kicker>Privacy</Kicker>
          <h1 className="mt-4 font-display text-3xl font-bold tracking-tight text-ink sm:text-5xl">
            Privacy, in plain language
          </h1>
          <p className="mt-5 text-base leading-relaxed text-ink-2 sm:text-lg">
            ContextShrink Code does its work on your computer. This page says what leaves it. It is a
            summary; the full legal policy is at{" "}
            <a href={`${LANDING_URL}/privacy.html`} className="text-ember-2 underline-offset-4 hover:underline">
              contextshrink.com
            </a>
            .
          </p>

          <Section title="What stays on your computer">
            <ul>
              <li>Your prompts, the model's responses, and your code and files.</li>
              <li>Your Claude or ChatGPT login and any provider credentials.</li>
              <li>Model names and file paths.</li>
            </ul>
            <p>
              Compression runs in a small local engine. Requests go straight from that engine to Anthropic or
              OpenAI. ContextShrink's servers are not in the path.
            </p>
          </Section>

          <Section title="What we receive">
            <ul>
              <li>
                Device registration: a one-way hash of the computer's machine ID (the ID itself is never sent),
                plus the computer's name and platform.
              </li>
              <li>Plan status: whether you are on the free plan (and whether this month's free week is running), subscribed, or past due.</li>
              <li>
                Hourly counts: requests, input tokens before compression, input tokens after compression, and
                which tool made the request.
              </li>
            </ul>
            <p>That is the complete list. We never see or store prompts, responses, code, or logins.</p>
          </Section>

          <Section title="Your account">
            <p>
              Your name and email are stored with your ContextShrink account. It is the same account you use at
              app.contextshrink.com. Payments are handled by Stripe, and we never see your card number.
            </p>
          </Section>

          <Section title="Removing a computer">
            <p>
              Run <code className="font-mono text-[0.95em]">npx contextshrink-claude uninstall</code> or{" "}
              <code className="font-mono text-[0.95em]">npx contextshrink-codex uninstall</code> on that computer.
              Or open your account page and click Disconnect next to it. Disconnecting stops that computer from
              sending counts and frees its slot.
            </p>
          </Section>

          <Section title="Questions">
            <p>
              Ask us through the{" "}
              <a href={`${LANDING_URL}/contact.html`} className="text-ember-2 underline-offset-4 hover:underline">
                contact page
              </a>
              .
            </p>
          </Section>

          <div className="mt-12">
            <ButtonLink to="/signup" variant="ink">
              Create free account
            </ButtonLink>
          </div>
        </article>
      </main>
      <SiteFooter />
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mt-10 border-t border-line pt-8">
      <h2 className="font-display text-xl font-semibold text-ink sm:text-2xl">{title}</h2>
      <div className="mt-4 space-y-4 text-[15px] leading-relaxed text-ink-2 sm:text-base [&_ul]:list-disc [&_ul]:space-y-2 [&_ul]:pl-5">
        {children}
      </div>
    </section>
  );
}
