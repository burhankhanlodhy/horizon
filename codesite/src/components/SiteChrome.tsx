import { Link } from "react-router-dom";
import { LANDING_URL } from "../lib/api";
import { hasSession } from "../lib/auth";
import { ButtonLink, LogoMark, Wordmark, cx } from "./ui";

/** Top bar shared by every page. Anchor links only appear on the landing page. */
export function SiteHeader({ anchors = false }: { anchors?: boolean }) {
  const signedIn = hasSession();
  return (
    <header className="sticky top-0 z-40 border-b border-line bg-paper/95 backdrop-blur-sm">
      <div className="mx-auto flex h-16 max-w-6xl items-center gap-4 px-4 sm:gap-8 sm:px-6">
        <Link to="/" className="flex min-w-0 items-center gap-2.5" aria-label="ContextShrink Code home">
          <LogoMark size={28} />
          <Wordmark product={false} />
        </Link>

        {anchors && (
          <nav aria-label="Page sections" className="hidden items-center gap-7 md:flex">
            {[
              ["How it works", "#how"],
              ["Privacy", "#privacy"],
              ["Pricing", "#pricing"],
              ["FAQ", "#faq"],
            ].map(([label, href]) => (
              <a
                key={href}
                href={href}
                className="text-sm text-ink-2 underline-offset-4 transition hover:text-ink hover:underline hover:decoration-ember hover:decoration-2"
              >
                {label}
              </a>
            ))}
          </nav>
        )}

        <div className="ml-auto flex items-center gap-2 sm:gap-3">
          {signedIn ? (
            <ButtonLink to="/account" variant="ghost" className="hidden !px-3 !py-2 text-[12px] sm:inline-flex">
              Account
            </ButtonLink>
          ) : (
            <ButtonLink to="/login" variant="ghost" className="hidden !px-3 !py-2 text-[12px] sm:inline-flex">
              Sign in
            </ButtonLink>
          )}
          <ButtonLink
            to={signedIn ? "/account" : "/signup"}
            variant="ink"
            className="!px-3 !py-2 whitespace-nowrap text-[12px]"
          >
            {signedIn ? "Your plan" : "Sign up free"}
          </ButtonLink>
        </div>
      </div>
    </header>
  );
}

const FOOTER_LINKS: Array<{ label: string; href?: string; external?: boolean; to?: string }> = [
  { label: "Privacy", to: "/privacy" },
  { label: "Terms", href: `${LANDING_URL}/terms.html`, external: true },
  { label: "Contact", href: `${LANDING_URL}/contact.html`, external: true },
  { label: "ContextShrink", href: LANDING_URL, external: true },
];

export function SiteFooter({ className }: { className?: string }) {
  return (
    <footer className={cx("border-t border-line", className)}>
      <div className="mx-auto flex max-w-6xl flex-col gap-4 px-4 py-8 text-sm text-ink-3 sm:flex-row sm:items-center sm:justify-between sm:px-6">
        <p>
          ContextShrink Code. Compression runs on your computer. © {new Date().getFullYear()} ContextShrink.
        </p>
        <nav aria-label="Footer" className="flex flex-wrap gap-x-5 gap-y-2">
          {FOOTER_LINKS.map((l) =>
            l.to ? (
              <Link key={l.label} to={l.to} className="transition hover:text-ink hover:underline">
                {l.label}
              </Link>
            ) : (
              <a
                key={l.label}
                href={l.href}
                className="transition hover:text-ink hover:underline"
                {...(l.external ? { rel: "noopener noreferrer" } : {})}
              >
                {l.label}
              </a>
            ),
          )}
        </nav>
      </div>
    </footer>
  );
}
