'use strict';

const fs = require('node:fs');
const path = require('node:path');
const files = require('./files');
const NAME = 'contextshrink-npx';
const PLUGIN = `${NAME}@${NAME}`;
const VERSION = require('../package.json').version;
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const directoryOf = paths => path.join(paths.csHome, 'npx-ui');
const fields = [['statusLine', null], ['extraKnownMarketplaces', NAME], ['enabledPlugins', PLUGIN]];

function valueAt(payload, group, name) {
  return name === null ? payload[group] : payload[group]?.[name];
}

// The ContextShrink desktop app's own savings line (desktop/claude-code-plugin), enabled
// through its plugin marketplace. When it is on, a second line above the prompt would
// duplicate it, so the npx line is not added.
const DESKTOP_PLUGIN = 'contextshrink@contextshrink';
function desktopLineOn(payload) {
  return files.isPlainObject(payload.enabledPlugins) && payload.enabledPlugins[DESKTOP_PLUGIN] === true;
}

// Why the npx plugin cannot be registered, or null when it can. The plugin is display-only
// and loads in every Claude Code instance (terminal and the Desktop Code tab), so it does
// not depend on which program routes the Code tab. Only a conflict stops it.
function pluginConflict(payload, ours) {
  const marketplace = payload.extraKnownMarketplaces;
  const enabled = payload.enabledPlugins;
  if (marketplace !== undefined && !files.isPlainObject(marketplace)) return 'the marketplace settings are not in the expected format';
  if (enabled !== undefined && !files.isPlainObject(enabled)) return 'the plugin settings are not in the expected format';
  if (marketplace?.[NAME] !== undefined && !same(marketplace[NAME], ours.extraKnownMarketplaces)) {
    return `a "${NAME}" marketplace in Claude Code settings already points elsewhere`;
  }
  if (enabled?.[PLUGIN] !== undefined && enabled[PLUGIN] !== true) {
    return `"${PLUGIN}" is already set in Claude Code settings`;
  }
  return null;
}

