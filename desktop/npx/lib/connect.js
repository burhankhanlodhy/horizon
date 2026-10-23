'use strict';

// connect, uninstall and status for Claude Code's user settings and Code tab.
//
// What connect writes, in settings.json:
//   env.ANTHROPIC_BASE_URL        http://127.0.0.1:18793 (the local engine)
//   env.ANTHROPIC_CUSTOM_HEADERS  removed when present (0.2.x wrote the account key here)
//   env.ENABLE_TOOL_SEARCH        "true"   (only when the user has not set it)
//   hooks.SessionStart            "<engine>\horizon.exe" code-engine ensure
// savings.js also manages the status line entry outside the env block.
//
// Before writing, connect records the previous value of each key it touches, and the
// value it wrote (null when it removed the key), in a state file next to settings.json.
// A key that connect did not write has no entry in the state's "written" map.
// Uninstall restores a key only while it still holds the value this installer wrote.

const fs = require('node:fs');
const cfg = require('./config');
const files = require('./files');
const codetab = require('./codetab');
const savings = require('./savings');
const device = require('./code-device');
const engine = require('./code-engine');
const { CsError, platformOf } = require('./code-env');

/** A refusal: the message is shown as is, and nothing was changed. */
class Refusal extends CsError {}

const STATE_VERSION = 2;
const { hasOwn } = files;

function hasText(value) {
  return typeof value === 'string' && value.trim() !== '';
}

// Shows only the host of a URL, never its path or any credentials in it.
function displayHost(value) {
  try {
    return new URL(value).host || 'an unknown host';
  } catch {
    return 'a value that is not a URL';
  }
}

