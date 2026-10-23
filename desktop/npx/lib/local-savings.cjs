'use strict';

// Reads what the status bar shows from local files only: the engine's pending usage file
// and the plan note. No network call. npx and npx-codex carry byte-identical copies
// (cmp-checked by the tests), so both packages format the same text.
const fs = require('node:fs');
const path = require('node:path');

const USAGE_FILE = 'usage-pending.json';
const DAY_MS = 86400000;

function readJsonFile(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8').replace(/^﻿/, ''));
  } catch {
    return null;
  }
}

function isCount(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

/**
 * Today's totals (UTC day) for one tool from the engine's usage-pending.json in ``home``.
 * Returns null when the file is missing or unreadable, so the caller can fall back.
 * pct is (before - after) / before, clamped to 0..100; 0 when nothing was counted today.
 */
function todayUsage(home, tool, now = Date.now()) {
  const data = readJsonFile(path.join(home, USAGE_FILE));
  if (!data || data.version !== 1 || !Array.isArray(data.buckets)) return null;
  const day = new Date(now).toISOString().slice(0, 10);
  let requests = 0;
  let before = 0;
  let after = 0;
  for (const bucket of data.buckets) {
    if (!bucket || bucket.tool !== tool || typeof bucket.hour !== 'string' || !bucket.hour.startsWith(day)) continue;
    if (!isCount(bucket.requests) || !isCount(bucket.tokens_before) || !isCount(bucket.tokens_after)) continue;
    requests += bucket.requests;
    before += bucket.tokens_before;
    after += bucket.tokens_after;
  }
  const pct = before > 0 ? Math.max(0, Math.min(100, ((before - after) / before) * 100)) : 0;
  return { requests, tokensBefore: before, tokensAfter: after, pct };
}

/**
 * The plan shown in the bar, from license.json's note. The payload is decoded only for
 * display; the engine checks the signature. Returns { plan: 'active' } or
 * { plan: 'trial', daysLeft } or null.
 */
function planOf(licenseFile, now = Date.now()) {
  const data = readJsonFile(licenseFile);
  if (!data || typeof data.token !== 'string') return null;
  try {
    const payload = JSON.parse(Buffer.from(data.token.split('.')[0], 'base64url').toString('utf8'));
    if (payload.plan === 'active') return { plan: 'active' };
    if (payload.plan === 'trial') {
      const until = Number(payload.until) * 1000;
      if (!Number.isFinite(until)) return null;
      return { plan: 'trial', daysLeft: Math.max(0, Math.ceil((until - now) / DAY_MS)) };
    }
  } catch {
    // A note that does not decode shows no plan.
  }
  return null;
}

/** The percentage text: one decimal under 10 percent, whole numbers from 10 up. */
function percentText(pct) {
  const value = Math.max(0, Math.min(100, Number(pct) || 0));
  const tenths = Math.round(value * 10) / 10;
  return tenths < 10 ? tenths.toFixed(1) : String(Math.round(value));
}

/** "ContextShrink · usage saved 4.3% today · free week 6d left" (or "· Subscribed"). */
function formatLine(pct, plan) {
  let tail = '';
  if (plan && plan.plan === 'active') tail = ' · Subscribed';
  else if (plan && plan.plan === 'trial') {
    tail = plan.daysLeft > 0 ? ` · free week ${plan.daysLeft}d left` : ' · free week ended';
  }
  return `ContextShrink · usage saved ${percentText(pct)}% today${tail}`;
}

module.exports = { USAGE_FILE, todayUsage, planOf, percentText, formatLine };
