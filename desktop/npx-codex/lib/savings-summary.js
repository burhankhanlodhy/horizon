'use strict';
const { ApiError } = require('./api');

// Today's usage from GET /code/usage/summary, plus the trial days left (null when
// there is no trial). Dollar amounts are not shown: the plan is a flat fee.
function summary(data, trialDays = null, now = new Date()) {
  const today = data && data.today;
  if (!today || !Number.isFinite(today.pct) || !Number.isSafeInteger(today.requests) || today.requests < 0) {
    throw new ApiError('Usage is currently unavailable.');
  }
  return {
    version: 1,
    available: true,
    pct: Math.max(0, Math.min(100, Math.round(today.pct))),
    requests: today.requests,
    trialDays: Number.isSafeInteger(trialDays) && trialDays >= 0 ? trialDays : null,
    updatedAt: now.toISOString(),
  };
}

function format(data) {
  const trial = data.trialDays === null ? '' : ` · trial ${data.trialDays}d left`;
  return `ContextShrink · usage saved ${data.pct}% today${trial}`;
}

module.exports = { summary, format };
