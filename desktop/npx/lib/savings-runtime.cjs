'use strict';

// Installed outside npm's disposable cache. No daemon, dependencies, or credentials
// in arguments, cache or output. The device key is sent only to the ContextShrink
// API, as a bearer token, and only when this computer is still connected.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const http = require('node:http');
const https = require('node:https');
const local = require('./local-savings.cjs');
const TTL = 30000;
// The bar reads local files first; the summary API is asked only when they are missing.
const UNAVAILABLE = 'ContextShrink · savings unavailable';
const DISCONNECTED = 'ContextShrink · disconnected';

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8').replace(/^﻿/, ''));
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

// The summary API, used only when the engine's local usage file is missing. Cached for
// TTL, and per day, so a bar that refreshes often does not call the API each time.
async function fallbackSummary(config, directory, now) {
  try {
    const device = readJson(config.device);
    if (!/^cs_dev_[A-Za-z0-9_-]{16,200}$/.test(device.device_key || '')) return null;
    const url = new URL(String(device.api_url || '').replace(/\/+$/, '') + '/code/usage/summary');
    if (url.protocol !== 'https:' && !(url.protocol === 'http:' && url.hostname === '127.0.0.1')) return null;
    const identity = crypto.createHash('sha256').update(url.href + '\0' + device.device_key).digest('hex');
    const cachePath = path.join(directory, 'summary-cache.json');
    const day = new Date(now).toISOString().slice(0, 10);
    let cache;
    try { cache = JSON.parse(fs.readFileSync(cachePath, 'utf8')); } catch {}
    const age = now - cache?.fetchedAt;
    if (cache?.identity === identity && age >= 0 && age < TTL && cache.day === day) return cache.data;
    let data = null;
    try { data = await fetchSummary(url, device.device_key); } catch {}
    const temp = path.join(directory, `.summary-${process.pid}-${crypto.randomBytes(4).toString('hex')}.tmp`);
    try {
      fs.writeFileSync(temp, JSON.stringify({ identity, fetchedAt: now, day, data: data && data.today ? data : null }), { mode: 0o600, flag: 'wx' });
      fs.renameSync(temp, cachePath);
    } catch {} finally { try { fs.rmSync(temp, { force: true }); } catch {} }
    return data;
  } catch { return null; }
}

// The status line text. Local files only, so it changes as soon as the engine's next
// upload is written; no network call on this path unless the local usage file is missing.
async function render(directory = __dirname) {
  try {
    const config = readJson(path.join(directory, 'ui-config.json'));
    const state = readJson(config.state);
    const settings = readJson(config.settings);
    const base = state.written?.ANTHROPIC_BASE_URL;
    // Stop displaying figures when the route no longer matches our installation.
    if (!base || settings.env?.ANTHROPIC_BASE_URL !== base) return DISCONNECTED;
    const now = Date.now();
    const plan = config.license ? local.planOf(config.license, now) : null;
    const home = path.dirname(config.device);
    const today = local.todayUsage(home, config.tool === 'codex' ? 'codex' : 'claude', now);
    if (today) return local.formatLine(today.pct, plan);
    const data = await fallbackSummary(config, directory, now);
    const pct = data && data.today && Number.isFinite(data.today.pct) ? data.today.pct : null;
    return pct === null ? UNAVAILABLE : local.formatLine(pct, plan);
  } catch { return UNAVAILABLE; }
}

if (require.main === module) render().then(line => console.log(line), () => console.log(UNAVAILABLE));
module.exports = { render, format: local.formatLine, planOf: local.planOf, todayUsage: local.todayUsage };
