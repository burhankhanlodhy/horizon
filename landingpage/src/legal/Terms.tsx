import { LegalPage, List, Section } from "./LegalPage";

export function Terms() {
  return (
    <LegalPage
      title="Terms of Service"
      updated="11 October 2026"
      intro={
        <p>
          These terms are the agreement between you and ContextShrink for using our
          website, dashboard, API, hosted proxy and desktop app (together, &ldquo;the
          service&rdquo;). By creating an account or using the service you accept
          them. If you use it for an organisation, you accept them on its behalf and
          confirm you may do so.
        </p>
      }
    >
      <Section title="The service">
        <p>
          ContextShrink sits between your AI coding tools and the AI providers you
          use. It compresses what your tools send, forwards the request to your
          provider, and reports the tokens and money saved. We work to keep answers
          the same, but compression changes what the model receives, so you remain
          responsible for reviewing the output your tools produce.
        </p>
        <p>
          We may improve, change or retire features. Features marked as beta or
          preview may change or stop without notice.
        </p>
      </Section>

      <Section title="Your account">
        <List>
          <li>Give accurate information and keep it current.</li>
          <li>
            Keep your password and API keys secret. You are responsible for activity
            under your account; revoke a key and tell us if you think it has leaked.
          </li>
          <li>You must be at least 16, and old enough to enter a contract where you live.</li>
        </List>
      </Section>

      <Section title="Your AI provider accounts">
        <p>
          You use your own provider accounts, logins and API keys. Your agreement with
          each provider applies to the requests we forward on your behalf, and the
          provider bills you for its usage, including the small requests cache
          keep-alive sends during a session when it is on. We are not responsible for a
          provider&rsquo;s service, pricing or decisions about your account.
        </p>
      </Section>

      <Section title="Acceptable use">
        <p>You agree not to:</p>
        <List>
          <li>break the law or a provider&rsquo;s terms through the service;</li>
          <li>
            attempt to access other accounts, probe or overload our systems, or get
            around rate limits, plan limits or security measures;
          </li>
          <li>resell or share the service or your account outside your plan;</li>
          <li>reverse engineer the hosted service, except where the law allows it.</li>
        </List>
        <p>We may suspend accounts that put the service or other users at risk.</p>
      </Section>

      <Section title="Plans, fees and billing">
        <p>
          Plans and fees are described on our{" "}
          <a href="/#pricing" className="text-ember underline">
            pricing section
          </a>{" "}
          and in your dashboard. Paid plans are billed through Stripe in US dollars. A
          fee based on savings is calculated from the savings the service measures for
          your account in each billing period; the dashboard shows how the figure is
          reached. Taxes may apply on top.
        </p>
        <p>
          You can cancel a paid plan at any time from Manage billing; it stays active
          until the end of the period already paid, and any savings fee for that
          period is still billed. If a payment fails, paid features may pause until it
          is settled. If you think a charge is wrong, contact us and we will look into
          it.
        </p>
        <p>
          We may change prices. We will tell you in advance, and the change applies
          from your next billing period.
        </p>
      </Section>

      <Section title="Your content">
        <p>
          You keep all rights to the prompts, code and responses that pass through the
          service. You give us only the permission we need to process them to run the
          service, as described in our{" "}
          <a href="/privacy.html" className="text-ember underline">
            Privacy Policy
          </a>
          . We do not use your content to train AI models.
        </p>
      </Section>

      <Section title="Our service and software">
        <p>
          The service and its software belong to ContextShrink and its licensors.
          Parts of ContextShrink are built on open-source software, which remains
          under its own licences. We give you a personal, non-transferable right to
          use the service and the desktop app under these terms.
        </p>
      </Section>

      <Section title="Availability and changes">
        <p>
          We aim to keep the service available but do not promise it will be
          uninterrupted or error-free. If the proxy is unavailable, your tools may
          need to connect to your provider directly until it returns.
        </p>
      </Section>

      <Section title="Disclaimers">
        <p>
          The service is provided &ldquo;as is&rdquo;. To the fullest extent the law
          allows, we disclaim all warranties, express or implied, including fitness
          for a particular purpose and non-infringement. Savings figures are estimates
          based on provider pricing and may differ from what your provider bills.
        </p>
      </Section>

      <Section title="Limitation of liability">
        <p>
          To the fullest extent the law allows, ContextShrink is not liable for
          indirect, incidental, special or consequential losses, or for lost profits,
          data or goodwill. Our total liability for any claim about the service is
          limited to the fees you paid us in the 12 months before the claim arose.
          Nothing in these terms limits liability that cannot be limited by law.
        </p>
      </Section>

      <Section title="Ending the agreement">
        <p>
          You can stop using the service and ask us to delete your account at any
          time. We may suspend or end your access if you break these terms or if we
          stop offering the service; where we can, we will give notice and let you
          retrieve your account information first.
        </p>
      </Section>

      <Section title="Changes to these terms">
        <p>
          We may update these terms. We will change the date at the top and, for
          significant changes, tell account holders before they take effect. Using the
          service after that means you accept the updated terms.
        </p>
      </Section>

      <Section title="Governing law">
        <p>
          These terms are governed by the laws of the country where ContextShrink is
          registered, and its courts have jurisdiction, unless the law where you live
          gives you the right to bring a claim there.
        </p>
      </Section>

      <Section title="Contact">
        <p>
          Questions about these terms? Reach us through our{" "}
          <a href="/contact.html" className="text-ember underline">
            Contact page
          </a>
          .
        </p>
      </Section>
    </LegalPage>
  );
}
