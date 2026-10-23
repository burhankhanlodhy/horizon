'use strict';

// Installed outside npm's disposable cache. No daemon, dependencies, or credentials
// in arguments, cache or output. The device key is sent only to the ContextShrink
// API, as a bearer token, and only when this computer is still connected.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const http = require('node:http');
const https = require('node:https');
const TTL = 30000;
const UNAVAILABLE = 'ContextShrink · savings unavailable';
const DISCONNECTED = 'ContextShrink · disconnected';

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8').replace(/^﻿/, ''));
}

// The plan from license.json's payload. The engine checks the signature; this line only displays it.
function planOf(file, now) {
  try {
    const token = readJson(file).token;
    const payload = JSON.parse(Buffer.from(String(token).split('.')[0], 'base64url').toString('utf8'));
    if (payload.plan === 'active') return '';
    if (payload.plan !== 'trial') return '';
    const left = Math.ceil((Number(payload.until) * 1000 - now) / 86400000);
    return left > 0 ? `free week ${left}d left` : 'free week ended';
  } catch {
    return '';
  }
}

function format(data, trial) {
  const pct = data && data.today && Number.isFinite(data.today.pct)
    ? Math.max(0, Math.min(100, Math.round(data.today.pct)))
    : null;
  if (pct === null) return UNAVAILABLE;
  const line = `ContextShrink · usage saved ${pct}% today`;
  return trial ? `${line} · ${trial}` : line;
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
    const config = readJson(path.join(directory, 'ui-config.json'));
    const state = readJson(config.state);
    const settings = readJson(config.settings);
    const base = state.written?.ANTHROPIC_BASE_URL;
    // Stop displaying figures when the route no longer matches our installation.
    if (!base || settings.env?.ANTHROPIC_BASE_URL !== base) return DISCONNECTED;
    const device = readJson(config.device);
    if (!/^cs_dev_[A-Za-z0-9_-]{16,200}$/.test(device.device_key || '')) return UNAVAILABLE;
    const url = new URL(String(device.api_url || '').replace(/\/+$/, '') + '/code/usage/summary');
    if (url.protocol !== 'https:' && !(url.protocol === 'http:' && url.hostname === '127.0.0.1')) return UNAVAILABLE;
    const identity = crypto.createHash('sha256').update(url.href + '\0' + device.device_key).digest('hex');
    const cachePath = path.join(directory, 'summary-cache.json');
    const now = Date.now();
    const day = new Date(now).toISOString().slice(0, 10);
    let cache;
    try { cache = JSON.parse(fs.readFileSync(cachePath, 'utf8')); } catch {}
    const age = now - cache?.fetchedAt;
    const trial = config.license ? planOf(config.license, now) : '';
    if (cache?.identity === identity && age >= 0 && age < TTL && cache.day === day) {
      return format(cache.data, trial);
    }
    let data = null;
    try { data = await fetchSummary(url, device.device_key); } catch {}
    const line = format(data, trial);
    {
      const temp = path.join(directory, `.summary-${process.pid}-${crypto.randomBytes(4).toString('hex')}.tmp`);
      try {
        fs.writeFileSync(temp, JSON.stringify({ identity, fetchedAt: now, day, data: data && data.today ? data : null }), { mode: 0o600, flag: 'wx' });
        fs.renameSync(temp, cachePath);
      } catch {} finally { try { fs.rmSync(temp, { force: true }); } catch {} }
    }
    return line;
  } catch { return UNAVAILABLE; }
}

if (require.main === module) render().then(line => console.log(line), () => console.log(UNAVAILABLE));
module.exports = { render, format, planOf };
