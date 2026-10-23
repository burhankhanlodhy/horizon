'use strict';

// The control-plane calls the desktop app makes for sign-in and its device key
// (desktop/app/src-tauri/src/api.rs): POST /auth/login, POST /keys,
// DELETE /keys/{id}, POST /auth/logout. The session token is only ever held in
// memory, for the length of one run.

const http = require('node:http');
const https = require('node:https');
const { KEY_PREFIX, KEY_SCOPES } = require('./config');

const USER_AGENT = `contextshrink-npx-codex/${require('../package.json').version}`;
const TIMEOUT_MS = 20000;
const UNREACHABLE = 'Cannot reach ContextShrink. Check your internet connection. Nothing was changed.';

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status || 0;
  }
}

function request(baseUrl, route, { method = 'GET', token, body, teamId, timeoutMs = TIMEOUT_MS } = {}) {
  return new Promise((resolve, reject) => {
    let url;
    try {
      url = new URL(baseUrl + route);
    } catch {
      reject(new ApiError('The API URL is not valid.'));
      return;
    }
    const lib = url.protocol === 'https:' ? https : http;
    const payload = body === undefined ? null : Buffer.from(JSON.stringify(body));
    const headers = { accept: 'application/json', 'user-agent': USER_AGENT };
    if (payload) {
      headers['content-type'] = 'application/json';
      headers['content-length'] = payload.length;
    }
    if (token) headers.authorization = `Bearer ${token}`;
    if (teamId) headers['x-contextshrink-team'] = teamId;
    const req = lib.request(url, { method, headers, timeout: timeoutMs }, (res) => {
      const chunks = [];
      let bytes = 0;
      res.on('data', (chunk) => {
        bytes += chunk.length;
        if (bytes > 1024 * 1024) { req.destroy(new Error('oversize')); return; }
        chunks.push(chunk);
      });
      res.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8');
        let json = null;
        try {
          json = text ? JSON.parse(text) : null;
        } catch {
          json = null;
        }
        resolve({ status: res.statusCode, body: json });
      });
      res.on('error', () => reject(new ApiError(UNREACHABLE)));
    });
    req.on('timeout', () => req.destroy(new Error('timeout')));
    req.on('error', () => reject(new ApiError(UNREACHABLE)));
    const deadline = setTimeout(() => req.destroy(new Error('timeout')), timeoutMs);
    req.on('close', () => clearTimeout(deadline));
    if (payload) req.write(payload);
    req.end();
  });
}

// The server's `detail` text, never the request body.
function detailOf(res, fallback) {
  const detail = res.body && res.body.detail;
  if (typeof detail === 'string' && detail.trim()) return detail.trim();
  return `${fallback} (HTTP ${res.status}).`;
}

/** Signs in with email and password. Returns the session token. */
async function signIn(baseUrl, email, password) {
  const res = await request(baseUrl, '/auth/login', {
    method: 'POST',
    body: { email, password },
  });
  if (res.status === 401) throw new ApiError('Incorrect email or password. Nothing was changed.', 401);
  if (res.status >= 400) throw new ApiError(detailOf(res, 'Sign-in failed'), res.status);
  const token = res.body && res.body.token;
  if (typeof token !== 'string' || !token) {
    throw new ApiError('Unexpected sign-in response. Nothing was changed.');
  }
  return token;
}

/** Creates the proxy key for this computer. Returns { id, key }; the key is secret. */
async function createDeviceKey(baseUrl, token, name, teamId) {
  const res = await request(baseUrl, '/keys', {
    method: 'POST',
    token,
    body: { name, scopes: KEY_SCOPES },
    teamId,
  });
  if (res.status >= 400) throw new ApiError(detailOf(res, 'Could not create the account key'), res.status);
  const { id, key } = res.body || {};
  if (typeof id !== 'string' || typeof key !== 'string' || !key.startsWith(KEY_PREFIX)) {
    throw new ApiError('Unexpected key response. Nothing was changed.');
  }
  return { id, key };
}

/** Revokes a key by id. A key that is already gone counts as revoked. */
async function revokeDeviceKey(baseUrl, token, id, teamId) {
  const res = await request(baseUrl, `/keys/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    token,
    teamId,
  });
  if (res.status === 404 || res.status === 204 || res.status === 200) return;
  throw new ApiError(detailOf(res, 'Could not revoke the account key'), res.status);
}

/** Ends the throwaway session. Best effort: the key does not depend on it. */
async function signOut(baseUrl, token) {
  try {
    await request(baseUrl, '/auth/logout', { method: 'POST', token });
  } catch {
    // Nothing to do: the session expires on the server anyway.
  }
}

async function keySummary(baseUrl, key, options = {}) {
  const res = await request(baseUrl, '/usage/key-summary', { token: key, ...options });
  if (res.status !== 200 || !res.body) throw new ApiError('Savings are unavailable. Check the connection or API key.');
  return res.body;
}
module.exports = { ApiError, USER_AGENT, signIn, createDeviceKey, revokeDeviceKey, signOut, keySummary };
