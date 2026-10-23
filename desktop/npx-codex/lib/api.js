'use strict';

// HTTP for the ContextShrink API. Requests carry a device key only as a bearer
// token, and error text is the server's `detail`, never a request body.

const http = require('node:http');
const https = require('node:https');

const USER_AGENT = `contextshrink-npx-codex/${require('../package.json').version}`;
const TIMEOUT_MS = 20000;
const UNREACHABLE = 'Cannot reach ContextShrink. Check your internet connection. Nothing was changed.';

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status || 0;
  }
}

function request(baseUrl, route, { method = 'GET', token, body, timeoutMs = TIMEOUT_MS } = {}) {
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
    const req = lib.request(url, { method, headers, timeout: timeoutMs }, (res) => {
      const chunks = [];
      let bytes = 0;
      res.on('data', (chunk) => {
        bytes += chunk.length;
        if (bytes > 1024 * 1024) {
          req.destroy(new Error('oversize'));
          return;
        }
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

module.exports = { ApiError, USER_AGENT, request, detailOf };
