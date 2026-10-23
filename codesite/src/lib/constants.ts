/**
 * Product copy and plan facts in one place. Marketing text reads from here so
 * the pricing, device limit and install commands are never out of step.
 */

/**
 * Optional usage claim line for the landing page. It stays null until a
 * number has been measured. While it is null, nothing renders.
 */
export const USAGE_CLAIM: string | null = null;

export const PRICE_MONTHLY_USD = 5;
export const DEVICE_LIMIT = 2;

export const PRICE_LINE =
  "Free: one week every month (sign-up required). Pro: $5/month for unlimited use.";

export const INSTALL_COMMANDS = [
  {
    id: "claude",
    label: "For Claude Code",
    command: "npx contextshrink-claude",
  },
  {
    id: "codex",
    label: "For Codex",
    command: "npx contextshrink-codex",
  },
] as const;
