'use strict';

// Paths for contextshrink-codex. Every path and URL can be overridden through the
// environment (USERPROFILE, HOME, CODEX_HOME, CONTEXTSHRINK_HOME, CONTEXTSHRINK_API_URL,
// CONTEXTSHRINK_ENGINE_URL, LOCALAPPDATA, APPDATA), so tests never touch a real ~/.codex.

const path = require('node:path');
const { homeOf, apiUrlOf, engineUrlOf } = require('./code-env');
const device = require('./code-device');

function resolvePaths(env) {
  const dir = path.resolve(env.CODEX_HOME || path.join(homeOf(env), '.codex'));
  const files = device.devicePathsOf(env);
  return {
    dir,
    config: path.join(dir, 'config.toml'),
    state: path.join(dir, '.contextshrink-npx-codex.json'),
    csHome: files.home,
    device: files.device,
    license: files.license,
    usage: files.usage,
    // The per-computer record of versions before 0.3.0. Removed once this version connects.
    legacyAccount: path.join(files.home, 'npx-codex.json'),
    apiUrl: apiUrlOf(env),
    engineUrl: engineUrlOf(env),
  };
}

module.exports = { resolvePaths };
