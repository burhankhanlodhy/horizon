'use strict';
const { ApiError } = require('./api');
function summary(data, now = new Date()) {
  if (data?.scope !== 'key' || data.period !== 'today_utc' ||
      !['requests', 'tokens_before', 'tokens_saved'].every(k => Number.isSafeInteger(data[k]) && data[k] >= 0) ||
      data.tokens_saved > data.tokens_before || !Number.isFinite(data.savings_usd)) throw new ApiError('Savings are currently unavailable.');
  return { version: 1, available: true,
    pct: data.tokens_before > 0 ? Math.round(data.tokens_saved / data.tokens_before * 100) : 0,
    netUsd: data.savings_usd, requests: data.requests, updatedAt: now.toISOString() };
}
// Dollars readable at any size (same rule as the desktop status line): cents
// from $0.01 up, up to four decimals below a cent ("$0.0015"), "<$0.0001" below.
function usd(amount) {
  const value = Math.abs(Number(amount) || 0);
  if (value === 0) return '$0.00';
  if (value >= 0.005) return '$' + value.toFixed(2);
  if (value >= 0.00005) return '$' + value.toFixed(4).replace(/0+$/, '');
  return '<$0.0001';
}
function format(data) {
  return `ContextShrink · trimmed ${data.pct}% · net ${data.netUsd < 0 ? 'cost' : 'saved'} ${usd(data.netUsd)} · ${data.requests} req · this key today (UTC)`;
}
module.exports = { summary, format, usd };
