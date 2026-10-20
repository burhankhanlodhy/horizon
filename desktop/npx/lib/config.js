'use strict';

// Paths, endpoints and fixed names for the contextshrink installer.
//
// Every path and URL can be overridden through the environment (HOME,
// USERPROFILE, CLAUDE_CONFIG_DIR, CONTEXTSHRINK_API_URL,
// CONTEXTSHRINK_PROXY_URL), so tests never touch a real ~/.claude or the
// real ContextShrink services.

const os = require('node:os');
const path = require('node:path');

const DEFAULT_API_URL = 'https://api.contextshrink.com';
const DEFAULT_PROXY_URL = 'https://proxy.contextshrink.com';

// Written by the desktop app's Connect next to settings.json. When it exists,
// the app owns the proxy route, so this installer steps aside.
const DESKTOP_STATE_FILE = '.horizon-vscode-claude.json';
// Written by this installer next to settings.json.
const STATE_FILE = '.contextshrink-npx.json';
const SETTINGS_FILE = 'settings.json';

const BASE_URL_KEY = 'ANTHROPIC_BASE_URL';
const HEADERS_KEY = 'ANTHROPIC_CUSTOM_HEADERS';
// The two keys that route Claude Code through the proxy. Connect refuses to
// replace a value of these unless --force, and a connection needs both.
const MANAGED_KEYS = [BASE_URL_KEY, HEADERS_KEY];
// Claude Code's own MCP tool search. A gateway in ANTHROPIC_BASE_URL makes Claude
// Code switch it off, and the proxy then defers the tools itself, which is not a
// real saving. Connect sets it to "true" only when the user has not set it.
const TOOL_SEARCH_KEY = 'ENABLE_TOOL_SEARCH';
const TOOL_SEARCH_VALUE = 'true';
// Every key connect may write. Only MANAGED_KEYS are required for "connected".
const OWNED_KEYS = [...MANAGED_KEYS, TOOL_SEARCH_KEY];

// The header the hosted proxy reads the account key from (horizon/proxy/server.py,
// read_proxy_token). Same spelling as the dashboard's manual setup page.
const PROXY_TOKEN_HEADER = 'X-Horizon-Proxy-Token';
const KEY_PREFIX = 'cs_live_';
// Scopes the desktop app asks for when it creates its device key (desktop/app/src-tauri/src/api.rs).
const KEY_SCOPES = ['proxy:messages', 'proxy:responses'];
const KEY_NAME_SUFFIX = ' (npx)';

function expandHome(p, homeDir) {
  if (p === '~') return homeDir;
  if (p.startsWith('~/') || p.startsWith('~\\')) return path.join(homeDir, p.slice(2));
  return p;
}

function trimSlash(url) {
  return url.replace(/\/+$/, '');
}

function endpoint(env, name, fallback) {
  const raw = (env[name] || '').trim();
  const value = trimSlash(raw || fallback);
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error(`${name} is not a valid URL.`);
  }
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') {
    throw new Error(`${name} must be an http or https URL.`);
  }
  return value;
}

/**
 * Every path and URL this run touches, resolved from the environment.
 */
function resolvePaths(env = process.env) {
  const homeDir = os.homedir();
  const configDir = env.CLAUDE_CONFIG_DIR
    ? path.resolve(expandHome(env.CLAUDE_CONFIG_DIR, homeDir))
    : path.join(homeDir, '.claude');
  const settings = path.join(configDir, SETTINGS_FILE);
  return {
    homeDir,
    configDir,
    settings,
    state: path.join(configDir, STATE_FILE),
    desktopState: path.join(configDir, DESKTOP_STATE_FILE),
    // Account-level memory of this computer: the keep-alive id and the device
    // key id (never the key itself). Lives in the user's home, not in .claude.
    home: path.join(homeDir, '.contextshrink', 'npx.json'),
    apiUrl: endpoint(env, 'CONTEXTSHRINK_API_URL', DEFAULT_API_URL),
    proxyUrl: endpoint(env, 'CONTEXTSHRINK_PROXY_URL', DEFAULT_PROXY_URL),
  };
}

module.exports = {
  DEFAULT_API_URL,
  DEFAULT_PROXY_URL,
  BASE_URL_KEY,
  HEADERS_KEY,
  MANAGED_KEYS,
  TOOL_SEARCH_KEY,
  TOOL_SEARCH_VALUE,
  OWNED_KEYS,
  PROXY_TOKEN_HEADER,
  KEY_PREFIX,
  KEY_SCOPES,
  KEY_NAME_SUFFIX,
  resolvePaths,
};
