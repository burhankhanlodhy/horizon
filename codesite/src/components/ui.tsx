import { useEffect, useRef, useState, type ButtonHTMLAttributes, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { Check, Copy, Shrink } from "lucide-react";

export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

export function LogoMark({ size = 30 }: { size?: number }) {
  return (
    <span
      className="grid shrink-0 place-items-center border-[1.5px] border-ink bg-ember text-card"
      style={{ width: size, height: size, borderRadius: 2 }}
      aria-hidden="true"
    >
      <Shrink size={size * 0.52} strokeWidth={2.4} />
    </span>
  );
}

/** "ContextShrink" wordmark with the product name beside it. */
export function Wordmark({ product = true }: { product?: boolean }) {
  return (
    <span className="flex items-baseline gap-2 whitespace-nowrap">
      <span className="font-display text-lg font-bold tracking-tight">
        Context<span className="italic text-ember">Shrink</span>
      </span>
      {product && (
        <span className="font-mono text-[11px] uppercase tracking-[0.16em] text-ink-3">Code</span>
      )}
    </span>
  );
}

type Variant = "ink" | "ghost" | "danger";

const VARIANT: Record<Variant, string> = {
  ink: "btn-ink",
  ghost: "btn-ghost",
  danger: "btn-danger",
};

export function Button({
  variant = "ink",
  className,
  type = "button",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return <button type={type} className={cx("btn", VARIANT[variant], className)} {...rest} />;
}

/** Router link (to=) or plain anchor (href=) styled as a button. */
export function ButtonLink({
  to,
  href,
  variant = "ink",
  className,
  children,
}: {
  to?: string;
  href?: string;
  variant?: Variant;
  className?: string;
  children: ReactNode;
}) {
  const classes = cx("btn", VARIANT[variant], className);
  if (to) {
    return (
      <Link to={to} className={classes}>
        {children}
      </Link>
    );
  }
  return (
    <a href={href} className={classes}>
      {children}
    </a>
  );
}

export function Card({
  className,
  children,
  as: Tag = "section",
  labelledBy,
}: {
  className?: string;
  children: ReactNode;
  as?: "section" | "div" | "article";
  labelledBy?: string;
}) {
  return (
    <Tag className={cx("ink-card rounded-lg p-5 sm:p-7", className)} aria-labelledby={labelledBy}>
      {children}
    </Tag>
  );
}

export function Kicker({ children, className }: { children: ReactNode; className?: string }) {
  return <p className={cx("kicker", className)}>{children}</p>;
}

/** Copies text to the clipboard and confirms for a moment. */
export function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard blocked: the command is still visible to select by hand */
    }
  };

  return (
    <button
      type="button"
      onClick={copy}
      aria-label={copied ? "Copied to clipboard" : `Copy ${text}`}
      className="inline-flex shrink-0 items-center gap-1.5 rounded-[2px] border border-code-ink/30 px-2.5 py-1.5 font-mono text-[12px] text-code-ink transition hover:border-code-ink hover:bg-code-ink/10"
    >
      {copied ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
      <span>{copied ? "Copied" : "Copy"}</span>
    </button>
  );
}

/** A terminal-style command with a copy button. */
export function CodeCommand({ command, label }: { command: string; label?: string }) {
  return (
    <div className="codeblock flex min-w-0 items-center justify-between gap-3 px-4 py-3.5">
      <div className="min-w-0">
        {label && (
          <div className="mb-1 font-mono text-[11px] uppercase tracking-[0.14em] opacity-70">
            {label}
          </div>
        )}
        <code className="block break-all font-mono text-[15px] sm:text-base">
          <span aria-hidden="true" className="select-none opacity-60">
            ${" "}
          </span>
          {command}
        </code>
      </div>
      <CopyButton text={command} />
    </div>
  );
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-sm text-ink-3" role="status">
      <span
        aria-hidden="true"
        className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-ink-3 border-t-transparent"
      />
      <span>{label}…</span>
    </span>
  );
}

const BANNER: Record<"info" | "error" | "success", string> = {
  info: "border-line-2 bg-paper-2 text-ink-2",
  error: "border-ember bg-ember-soft text-ink",
  success: "border-sage bg-sage-soft text-ink",
};

/** Inline message. Errors use role=alert so screen readers announce them. */
export function Banner({
  tone = "info",
  children,
  className,
}: {
  tone?: "info" | "error" | "success";
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      className={cx("rounded-[2px] border px-3.5 py-2.5 text-sm leading-relaxed", BANNER[tone], className)}
    >
      {children}
    </div>
  );
}
