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
function format(data) {
  return `ContextShrink · trimmed ${data.pct}% · net ${data.netUsd < 0 ? 'cost' : 'saved'} $${Math.abs(data.netUsd).toFixed(2)} · ${data.requests} req · this key today (UTC)`;
}
module.exports = { summary, format };
