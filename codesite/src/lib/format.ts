/** Display helpers. Usage is shown as percentages and token counts only, never dollars. */

const compact = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });
const full = new Intl.NumberFormat("en");

/** 1234567 -> "1.2M" */
export function fmtCount(n: number): string {
  return compact.format(n);
}

/** 1234567 -> "1,234,567" */
export function fmtFullCount(n: number): string {
  return full.format(n);
}

/** 23.4 -> "23.4%" below 10, otherwise "23%". */
export function fmtPct(p: number): string {
  if (!Number.isFinite(p) || p <= 0) return "0%";
  return p < 10 ? `${p.toFixed(1)}%` : `${Math.round(p)}%`;
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("en", { month: "short", day: "numeric", year: "numeric" });
}

/** Whole days from now until the given time, never negative. */
export function daysUntil(iso: string | null | undefined, now = Date.now()): number | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  return Math.max(0, Math.ceil((t - now) / 86_400_000));
}

const relative = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

/** "3 minutes ago", "yesterday", or "never" for a missing timestamp. */
export function fmtRelative(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "never";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "never";
  const diffSec = Math.round((t - now) / 1000);
  const abs = Math.abs(diffSec);
  if (abs < 60) return "just now";
  if (abs < 3600) return relative.format(Math.round(diffSec / 60), "minute");
  if (abs < 86_400) return relative.format(Math.round(diffSec / 3600), "hour");
  return relative.format(Math.round(diffSec / 86_400), "day");
}

export function platformLabel(platform: string): string {
  switch (platform) {
    case "windows":
      return "Windows";
    case "macos":
      return "macOS";
    case "linux":
      return "Linux";
    default:
      return platform;
  }
}

export function toolLabel(tool: string): string {
  switch (tool) {
    case "claude":
      return "Claude Code";
    case "codex":
      return "Codex";
    default:
      return tool;
  }
}
