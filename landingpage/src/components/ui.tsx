import { useEffect, useRef, useState, type ReactNode } from "react";
import { animate, useInView } from "framer-motion";
import { Check, Copy, Shrink } from "lucide-react";
import { cn, fmtFull } from "../lib/utils";

/** One shared easing curve — snappy in, long settle out. */
export const EASE: [number, number, number, number] = [0.22, 1, 0.36, 1];

/** Where the app lives — baked at build time for the two-host deploy. */
export const DASHBOARD_URL: string =
  (import.meta.env.VITE_DASHBOARD_URL as string | undefined) ?? "http://127.0.0.1:5173";

/** The control-plane API (the Contact form posts here). */
export const API_URL: string =
  (import.meta.env.VITE_API_URL as string | undefined) ??
  (import.meta.env.DEV ? "http://127.0.0.1:8788" : "https://api.contextshrink.com");

/** Standalone pages, opened in a new tab from the footer. */
export const LEGAL_LINKS = [
  { label: "Privacy Policy", href: "/privacy.html" },
  { label: "Terms of Service", href: "/terms.html" },
  { label: "Contact Us", href: "/contact.html" },
  // Hosted by UptimeRobot, so it stays up when ContextShrink does not.
  { label: "Status", href: "https://stats.uptimerobot.com/RNJwy7h05k" },
];

/* ------------------------------ brand ------------------------------ */

export function LogoMark({ size = 30 }: { size?: number }) {
  return (
    <span
      className="grid shrink-0 place-items-center border-[1.5px] border-ink bg-ember text-paper"
      style={{ width: size, height: size, borderRadius: 2 }}
      aria-hidden
    >
      <Shrink size={size * 0.52} strokeWidth={2.4} />
    </span>
  );
}

export function Wordmark({ className }: { className?: string }) {
  return (
    <span className={cn("font-display text-lg font-bold tracking-tight", className)}>
      Context<span className="italic text-ember">Shrink</span>
    </span>
  );
}

/* ----------------------------- buttons ----------------------------- */

type BtnProps = {
  children: ReactNode;
  href?: string;
  onClick?: () => void;
  className?: string;
  variant?: "ink" | "ghost" | "paper" | "ghost-light";
};

export function Btn({ children, href, onClick, className, variant = "ink" }: BtnProps) {
  const cls = cn(
    "btn",
    variant === "ink" && "btn-ink",
    variant === "ghost" && "btn-ghost",
    variant === "paper" && "btn-paper",
    variant === "ghost-light" && "btn-ghost-light",
    className,
  );
  if (href) {
    return (
      <a href={href} className={cls}>
        {children}
      </a>
    );
  }
  return (
    <button onClick={onClick} className={cls}>
      {children}
    </button>
  );
}

/* ----------------------- editorial primitives ---------------------- */

/** Small mono index number — "01", "02"… */
export function Idx({ n, className }: { n: number; className?: string }) {
  return <span className={cn("idx", className)}>{String(n).padStart(2, "0")}</span>;
}

/** Mono kicker with a short ember rule. */
export function Kicker({ children, className }: { children: ReactNode; className?: string }) {
  return <p className={cn("kicker", className)}>{children}</p>;
}

/**
 * Word underlined with a hand-drawn squiggle that draws itself in on view.
 * Replaces gradient text as the headline accent.
 */
export function Squiggle({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLSpanElement>(null);
  const inView = useInView(ref, { once: true, margin: "-60px" });
  return (
    <span ref={ref} className={cn("squiggle squiggle-anim italic", inView && "drawn")}>
      {children}
    </span>
  );
}

/** Hand-drawn-ish arrow annotation with a mono caption. */
export function AnnotArrow({
  caption,
  flip = false,
  className,
}: {
  caption: string;
  flip?: boolean;
  className?: string;
}) {
  return (
    <div className={cn("annot-arrow flex flex-col items-center gap-1", className)} aria-hidden>
      <svg
        width="64"
        height="30"
        viewBox="0 0 64 30"
        fill="none"
        style={flip ? { transform: "scaleX(-1)" } : undefined}
      >
        <path
          d="M4 6 C 22 2, 40 6, 56 20"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
          strokeDasharray="1 5"
        />
        <path
          className="arrow-head"
          d="M49 19 L 57 22 L 52 13"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
          fill="none"
        />
      </svg>
      <span className="font-mono text-[10.5px] italic tracking-wide">{caption}</span>
    </div>
  );
}



/* ------------------- count-up number & code block ------------------ */

export function CountUp({
  value,
  format = fmtFull,
  className,
  duration = 1.4,
}: {
  value: number;
  format?: (n: number) => string;
  className?: string;
  duration?: number;
}) {
  const ref = useRef<HTMLSpanElement>(null);
  const inView = useInView(ref, { once: true, margin: "-40px" });
  const [display, setDisplay] = useState(() => format(0));

  useEffect(() => {
    if (!inView) return;
    const controls = animate(0, value, {
      duration,
      ease: EASE,
      onUpdate: (v) => setDisplay(format(v)),
    });
    return () => controls.stop();
  }, [inView, value, duration, format]);

  return (
    <span ref={ref} className={cn("tabular-nums", className)}>
      {display}
    </span>
  );
}

export function CopyButton({ text, className }: { text: string; className?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
        } catch {
          /* clipboard unavailable — visual feedback only */
        }
        setCopied(true);
        setTimeout(() => setCopied(false), 1600);
      }}
      aria-label={copied ? "Copied" : "Copy to clipboard"}
      className={cn(
        "inline-flex items-center gap-1.5 border px-2 py-1 font-mono text-[11px] transition",
        copied
          ? "border-sage text-sage"
          : "border-line-2 text-ink-3 hover:border-ink hover:text-ink",
        className,
      )}
      style={{ borderRadius: 2 }}
    >
      {copied ? <Check size={12} /> : <Copy size={12} />}
      {copied ? "copied" : "copy"}
    </button>
  );
}

export function CodeBlock({ code, className }: { code: string; className?: string }) {
  return (
    <div className={cn("paper-panel relative", className)}>
      <CopyButton text={code} className="absolute right-3 top-3" />
      <pre className="overflow-x-auto p-4 pr-20 font-mono text-[12.5px] leading-relaxed text-ink-2">
        {code}
      </pre>
    </div>
  );
}
