'use strict';
const { ApiError } = require('./api');
const local = require('./local-savings.cjs');

// The savings snapshot for the status line and the window. Dollar amounts are not shown:
// the plan is a flat fee. planInfo is { plan: 'trial', daysLeft } | { plan: 'active' } | null.

function planFields(planInfo) {
  if (planInfo && planInfo.plan === 'active') return { plan: 'active', trialDays: null };
  if (planInfo && planInfo.plan === 'trial' && Number.isSafeInteger(planInfo.daysLeft) && planInfo.daysLeft >= 0) {
    return { plan: 'trial', trialDays: planInfo.daysLeft };
  }
  return { plan: null, trialDays: null };
}

function snapshotOf(today, planInfo, now) {
  return {
    version: 1,
    available: true,
    pct: Math.max(0, Math.min(100, Number(today.pct) || 0)),
    requests: today.requests,
    ...planFields(planInfo),
    updatedAt: now.toISOString(),
  };
}

// Today's usage from GET /code/usage/summary (used only when the local usage file is missing).
function summary(data, planInfo = null, now = new Date()) {
  const today = data && data.today;
  if (!today || !Number.isFinite(today.pct) || !Number.isSafeInteger(today.requests) || today.requests < 0) {
    throw new ApiError('Usage is currently unavailable.');
  }
  return snapshotOf(today, planInfo, now);
}

// Today's usage from the engine's local usage file, or null when that file is missing.
function fromLocal(home, planInfo = null, now = new Date()) {
  const today = local.todayUsage(home, 'codex', now.getTime());
  if (!today) return null;
  return snapshotOf({ pct: today.pct, requests: today.requests }, planInfo, now);
}

function format(data) {
  const plan = data.plan === 'active' ? { plan: 'active' }
    : data.plan === 'trial' ? { plan: 'trial', daysLeft: data.trialDays } : null;
  return local.formatLine(data.pct, plan);
}

module.exports = { summary, fromLocal, format };
