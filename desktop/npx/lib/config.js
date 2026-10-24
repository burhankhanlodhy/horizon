'use strict';

// Paths and fixed names for contextshrink-claude.
//
// Every path and URL can be overridden through the environment (HOME,
// USERPROFILE, CLAUDE_CONFIG_DIR, CONTEXTSHRINK_HOME, CONTEXTSHRINK_API_URL,
// CONTEXTSHRINK_ENGINE_URL, LOCALAPPDATA, APPDATA), so tests never touch a real
// ~/.claude or the real ContextShrink services.

const os = require('node:os');
const path = require('node:path');
const { apiUrlOf, engineUrlOf, DEFAULT_ENGINE_URL } = require('./code-env');
const device = require('./code-device');

// Written by the desktop app's Connect next to settings.json. When it exists,
// the app owns the connection, so this installer steps aside.
const DESKTOP_STATE_FILE = '.horizon-vscode-claude.json';
// Written by this installer next to settings.json.
const STATE_FILE = '.contextshrink-npx.json';
// Written by `lean on` next to settings.json (see lean.js).
const LEAN_FILE = '.contextshrink-lean.json';
const SETTINGS_FILE = 'settings.json';
// The hosted proxy that versions 0.2.x connected Claude Code to. Used only to
// recognise and take over an earlier connection.
const LEGACY_PROXY_URL = 'https://proxy.contextshrink.com';

const BASE_URL_KEY = 'ANTHROPIC_BASE_URL';
// Versions before 0.3.0 wrote the account key here. 0.3.0 removes it.
const HEADERS_KEY = 'ANTHROPIC_CUSTOM_HEADERS';
// The key that routes Claude Code through the local engine. Connect refuses to
// replace it unless --force.
const MANAGED_KEYS = [BASE_URL_KEY];
// Claude Code's own MCP tool search. A gateway in ANTHROPIC_BASE_URL makes Claude
// Code switch it off, so connect sets it to "true" only when the user has not set it.
const TOOL_SEARCH_KEY = 'ENABLE_TOOL_SEARCH';
const TOOL_SEARCH_VALUE = 'true';
// Every key connect may write. Only MANAGED_KEYS are required for "connected".
const OWNED_KEYS = [...MANAGED_KEYS, HEADERS_KEY, TOOL_SEARCH_KEY];
// Keep sessions lean: the auto-compact window Claude Code reads. Kept out of OWNED_KEYS:
// lean has its own record (LEAN_FILE), so connect's state does not change.
const LEAN_KEY = 'CLAUDE_CODE_AUTO_COMPACT_WINDOW';
const LEAN_VALUE = '200000';

function expandHome(p, homeDir) {
  if (p === '~') return homeDir;
  if (p.startsWith('~/') || p.startsWith('~\\')) return path.join(homeDir, p.slice(2));
  return p;
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
  const files = device.devicePathsOf(env);
  return {
    homeDir,
    configDir,
    settings,
    state: path.join(configDir, STATE_FILE),
    leanRecord: path.join(configDir, LEAN_FILE),
    desktopState: path.join(configDir, DESKTOP_STATE_FILE),
    csHome: files.home,
    device: files.device,
    license: files.license,
    // The keep-alive file of versions before 0.3.0. Removed once this version connects.
    legacyHome: path.join(files.home, 'npx.json'),
    apiUrl: apiUrlOf(env),
    engineUrl: engineUrlOf(env),
    proxyUrl: (env.CONTEXTSHRINK_PROXY_URL || '').trim() || LEGACY_PROXY_URL,
  };
}

module.exports = {
  BASE_URL_KEY,
  HEADERS_KEY,
  MANAGED_KEYS,
  TOOL_SEARCH_KEY,
  TOOL_SEARCH_VALUE,
  OWNED_KEYS,
  LEAN_KEY,
  LEAN_VALUE,
  LEGACY_PROXY_URL,
  DEFAULT_ENGINE_URL,
  resolvePaths,
};
