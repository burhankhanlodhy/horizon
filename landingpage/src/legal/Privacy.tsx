import { LegalPage, List, Section, Strong } from "./LegalPage";

/*
 * Keep this page true to what the service does. Each retention figure below
 * matches a setting in the code: the CCR store's 30-minute TTL
 * (horizon/cache/compression_store.py), the response cache's one-hour TTL and
 * per-account partition (horizon/proxy/semantic_cache.py), the prefix tracker
 * living for the provider's cache lifetime (horizon/cache/prefix_tracker.py),
 * and the keep-alive's in-memory session (horizon/proxy/cache_keeper.py).
 * Email link lifetimes come from api/account_email.py (30 minutes / 7 days);
 * backup retention from the S3 lifecycle rule on contextshrink-weekly-backup
 * (deploy/backup/README.md). Change the page whenever one of those changes.
 */

export function Privacy() {
  return (
    <LegalPage
      title="Privacy Policy"
      updated="11 October 2026"
      intro={
        <p>
          ContextShrink compresses the context your AI coding tools send, so you pay for
          fewer tokens and get the same answers. This policy explains what information
          we handle to do that, how long we keep it, who else sees it, and the choices
          you have. In short: your prompts and responses are never written to our logs
          or analytics, and the little content we hold, we hold briefly and only to
          make the service work.
        </p>
      }
    >
      <Section title="Who we are">
        <p>
          ContextShrink (&ldquo;we&rdquo;, &ldquo;us&rdquo;), a company registered in
          the State of Illinois, United States, operates the website at
          contextshrink.com, the dashboard at app.contextshrink.com, the account API
          at api.contextshrink.com, the hosted compression proxy at
          proxy.contextshrink.com, and the ContextShrink desktop app. Our servers are
          in the United States. Questions about this policy can reach us through our{" "}
          <a href="/contact.html" className="text-ember underline">
            Contact page
          </a>
          .
        </p>
      </Section>

      <Section title="What we collect">
        <p>
          <Strong>Your account.</Strong> Your name, email address and password. We
          store the password only as an Argon2 hash, never in readable form. Sign-in
          sessions and API keys are stored as one-way hashes too; we keep the first
          few characters of each key so you can tell them apart.
        </p>
        <p>
          <Strong>Your plan and billing.</Strong> Your plan, its status and billing
          period, and the identifiers Stripe gives us for your customer record,
          subscription and invoices. Card details go to Stripe directly and never
          reach our servers.
        </p>
        <p>
          <Strong>Usage records.</Strong> For each request that passes through the
          proxy we record when it happened, the model and provider, the project
          label and tool your setup sends, the response status, token counts
          (including cached tokens), the tokens saved, estimated costs and savings,
          and timings. These records power your dashboard, your savings figures and
          billing. They never contain the text of your prompts or responses.
        </p>
        <p>
          <Strong>What you send through the proxy.</Strong> Your prompts, files,
          tool output and the model&rsquo;s responses pass through the proxy so we can
          compress them and forward them to the AI provider you chose. This content is
          not written to our logs or analytics. To make the service work, some of it
          is held briefly:
        </p>
        <List>
          <li>
            When we shorten bulky content, we keep the original for about 30 minutes
            so the model can ask for it back in full. It is stored on our server and
            deleted when that time runs out.
          </li>
          <li>
            A response may be kept in memory for up to one hour, so an identical
            repeat request can be answered without charging you again. This cache is
            separate for each account; one account never receives another&rsquo;s
            responses.
          </li>
          <li>
            While a session is active, the proxy keeps your most recent request in
            memory so later requests stay identical to what your provider has cached,
            for as long as that provider cache lasts (at most about an hour of
            inactivity).
          </li>
        </List>
        <p>
          <Strong>Your AI provider credentials.</Strong> Your provider API key or
          login travels with each request so the proxy can forward it. We hold it in
          memory only for the duration of your session and never write it to disk or
          to logs. If cache keep-alive is on, the proxy uses it during your session to
          send small requests that keep your provider&rsquo;s prompt cache warm; their
          cost is billed by your provider and shown in your dashboard.
        </p>
        <p>
          <Strong>Account emails.</Strong> When you sign up or reset your password we
          email you a link. We store only a one-way hash of each link, and it stops
          working after 30 minutes (password reset) or 7 days (email confirmation).
        </p>
        <p>
          <Strong>Contact messages.</Strong> When you use the Contact page we keep the
          name, email address, topic and message you enter. We do not store your IP
          address with it.
        </p>
        <p>
          <Strong>Security checks.</Strong> The signup and contact forms use
          Cloudflare Turnstile to tell people from bots. It processes your IP address
          and signals from your browser for that check only.
        </p>
        <p>
          <Strong>Desktop app updates.</Strong> The desktop app checks
          app.contextshrink.com for a newer version when it starts and every six hours.
          That request carries your IP address, like any web request, but nothing about
          your account or your work.
        </p>
        <p>
          <Strong>Operational logs.</Strong> Our servers keep technical logs (request
          identifiers, times, status codes, model names and error descriptions) to run
          and debug the service. They do not contain prompt or response text and are
          overwritten as they rotate.
        </p>
      </Section>

      <Section title="How we use it">
        <List>
          <li>To run the service: compress requests, forward them, and keep your account signed in.</li>
          <li>To show your usage and savings, and to bill the plan you chose.</li>
          <li>To keep the service secure and reliable, and to stop abuse.</li>
          <li>To answer you when you contact us.</li>
        </List>
        <p>
          We do not sell your information, use it for advertising, or use your prompts,
          code or responses to train AI models.
        </p>
      </Section>

      <Section title="Who else receives it">
        <List>
          <li>
            <Strong>The AI providers you use</Strong> (for example Anthropic, OpenAI,
            Google, xAI). We forward your compressed requests to the provider you
            choose; what they do with it is governed by your agreement with them.
          </li>
          <li>
            <Strong>Stripe</Strong>, which processes payments for paid plans.
          </li>
          <li>
            <Strong>Cloudflare</Strong>, which hosts our website, dashboard and app
            downloads, carries traffic to our servers, protects them, and runs the
            Turnstile security check. It processes your IP address and request data in
            transit.
          </li>
          <li>
            <Strong>Amazon Web Services</Strong>, which sends our account emails
            (Amazon SES) and stores our encrypted database backups (Amazon S3, in the
            United States).
          </li>
          <li>
            <Strong>Google Fonts</Strong>, which serves the fonts on our website and
            dashboard and so receives your IP address when a page loads.
          </li>
        </List>
        <p>
          We may also disclose information when the law requires it, or to protect our
          users, the service or others from harm.
        </p>
      </Section>

      <Section title="Cookies and tracking">
        <p>
          The website sets no cookies of its own and uses no analytics or advertising
          trackers. The
          dashboard keeps your sign-in session in your browser&rsquo;s local storage,
          which you can clear by signing out. The desktop app stores its device key in
          your operating system&rsquo;s credential store and sends no usage telemetry
          of its own.
        </p>
      </Section>

      <Section title="How long we keep it">
        <List>
          <li>Account, plan and usage records: while your account exists.</li>
          <li>Billing records: as long as tax and accounting law requires.</li>
          <li>Proxy content: as described above, from minutes up to about an hour.</li>
          <li>Contact messages: until your question is resolved, then as long as useful for follow-up.</li>
          <li>Operational logs: a short period, after which they are overwritten.</li>
          <li>
            Database backups: taken daily, encrypted, and kept for up to 90 days. When
            you delete your account, its information leaves our backups as they
            expire.
          </li>
        </List>
      </Section>

      <Section title="Security">
        <p>
          Traffic to our services is encrypted in transit. Passwords, sessions and API
          keys are stored only as hashes, and access to our servers is restricted. No
          system is perfectly secure, so please use a strong, unique password and
          revoke API keys you no longer need.
        </p>
      </Section>

      <Section title="Your choices and rights">
        <p>
          You can see your usage on the dashboard and revoke API keys at any time. You
          can ask us to give you a copy of your information, correct it, or delete
          your account and the information tied to it. Depending on where you live,
          you may have further rights, such as objecting to certain processing or
          complaining to your data protection authority. To use any of these rights,
          write to us through the{" "}
          <a href="/contact.html" className="text-ember underline">
            Contact page
          </a>{" "}
          with the topic &ldquo;Privacy&rdquo;. We answer within 30 days, and we may
          need to confirm it is really you before acting.
        </p>
        <p>
          We do not sell or share your personal information for advertising, as those
          terms are defined in US state privacy laws such as California&rsquo;s, and
          we will not treat you differently for using your rights.
        </p>
      </Section>

      <Section title="International transfers">
        <p>
          ContextShrink is based in the United States and our servers are there. If
          you use the service from another country, your information is transferred to
          and processed in the United States, and the providers listed above may
          process it in other countries too. We rely on those providers&rsquo;
          safeguards for such transfers.
        </p>
      </Section>

      <Section title="If something goes wrong">
        <p>
          If a security incident affects your personal information, we will tell you
          and the relevant authorities as the law requires, including the Illinois
          Personal Information Protection Act.
        </p>
      </Section>

      <Section title="Children">
        <p>
          ContextShrink is a tool for developers and is not directed at children under
          16. We do not knowingly collect their information.
        </p>
      </Section>

      <Section title="Changes to this policy">
        <p>
          If we change this policy, we will update the date at the top of this page,
          and for significant changes we will tell account holders by email or on the
          dashboard before they take effect.
        </p>
      </Section>
    </LegalPage>
  );
}
