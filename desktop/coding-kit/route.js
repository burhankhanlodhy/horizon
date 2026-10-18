#!/usr/bin/env node
'use strict';

// Shows whether Claude Code (terminal and Claude Desktop's Code tab) is routed
// through Vachi right now. It prints host names and setting names only, never a
// key, token or header value.
//
//   node route.js

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const home = os.homedir();

function host(url) {
  try {
    return new URL(url).host;
  } catch {
    return url ? '(not a URL)' : null;
  }
}

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return null;
  }
}

const settingsFile = path.join(home, '.claude', 'settings.json');
const settings = readJson(settingsFile) || {};
const env = settings.env || {};
const cliHost = host(env.ANTHROPIC_BASE_URL);
const cliVachi = Boolean(cliHost && cliHost.includes('vachi'));

const desktopConf = path.join(home, '.vachi', 'desktop', 'gateway.conf');
let desktopHost = null;
if (fs.existsSync(desktopConf)) {
  const line = fs
    .readFileSync(desktopConf, 'utf8')
    .split(/\r?\n/)
    .find((text) => text.startsWith('base_url='));
  desktopHost = line ? host(line.slice('base_url='.length).trim()) : null;
}
const desktopVachi = Boolean(desktopHost && desktopHost.includes('vachi'));

const processHost = host(process.env.ANTHROPIC_BASE_URL);

console.log('Claude Code settings (%s)', settingsFile);
console.log('  ANTHROPIC_BASE_URL host: %s', cliHost || '(not set: Anthropic directly)');
console.log('  env settings present: %s', Object.keys(env).sort().join(', ') || '(none)');
console.log('  Vachi plugin enabled: %s', settings.enabledPlugins && settings.enabledPlugins['vachi-chip@vachi'] ? 'yes' : 'no');
console.log('Claude Desktop Code tab (%s)', desktopConf);
console.log('  gateway host: %s', desktopHost || '(no Vachi gateway config)');
if (processHost) {
  // Usually inherited from the Claude Code session this terminal was opened in.
  console.log('This terminal also has ANTHROPIC_BASE_URL set (host %s).', processHost);
  console.log('  Run route.js from a normal terminal, not one opened from Claude Code.');
}
console.log('');
if (cliVachi || desktopVachi) {
  console.log('Vachi routing: ON  (terminal: %s, Code tab: %s)', cliVachi ? 'on' : 'off', desktopVachi ? 'on' : 'off');
} else {
  console.log('Vachi routing: OFF');
}
