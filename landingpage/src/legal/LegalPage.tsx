import type { ReactNode } from "react";
import { LEGAL_LINKS, LogoMark, Wordmark } from "../components/ui";

/**
 * Shell for the standalone pages (Privacy, Terms, Contact). They open in their
 * own tab from the landing page's footer, so the header links back to the
 * site rather than to an in-page anchor.
 */
export function LegalPage({
  title,
  updated,
  intro,
  children,
}: {
  title: string;
  updated?: string;
  intro?: ReactNode;
  children: ReactNode;
}) {
  const here = typeof window === "undefined" ? "" : window.location.pathname;
  return (
    <div className="min-h-screen bg-paper text-ink">
      <header className="border-b border-line">
        <div className="mx-auto flex h-16 max-w-3xl items-center justify-between gap-6 px-4 sm:px-8">
          <a href="/" className="flex items-center gap-2.5">
            <LogoMark size={26} />
            <Wordmark />
          </a>
          <a href="/" className="font-mono text-xs text-ink-2 transition hover:text-ink">
            ← contextshrink.com
          </a>
        </div>
      </header>
      <main className="mx-auto max-w-3xl px-4 py-14 sm:px-8 sm:py-20">
        <h1 className="font-display text-4xl font-semibold tracking-tight sm:text-5xl">{title}</h1>
        {updated && (
          <p className="mt-3 font-mono text-xs uppercase tracking-[0.2em] text-ink-3">
            Last updated {updated}
          </p>
        )}
        {intro && <div className="mt-6 text-lg leading-relaxed text-ink-2">{intro}</div>}
        <div className="mt-10 flex flex-col gap-10">{children}</div>
      </main>
      <footer className="border-t border-line">
        <div className="mx-auto flex max-w-3xl flex-col items-center justify-between gap-4 px-4 py-8 sm:flex-row sm:px-8">
          <nav className="flex flex-wrap justify-center gap-6 font-mono text-xs text-ink-2">
            {LEGAL_LINKS.map((l) => (
              <a
                key={l.href}
                href={l.href}
                aria-current={here === l.href ? "page" : undefined}
                className="transition hover:text-ink aria-[current=page]:text-ink aria-[current=page]:underline"
              >
                {l.label}
              </a>
            ))}
          </nav>
          <div className="font-mono text-xs text-ink-3">© 2026 ContextShrink</div>
        </div>
      </footer>
    </div>
  );
}

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <h2 className="font-display text-2xl font-semibold tracking-tight">{title}</h2>
      <div className="mt-4 flex flex-col gap-4 leading-relaxed text-ink-2">{children}</div>
    </section>
  );
}

export function List({ children }: { children: ReactNode }) {
  return <ul className="flex list-disc flex-col gap-2 pl-6 marker:text-ember">{children}</ul>;
}

export function Strong({ children }: { children: ReactNode }) {
  return <strong className="font-semibold text-ink">{children}</strong>;
}