function installRuntime(paths) {
  const dir = directoryOf(paths);
  const runtime = path.join(dir, 'savings-runtime.cjs');
  files.writePrivateText(runtime, fs.readFileSync(path.join(__dirname, 'savings-runtime.cjs'), 'utf8'));
  files.writePrivateText(path.join(dir, 'local-savings.cjs'), fs.readFileSync(path.join(__dirname, 'local-savings.cjs'), 'utf8'));
  files.writePrivateJson(path.join(dir, 'ui-config.json'), {
    state: paths.state, settings: paths.settings, device: paths.device, license: paths.license, tool: 'claude',
  });
  const plugin = path.join(dir, 'plugin');
  files.writePrivateJson(path.join(plugin, '.claude-plugin', 'plugin.json'), {
    name: NAME, version: VERSION, description: 'ContextShrink savings for this API key today',
  });
  files.writePrivateJson(path.join(plugin, '.claude-plugin', 'marketplace.json'), {
    name: NAME, owner: { name: 'ContextShrink' }, plugins: [{ name: NAME, source: '.', version: VERSION }],
  });
  files.writePrivateJson(path.join(plugin, 'hooks', 'hooks.json'), {
    description: 'ContextShrink savings above the prompt', modules: ['./register.js'],
  });
  const template = fs.readFileSync(path.join(__dirname, 'bar-register.js'), 'utf8');
  files.writePrivateText(path.join(plugin, 'hooks', 'register.js'), template
    .replace('__NODE__', () => JSON.stringify(process.execPath))
    .replace('__RENDER__', () => JSON.stringify(runtime)));
  // Claude Code runs statusLine commands in a shell. Windows paths with forward
  // slashes also work in Git Bash; quote each argument independently.
  const quote = text => process.platform === 'win32'
    ? '"' + text.replace(/\\/g, '/').replace(/[$`]/g, '\\$&') + '"'
    : "'" + text.replace(/'/g, "'\\''") + "'";
  return {
    statusLine: { type: 'command', command: `${quote(process.execPath)} ${quote(runtime)}` },
    extraKnownMarketplaces: { source: { source: 'directory', path: plugin } },
    enabledPlugins: true,
  };
}

// Install only absent settings. Records live in the existing transaction/state,
// so old 0.2.0 connections can upgrade without signing in or rotating their key.
function ensure(ctx, paths, { codeTab = true } = {}) {
  const state = files.readOwnJson(paths.state, 'contextshrink state file');
  if (!state) return;
  const settings = files.readSettings(paths.settings);
  const previousState = JSON.parse(JSON.stringify(state));
  const values = installRuntime(paths);
  const previous = Array.isArray(state.ui) ? state.ui : [];
  const records = [];
  // --no-code-tab leaves the line out on purpose. Otherwise a reason is given only when the
  // line cannot be added: the desktop app's own line is on, or a conflicting entry exists.
  let skipReason = null;
  if (codeTab && desktopLineOn(settings.payload)) {
    skipReason = 'the ContextShrink desktop app already shows its savings line above the prompt';
  } else if (codeTab) {
    skipReason = pluginConflict(settings.payload, values);
  }
  const skipPlugin = !codeTab || skipReason !== null;
  for (const [group, name] of fields) {
    const old = previous.find(entry => entry.group === group && entry.name === name);
    const current = valueAt(settings.payload, group, name);
    if (old && same(current, old.value)) { records.push(old); continue; }
    if ((name !== null && skipPlugin) || current !== undefined) continue;
    if (name !== null && files.hasOwn(settings.payload, group) && !files.isPlainObject(settings.payload[group])) continue;
    const groupExisted = files.hasOwn(settings.payload, group);
    if (name === null) settings.payload[group] = values[group];
    else {
      if (!groupExisted) settings.payload[group] = {};
      settings.payload[group][name] = values[group];
    }
    records.push({ group, name, value: values[group], groupExisted });
  }
  state.ui = records;
  if (!same(state, previousState)) {
    files.writePrivateJson(paths.state, state);
    try { files.writeSettings(paths.settings, settings); }
    catch (err) { files.writePrivateJson(paths.state, previousState); throw err; }
  }
  const addedNow = records.filter(entry => !previous.some(p => p.group === entry.group && p.name === entry.name));
  if (addedNow.some(entry => entry.name !== null)) {
    ctx.out('Savings line added above the prompt in Claude Code and the Desktop Code tab (new sessions).');
  } else if (skipReason && !records.some(entry => entry.name !== null)) {
    ctx.out(`Savings line not added: ${skipReason}.`);
  }
  if (!same(state, previousState) && !addedNow.some(entry => entry.name !== null)) {
    ctx.out(records.length
      ? 'Savings bar configured. It shows your usage saved today; open a new session.'
      : 'Savings bar: your existing display settings were kept.');
  }
}

function restore(payload, record) {
  let changed = false;
  const left = [];
  for (const [group, name] of fields) {
    const entry = Array.isArray(record) && record.find(item => item.group === group && item.name === name);
    if (!entry) continue;
    if (!same(valueAt(payload, group, name), entry.value)) { left.push(group); continue; }
    if (name === null) delete payload[group];
    else {
      delete payload[group][name];
      if (!entry.groupExisted && Object.keys(payload[group]).length === 0) delete payload[group];
    }
    changed = true;
  }
  return { changed, left };
}

function cleanup(paths, payload) {
  const dir = directoryOf(paths);
  // Keep edited user settings that still point at our runtime working. Remove
  // only fixed, known files within this installation, never recursively.
  const text = JSON.stringify(payload).replace(/\\\\/g, '/');
  if (text.includes(dir.replace(/\\/g, '/'))) return;
  for (const relative of ['summary-cache.json', 'ui-config.json', 'savings-runtime.cjs', 'local-savings.cjs',
    'plugin/hooks/register.js', 'plugin/hooks/hooks.json',
    'plugin/.claude-plugin/plugin.json', 'plugin/.claude-plugin/marketplace.json']) {
    files.removeFile(path.join(dir, relative));
  }
  for (const relative of ['plugin/hooks', 'plugin/.claude-plugin', 'plugin', '.']) {
    try { fs.rmdirSync(path.join(dir, relative)); } catch {}
  }
}

function describe(payload, record) {
  const owns = group => Array.isArray(record) && record.some(entry => entry.group === group &&
    same(valueAt(payload, entry.group, entry.name), entry.value));
  return `Savings bar: terminal ${owns('statusLine') ? 'configured' : 'left alone'}; ` +
    `Code tab ${owns('extraKnownMarketplaces') && owns('enabledPlugins') ? 'configured' : 'left alone'}; ` +
    'usage saved today.';
}

module.exports = { ensure, restore, cleanup, directoryOf, describe };