function isContextShrinkHost(value, proxyUrl) {
  let url;
  try {
    url = new URL(value);
  } catch {
    return false;
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') return false;
  if (url.host === new URL(proxyUrl).host) return true;
  return url.hostname === 'contextshrink.com' || url.hostname.endsWith('.contextshrink.com');
}

// A recorded value of null means "connect removed this key": it holds while the key is absent.
function holds(block, key, value) {
  if (value === null) return !hasOwn(block, key);
  return hasOwn(block, key) && block[key] === value;
}

// True when every key this installer wrote still holds its value.
function matchesWritten(block, written) {
  return Object.keys(written).every((key) => holds(block, key, written[key]));
}

function entryOf(block, key) {
  return hasOwn(block, key) ? { present: true, value: block[key] } : { present: false };
}

function envBlockOf(payload, settingsFile) {
  const block = payload.env;
  if (block === undefined) return undefined;
  if (!files.isPlainObject(block)) {
    throw new Refusal(`"env" in ${settingsFile} is not an object, so it was not touched.`);
  }
  return block;
}

function hooksOf(payload, settingsFile) {
  const hooks = payload.hooks;
  if (hooks === undefined) return undefined;
  if (!files.isPlainObject(hooks)) {
    throw new Refusal(`"hooks" in ${settingsFile} is not an object, so it was not touched.`);
  }
  if (hooks.SessionStart !== undefined && !Array.isArray(hooks.SessionStart)) {
    throw new Refusal(`"hooks.SessionStart" in ${settingsFile} is not a list, so it was not touched.`);
  }
  return hooks;
}

function readState(file) {
  const state = files.readOwnJson(file, 'contextshrink state file');
  if (!state) return null;
  // Version 1 (0.2.x) recorded the account key header; version 2 records null for keys it removed.
  const ok =
    [1, STATE_VERSION].includes(state.version) &&
    files.isPlainObject(state.written) &&
    files.isPlainObject(state.previous) &&
    cfg.MANAGED_KEYS.every((key) => hasOwn(state.written, key)) &&
    Object.keys(state.written).every(
      (key) =>
        cfg.OWNED_KEYS.includes(key) &&
        (state.written[key] === null || typeof state.written[key] === 'string') &&
        files.isPlainObject(state.previous[key])
    ) &&
    (state.hook === undefined ||
      (files.isPlainObject(state.hook) &&
        files.isPlainObject(state.hook.entry) &&
        typeof state.hook.hooks_existed === 'boolean' &&
        typeof state.hook.session_start_existed === 'boolean'));
  if (!ok) {
    throw new Refusal(`${file} is not in a format this version knows. Nothing was changed.`);
  }
  return state;
}

function refuseIfForeign(paths, block, force) {
  if (force) return;
  const base = block && block[cfg.BASE_URL_KEY];
  if (hasText(base) && base !== paths.engineUrl && !isContextShrinkHost(base, paths.proxyUrl)) {
    throw new Refusal(
      `ANTHROPIC_BASE_URL already points at ${displayHost(base)}, another gateway. ` +
        'Remove it, or add --force to replace it (uninstall restores the old value). Nothing was changed.'
    );
  }
  if (block && hasText(block[cfg.HEADERS_KEY])) {
    throw new Refusal(
      'ANTHROPIC_CUSTOM_HEADERS is already set. Remove it, or add --force to replace it ' +
        '(uninstall restores the old value). Nothing was changed.'
    );
  }
}

// Decides what connect does with ENABLE_TOOL_SEARCH. --force does not change this.
//   keep        connect wrote it earlier and still records it
//   write       the key is absent: connect sets it to "true" and records that it did
//   leave-true  the user set it to "true": left as it is, and not recorded
//   leave-user  the user set it to anything else: left as it is, and reported
function toolSearchPlan(block, state) {
  if (state && hasOwn(state.written, cfg.TOOL_SEARCH_KEY)) return { action: 'keep' };
  if (block && hasOwn(block, cfg.TOOL_SEARCH_KEY)) {
    return { action: block[cfg.TOOL_SEARCH_KEY] === cfg.TOOL_SEARCH_VALUE ? 'leave-true' : 'leave-user' };
  }
  return { action: 'write' };
}

function reportToolSearch(ctx, plan) {
  if (plan.action === 'leave-user') {
    ctx.out(
      'Tool search is off by your choice (ENABLE_TOOL_SEARCH in settings.json is not "true"), ' +
        'so the savings figures will include tool-schema deferral.'
    );
  }
}

// The values this run writes (null for a key it removes), and the previous value of
// every key the state records. A re-connect keeps the values recorded by the first one.
function planWrite({ block, state, plan, engineUrl }) {
  const work = block || {};
  const written = { ...(state ? state.written : {}) };
  written[cfg.BASE_URL_KEY] = engineUrl;
  if (hasOwn(work, cfg.HEADERS_KEY) || (state && hasOwn(state.written, cfg.HEADERS_KEY))) {
    written[cfg.HEADERS_KEY] = null;
  }
  if (plan.action === 'write') written[cfg.TOOL_SEARCH_KEY] = cfg.TOOL_SEARCH_VALUE;
  const previous = { ...(state ? state.previous : {}) };
  for (const key of Object.keys(written)) {
    if (!hasOwn(previous, key)) previous[key] = entryOf(work, key);
  }
  return { written, previous };
}

// The SessionStart entry connect appends. The first connect records whether
// hooks and SessionStart existed, so uninstall can remove them again.
function planHook(payload, state, entry) {
  const prior = state && state.hook ? state.hook : null;
  return {
    entry,
    hooks_existed: prior ? prior.hooks_existed : payload.hooks !== undefined,
    session_start_existed: prior
      ? prior.session_start_existed
      : Boolean(payload.hooks && payload.hooks.SessionStart !== undefined),
  };
}

function applyHook(payload, oldEntry, entry) {
  if (payload.hooks === undefined) payload.hooks = {};
  const current = Array.isArray(payload.hooks.SessionStart) ? payload.hooks.SessionStart : [];
  const same = (item) => JSON.stringify(item) === JSON.stringify(entry);
  const sameOld = (item) => oldEntry && JSON.stringify(item) === JSON.stringify(oldEntry);
  payload.hooks.SessionStart = current.filter((item) => !same(item) && !sameOld(item)).concat([entry]);
}

// true: our entry was removed. false: it was not found (the user removed or edited it).
function restoreHook(payload, hook) {
  const hooks = payload.hooks;
  if (!files.isPlainObject(hooks) || !Array.isArray(hooks.SessionStart)) return false;
  const json = JSON.stringify(hook.entry);
  const list = hooks.SessionStart.filter((item) => JSON.stringify(item) !== json);
  if (list.length === hooks.SessionStart.length) return false;
  if (list.length === 0 && !hook.session_start_existed) delete hooks.SessionStart;
  else hooks.SessionStart = list;
  if (Object.keys(hooks).length === 0 && !hook.hooks_existed) delete payload.hooks;
  return true;
}

function nextStateOf(state, settings, block, written, previous, hook, engineUrl) {
  // A re-connect keeps the first connect's record of what existed before it.
  return {
    version: STATE_VERSION,
    settings_existed: state ? state.settings_existed : settings.exists,
    env_existed: state ? state.env_existed : block !== undefined,
    previous,
    written,
    hook,
    engine_url: engineUrl,
    connected_at: state && state.connected_at ? state.connected_at : new Date().toISOString(),
    ...(state && state.ui ? { ui: state.ui } : {}),
  };
}

// Writes the state, then settings.json. The state goes first, so a settings write
// that fails leaves a state that matches the file; the old state is put back then.
function persist(paths, settings, state, nextState, settingsChanged) {
  files.writePrivateJson(paths.state, nextState);
  if (!settingsChanged) return;
  try {
    files.writeSettings(paths.settings, settings);
  } catch (err) {
    if (state) files.writePrivateJson(paths.state, state);
    else files.removeFile(paths.state);
    throw err;
  }
}

async function finishConnect(ctx, paths, options) {
  const code = await codetab.setUpCodeTab(ctx, { settings: paths.settings, engineUrl: paths.engineUrl }, options);
  savings.ensure(ctx, paths, { codeTab: !options.skip });
  return code;
}

// Removes the 0.2.x per-computer record. Its account key stays active until revoked there.
function retireLegacyRecord(ctx, paths) {
  let legacy = null;
  try {
    legacy = files.readOwnJson(paths.legacyHome, 'contextshrink account file');
  } catch {
    legacy = null;
  }
  if (!legacy) return;
  files.removeFile(paths.legacyHome);
  if (legacy.device_key_id) {
    ctx.out('The account key from the earlier version is no longer used. Revoke it on the API Keys page.');
  }
}

/**
 * Connects Claude Code to the local engine on this computer. Windows only. Order:
 * device sign-in, engine install, plan note, engine start, Startup shortcut, then
 * the settings. Nothing is written to Claude Code until the engine answers.
 */
async function connect(ctx, { force = false, codeTab = true, deps = {} } = {}) {
  const env = ctx.env;
  const paths = cfg.resolvePaths(env);
  if (platformOf(env) !== 'win32') {
    throw new Refusal('ContextShrink Code runs on Windows today. macOS and Linux are coming.');
  }
  const manifest = engine.loadManifest(env);
  if (!manifest.published) throw new Refusal(engine.UNPUBLISHED);
  if (fs.existsSync(paths.desktopState)) {
    throw new Refusal(
      'The ContextShrink desktop app already connects Claude Code on this computer. ' +
        'Use the app to change that connection. Nothing was changed.'
    );
  }

  const adapter = deps.adapter || engine.resolveAdapter(env);
  const settings = files.readSettings(paths.settings);
  const block = envBlockOf(settings.payload, paths.settings);
  hooksOf(settings.payload, paths.settings);
  const state = readState(paths.state);
  const plan = toolSearchPlan(block, state);
  if (state) {
    if (!matchesWritten(block || {}, state.written) && !force) {
      throw new Refusal(
        'Claude Code settings changed since contextshrink connected them. ' +
          'Run "npx contextshrink-claude uninstall" to restore them, or add --force to connect again. Nothing was changed.'
      );
    }
  } else {
    refuseIfForeign(paths, block, force);
  }

  const beforeJson = JSON.stringify(settings.payload);
  const { note } = await device.ensureDevice(ctx, env, {
    tool: 'claude',
    toolName: 'Claude',
    commandName: 'contextshrink-claude',
    deps: { adapter, sleep: deps.sleep },
  });
  const exe = await engine.installEngine(ctx, env, manifest, adapter);
  device.applyPlanNote(ctx, env, note, 'Claude');
  await engine.startEngine(env, exe, adapter);
  await engine.installStartup(env, exe, adapter);

  const entry = { hooks: [{ type: 'command', command: `"${exe}" code-engine ensure` }] };
  const { written, previous } = planWrite({ block, state, plan, engineUrl: paths.engineUrl });
  const hook = planHook(settings.payload, state, entry);
  const nextBlock = block || {};
  for (const key of Object.keys(written)) {
    if (written[key] === null) delete nextBlock[key];
    else nextBlock[key] = written[key];
  }
  settings.payload.env = nextBlock;
  applyHook(settings.payload, state && state.hook ? state.hook.entry : null, entry);
  const settingsChanged = JSON.stringify(settings.payload) !== beforeJson;
  const nextState = nextStateOf(state, settings, block, written, previous, hook, paths.engineUrl);
  const stateChanged = !state || JSON.stringify(nextState) !== JSON.stringify(state);
  if (settingsChanged || stateChanged) persist(paths, settings, state, nextState, settingsChanged);
  retireLegacyRecord(ctx, paths);

  if (!settingsChanged && state) {
    ctx.out('Already connected. Nothing to change in Claude Code settings.');
  } else {
    ctx.out('Connected. Claude Code now sends its requests through the ContextShrink engine on this computer.');
    if (plan.action === 'write') ctx.out('Tool search is on (ENABLE_TOOL_SEARCH=true).');
    ctx.out('Start a new Claude Code session to use the connection.');
  }
  reportToolSearch(ctx, plan);
  return finishConnect(ctx, paths, { skip: !codeTab });
}

/**
 * Restores the Code tab (Windows) and the settings this installer changed, then
 * leaves the device: the device is released only when no ContextShrink tool is left.
 */
async function uninstall(ctx, { deps = {} } = {}) {
  const undone = await codetab.undoCodeTab(ctx);
  await disconnectTerminal(ctx);
  if (undone !== 0) {
    // The device stays registered while the Code tab still points at the engine.
    ctx.err('Device not released, because the Code tab is still set up. Run uninstall again after fixing it.');
    return undone;
  }
  await device.leaveDevice(ctx, ctx.env, { tool: 'claude', deps });
  return 0;
}

async function disconnectTerminal(ctx) {
  const paths = cfg.resolvePaths(ctx.env);
  const state = readState(paths.state);
  if (!state) {
    ctx.out('Not connected by contextshrink. Nothing to restore.');
    return 0;
  }

  const settings = files.readSettings(paths.settings);
  const block = envBlockOf(settings.payload, paths.settings);
  const work = block || {};
  const restored = [];
  const left = [];
  const uiResult = savings.restore(settings.payload, state.ui);
  for (const key of cfg.OWNED_KEYS) {
    // No record means connect never wrote this key (or an older version did not set it).
    if (!hasOwn(state.written, key)) continue;
    if (holds(work, key, state.written[key])) {
      const prev = state.previous[key];
      if (prev.present) work[key] = prev.value;
      else delete work[key];
      restored.push(key);
    } else {
      left.push(key);
    }
  }
  let hookChanged = false;
  if (state.hook) {
    if (restoreHook(settings.payload, state.hook)) hookChanged = true;
    else left.push('the SessionStart hook');
  }

  if (restored.length || uiResult.changed || hookChanged) {
    if (state.env_existed === false && Object.keys(work).length === 0) {
      delete settings.payload.env;
    } else {
      settings.payload.env = work;
    }
    if (!state.settings_existed && Object.keys(settings.payload).length === 0) {
      files.removeFile(paths.settings);
    } else {
      files.writeSettings(paths.settings, settings);
    }
  }
  files.removeFile(paths.state);
  savings.cleanup(paths, settings.payload);

  if (restored.length || hookChanged) ctx.out(`Restored: ${[...restored, ...(hookChanged ? ['SessionStart hook'] : [])].join(', ')}.`);
  else ctx.out('No settings needed restoring.');
  if (left.length) ctx.out(`Left alone because they changed since connect: ${left.join(', ')}.`);
  if (uiResult.left.length) ctx.out('Kept savings-bar settings you changed since connect.');
  return 0;
}

/** Reports the connection, the device and the engine. Never prints the device key. */
async function status(ctx) {
  const paths = cfg.resolvePaths(ctx.env);
  if (fs.existsSync(paths.desktopState)) {
    ctx.out('Managed by the ContextShrink desktop app (Connect). This installer leaves it alone.');
    return 0;
  }
  const state = readState(paths.state);
  const settings = files.readSettings(paths.settings);
  const block = envBlockOf(settings.payload, paths.settings) || {};

  if (state) {
    if (matchesWritten(block, state.written)) {
      ctx.out(`Connected through the ContextShrink engine at ${displayHost(paths.engineUrl)}.`);
    } else {
      ctx.out('Changed: settings.json no longer matches what contextshrink wrote.');
      ctx.out('Run "npx contextshrink-claude uninstall" to restore them.');
    }
  } else if (hasText(block[cfg.BASE_URL_KEY])) {
    ctx.out(`Not connected by contextshrink. ANTHROPIC_BASE_URL points at ${displayHost(block[cfg.BASE_URL_KEY])}.`);
  } else {
    ctx.out('Not connected.');
  }
  ctx.out(toolSearchLine(block));
  if (state) ctx.out(savings.describe(settings.payload, state.ui));
  await device.reportDevice(ctx, ctx.env, { toolName: 'Claude' });
  await codetab.codeTabStatus(ctx);
  return 0;
}

function toolSearchLine(block) {
  if (block[cfg.TOOL_SEARCH_KEY] === cfg.TOOL_SEARCH_VALUE) return 'Tool search: on (ENABLE_TOOL_SEARCH is "true").';
  if (hasOwn(block, cfg.TOOL_SEARCH_KEY)) {
    return 'Tool search: off by your choice (ENABLE_TOOL_SEARCH is not "true" in settings.json).';
  }
  return 'Tool search: not set (ENABLE_TOOL_SEARCH is absent; connect sets it to "true").';
}

module.exports = { Refusal, connect, uninstall, status, isContextShrinkHost, displayHost };
