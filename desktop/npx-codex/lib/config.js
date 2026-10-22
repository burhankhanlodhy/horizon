'use strict';
const os = require('node:os');
const path = require('node:path');
function endpoint(value, fallback) {
  const url = new URL(value || fallback);
  if (url.username || url.password || url.search || url.hash ||
      (url.protocol !== 'https:' && !(url.protocol === 'http:' && url.hostname === '127.0.0.1'))) {
    throw new Error('Service URL must be HTTPS (or HTTP on 127.0.0.1 for tests).');
  }
  return url.href.replace(/\/+$/, '');
}
function resolvePaths(env) {
  const home = env.USERPROFILE || env.HOME || os.homedir();
  const dir = path.resolve(env.CODEX_HOME || path.join(home, '.codex'));
  return {
    dir, config: path.join(dir, 'config.toml'), state: path.join(dir, '.contextshrink-npx-codex.json'),
    account: path.join(home, '.contextshrink', 'npx-codex.json'),
    apiUrl: endpoint(env.CONTEXTSHRINK_API_URL, 'https://api.contextshrink.com'),
    proxyUrl: endpoint(env.CONTEXTSHRINK_PROXY_URL, 'https://proxy.contextshrink.com'),
  };
}
module.exports = { resolvePaths, KEY_PREFIX: 'cs_live_', KEY_SCOPES: ['proxy:responses'] };
