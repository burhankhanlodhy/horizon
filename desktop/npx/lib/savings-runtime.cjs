'use strict';

// Installed outside npm's disposable cache. No daemon, dependencies, or credentials
// in arguments/cache/output. The API authenticates and aggregates this key only.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const http = require('node:http');
const https = require('node:https');
const TTL = 30000;
const UNAVAILABLE = 'ContextShrink · savings unavailable';

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
  if (!data || data.scope !== 'key' || data.period !== 'today_utc' ||
      !['requests', 'tokens_before', 'tokens_saved', 'savings_usd'].every(k => typeof data[k] === 'number' && Number.isFinite(data[k])) ||
      data.requests < 0 || data.tokens_before < 0 || data.tokens_saved < 0) return UNAVAILABLE;
  const pct = data.tokens_before > 0 ? Math.min(100, Math.round(data.tokens_saved / data.tokens_before * 100)) : 0;
  const dollars = usd(data.savings_usd);
  const money = data.savings_usd < 0 ? `net cost ${dollars}` : `net saved ${dollars}`;
  return `ContextShrink · trimmed ${pct}% · ${money} · ${data.requests} req · this key today (UTC)`;
}

function fetchSummary(url, key) {
  return new Promise((resolve, reject) => {
    const lib = url.protocol === 'https:' ? https : http;
    const req = lib.get(url, { headers: { authorization: `Bearer ${key}`, accept: 'application/json' } }, res => {
      if (res.statusCode !== 200) { res.resume(); reject(new Error('unavailable')); return; }
      let bytes = 0;
      const chunks = [];
      res.on('data', chunk => {
        bytes += chunk.length;
        if (bytes > 65536) { req.destroy(new Error('oversize')); return; }
        chunks.push(chunk);
      });
      res.on('error', reject);
      res.on('end', () => {
        try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); } catch { reject(new Error('invalid')); }
      });
    });
    // Absolute deadline includes DNS, TLS and slow response bodies.
    const deadline = setTimeout(() => req.destroy(new Error('timeout')), 2000);
    req.on('close', () => clearTimeout(deadline));
    req.on('error', reject);
  });
}

async function render(directory = __dirname) {
  try {
    const config = JSON.parse(fs.readFileSync(path.join(directory, 'ui-config.json'), 'utf8'));
    const state = JSON.parse(fs.readFileSync(config.state, 'utf8'));
    const settings = JSON.parse(fs.readFileSync(config.settings, 'utf8').replace(/^\uFEFF/, ''));
    const header = state.written?.ANTHROPIC_CUSTOM_HEADERS;
    const base = state.written?.ANTHROPIC_BASE_URL;
    // Stop displaying figures when the route/key no longer matches our installation.
    if (settings.env?.ANTHROPIC_CUSTOM_HEADERS !== header || settings.env?.ANTHROPIC_BASE_URL !== base) return 'ContextShrink · disconnected';
    const match = /^X-Horizon-Proxy-Token: (cs_live_[A-Za-z0-9_-]{20,200})$/.exec(header || '');
    if (!match) return UNAVAILABLE;
    const url = new URL(config.apiUrl.replace(/\/+$/, '') + '/usage/key-summary');
    if (url.protocol !== 'https:' && !(url.protocol === 'http:' && url.hostname === '127.0.0.1')) return UNAVAILABLE;
    const identity = crypto.createHash('sha256').update(url.href + '\0' + match[1]).digest('hex');
    const cachePath = path.join(directory, 'summary-cache.json');
    const now = Date.now();
    let cache;
    try { cache = JSON.parse(fs.readFileSync(cachePath, 'utf8')); } catch {}
    const age = now - cache?.fetchedAt;
    if (cache?.identity === identity && age >= 0 && age < TTL && cache.day === new Date(now).toISOString().slice(0, 10)) {
      return format(cache.data);
    }
    let data = null;
    try { data = await fetchSummary(url, match[1]); } catch {}
    const line = format(data);
    {
      const temp = path.join(directory, `.summary-${process.pid}-${crypto.randomBytes(4).toString('hex')}.tmp`);
      try {
        fs.writeFileSync(temp, JSON.stringify({ identity, fetchedAt: now, day: new Date(now).toISOString().slice(0, 10), data: line === UNAVAILABLE ? null : data }), { mode: 0o600, flag: 'wx' });
        fs.renameSync(temp, cachePath);
      } catch {} finally { try { fs.rmSync(temp, { force: true }); } catch {} }
    }
    return line;
  } catch { return UNAVAILABLE; }
}

if (require.main === module) render().then(line => console.log(line), () => console.log(UNAVAILABLE));
module.exports = { render, format };
