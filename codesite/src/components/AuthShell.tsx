import type { ReactNode } from "react";
import { SiteFooter, SiteHeader } from "./SiteChrome";
import { cx } from "./ui";

/** Centred single-card layout for sign-in, sign-up and the connect step. */
export function AuthShell({
  title,
  intro,
  children,
  wide = false,
}: {
  title: string;
  intro?: ReactNode;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <div className="flex min-h-screen flex-col">
      <SiteHeader />
      <main className="flex flex-1 items-start justify-center px-4 pb-16 pt-10 sm:pt-16">
        <section
          className={cx(
            "ink-card w-full rounded-lg p-6 sm:p-9",
            wide ? "max-w-xl" : "max-w-md",
          )}
          aria-labelledby="auth-title"
        >
          <h1 id="auth-title" className="font-display text-2xl font-bold text-ink sm:text-[28px]">
            {title}
          </h1>
          {intro && <div className="mt-2 text-sm leading-relaxed text-ink-3">{intro}</div>}
          <div className="mt-6">{children}</div>
        </section>
      </main>
      <SiteFooter />
    </div>
  );
}

export const fieldLabelCls = "mb-1.5 block text-xs font-medium text-ink-2";
