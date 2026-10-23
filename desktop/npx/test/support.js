'use strict';

// Test helpers for contextshrink-claude. Every path and URL a test touches lives
// under a temp directory, the API and the engine are fakes on 127.0.0.1, and the
// Windows work goes through the fake adapter. Nothing reads the real ~/.claude.

const fs = require('node:fs');
const path = require('node:path');
const code = require('./code-support');

const BIN = path.join(__dirname, '..', 'bin', 'contextshrink.js');

/** Runs `fn` with a fake API, a fake engine and a sandbox, and always cleans them up. */
async function withSandbox(fn, { manifest } = {}) {
  const api = await code.startCodeApi();
  const engine = await code.startFakeEngine();
  const sb = code.makeCodeSandbox(api, engine);
  sb.configDir = path.join(sb.home, '.claude');
  fs.mkdirSync(sb.configDir, { recursive: true });
  sb.env.CLAUDE_CONFIG_DIR = sb.configDir;
  sb.settings = path.join(sb.configDir, 'settings.json');
  sb.state = path.join(sb.configDir, '.contextshrink-npx.json');
  sb.desktopState = path.join(sb.configDir, '.horizon-vscode-claude.json');
  sb.legacyHome = path.join(sb.home, '.contextshrink', 'npx.json');
  // A manifest may be a function of the fake API, for a url that points at it.
  if (manifest) fs.writeFileSync(sb.manifestFile, JSON.stringify(typeof manifest === 'function' ? manifest(api) : manifest));
  try {
    await fn({ api, engine, sb });
  } finally {
    sb.cleanup();
    await engine.close();
    await api.close();
  }
}

function runCli(sb, args, options) {
  return code.runBin(BIN, sb, args, options);
}

function writeSettingsFile(sb, text) {
  fs.writeFileSync(sb.settings, text, 'utf8');
}

function readSettings(sb) {
  return code.readJson(sb.settings);
}

module.exports = { BIN, withSandbox, runCli, writeSettingsFile, readSettings, readJson: code.readJson, adapterCalls: code.adapterCalls };
