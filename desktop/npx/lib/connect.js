'use strict';

// connect, disconnect, status and logout for Claude Code's user settings.
//
// What connect writes, in settings.json under "env":
//   ANTHROPIC_BASE_URL       <proxy>/k/<keep-alive id>
//   ANTHROPIC_CUSTOM_HEADERS X-Horizon-Proxy-Token: <account key>
//   ENABLE_TOOL_SEARCH       "true"   (only when the user has not set it)
//
// Before writing, connect records the previous value of each key, and the
// values it wrote, in a state file next to settings.json. A key that connect
// did not write has no entry in the state's "written" map. Disconnect restores
// a key only while it still holds the value this installer wrote.

const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const cfg = require('./config');
const files = require('./files');
const api = require('./api');
const { Cancelled } = require('./prompt');

/** A refusal: the message is shown as is, and nothing was changed. */
class Refusal extends Error {}

const STATE_VERSION = 1;
const KEEPALIVE_SHAPE = /^[A-Za-z0-9_-]{8,64}$/;
const KEY_SHAPE = /^cs_live_[A-Za-z0-9_-]{20,200}$/;
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

// True when every key this installer wrote still holds its value.
function matchesWritten(block, written) {
  return Object.keys(written).every((key) => hasOwn(block, key) && block[key] === written[key]);
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

function readState(file) {
  const state = files.readOwnJson(file, 'contextshrink state file');
  if (!state) return null;
  // Files from before tool search was added have no record for ENABLE_TOOL_SEARCH.
  // That is accepted: a missing record means connect never wrote the key.
  const ok =
    state.version === STATE_VERSION &&
    files.isPlainObject(state.written) &&
    files.isPlainObject(state.previous) &&
    cfg.MANAGED_KEYS.every((key) => hasOwn(state.written, key)) &&
    Object.keys(state.written).every(
      (key) =>
        cfg.OWNED_KEYS.includes(key) &&
        typeof state.written[key] === 'string' &&
        files.isPlainObject(state.previous[key])
    );
  if (!ok) {
    throw new Refusal(`${file} is not in a format this version knows. Nothing was changed.`);
  }
  return state;
}

// The per-computer record: keep-alive id and device key id. Never the key.
function readHome(paths) {
  const home = files.readOwnJson(paths.home, 'contextshrink account file');
  return home || {};
}

function newKeepaliveId() {
  return crypto.randomBytes(8).toString('hex');
}

function deviceKeyName() {
  const host = (os.hostname() || 'this computer').slice(0, 60);
  return host + cfg.KEY_NAME_SUFFIX;
}

async function askCredentials(ctx) {
  const email = (await ctx.prompter.ask('Email: ')).trim();
  if (!email) throw new Cancelled();
  const password = await ctx.prompter.ask('Password: ', { hidden: true });
  if (!password) throw new Cancelled();
  return { email, password };
}

function refuseIfForeign(paths, block, state, force) {
  if (state || force) return;
  if (block && hasText(block[cfg.BASE_URL_KEY]) && !isContextShrinkHost(block[cfg.BASE_URL_KEY], paths.proxyUrl)) {
    throw new Refusal(
      `ANTHROPIC_BASE_URL already points at ${displayHost(block[cfg.BASE_URL_KEY])}, another gateway. ` +
        'Remove it, or add --force to replace it (disconnect restores the old value). Nothing was changed.'
    );
  }
  if (block && hasText(block[cfg.HEADERS_KEY])) {
    throw new Refusal(
      'ANTHROPIC_CUSTOM_HEADERS is already set. Remove it, or add --force to replace it ' +
        '(disconnect restores the old value). Nothing was changed.'
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

// The keys this run writes, and the previous value of each key it writes.
// `gateway` holds the two proxy keys. The previous values of a fresh connect come
// from settings.json; a re-connect keeps the values recorded by the first one.
function planWrite({ block, state, gateway, plan }) {
  const written = { ...gateway };
  const previous = state ? { ...state.previous } : {};
  if (!state) {
    for (const k of cfg.MANAGED_KEYS) previous[k] = entryOf(block || {}, k);
  }
  if (plan.action === 'write') {
    written[cfg.TOOL_SEARCH_KEY] = cfg.TOOL_SEARCH_VALUE;
    previous[cfg.TOOL_SEARCH_KEY] = entryOf(block || {}, cfg.TOOL_SEARCH_KEY);
  } else if (plan.action === 'keep') {
    written[cfg.TOOL_SEARCH_KEY] = state.written[cfg.TOOL_SEARCH_KEY];
  }
  return { written, previous };
}

function nextStateOf(paths, settings, block, state, written, previous) {
  return {
    version: STATE_VERSION,
    settings_existed: state ? state.settings_existed : settings.exists,
    env_existed: state ? state.env_existed : block !== undefined,
    previous,
    written,
    proxy_host: new URL(paths.proxyUrl).host,
    connected_at: new Date().toISOString(),
  };
}

function withKeys(block, written) {
  const next = block || {};
  for (const k of Object.keys(written)) next[k] = written[k];
  return next;
}

// Writes the state, then settings.json. The state goes first, so a settings write
// that fails leaves a state that matches the file; the old state is put back then.
function persist(paths, settings, state, nextState, nextBlock) {
  settings.payload.env = nextBlock;
  files.writePrivateJson(paths.state, nextState);
  try {
    files.writeSettings(paths.settings, settings);
  } catch (err) {
    if (state) files.writePrivateJson(paths.state, state);
    else files.removeFile(paths.state);
    throw err;
  }
}

/**
 * Connects Claude Code to the hosted proxy. `login` always mints a new key, even
 * when already connected; `keyFromStdin` uses a key the user pastes instead of
 * signing in.
 */
async function connect(ctx, { force = false, login = false, keyFromStdin = false } = {}) {
  const paths = cfg.resolvePaths(ctx.env);
  if (fs.existsSync(paths.desktopState)) {
    throw new Refusal(
      'The ContextShrink desktop app already connects Claude Code on this computer. ' +
        'Use the app to change that connection. Nothing was changed.'
    );
  }

  const settings = files.readSettings(paths.settings);
  const block = envBlockOf(settings.payload, paths.settings);
  const state = readState(paths.state);
  const home = readHome(paths);
  const plan = toolSearchPlan(block, state);

  if (state) {
    if (matchesWritten(block || {}, state.written)) {
      if (!login) {
        if (plan.action === 'write') {
          // Connected by an earlier version, which did not set tool search. Add it
          // now. The proxy keys stay as they are, so no sign-in is needed.
          const gateway = Object.fromEntries(cfg.MANAGED_KEYS.map((k) => [k, state.written[k]]));
          const { written, previous } = planWrite({ block, state, gateway, plan });
          const nextBlock = withKeys(block, written);
          persist(paths, settings, state, nextStateOf(paths, settings, block, state, written, previous), nextBlock);
          ctx.out('Turned on Claude Code tool search (ENABLE_TOOL_SEARCH=true).');
          return 0;
        }
        ctx.out('Already connected. Nothing to change.');
        reportToolSearch(ctx, plan);
        return 0;
      }
    } else if (!force) {
      throw new Refusal(
        'Claude Code settings changed since contextshrink connected them. ' +
          'Run "contextshrink disconnect" to restore them, or add --force to connect again. Nothing was changed.'
      );
    }
  } else {
    refuseIfForeign(paths, block, state, force);
  }

  let key;
  let deviceKeyId = null;
  let token = null;
  try {
    if (keyFromStdin) {
      const typed = (await ctx.prompter.ask('Account key (cs_live_...): ', { hidden: true })).trim();
      if (!KEY_SHAPE.test(typed)) {
        throw new Refusal('That is not a ContextShrink account key (it starts with cs_live_). Nothing was changed.');
      }
      key = typed;
    } else {
      const creds = await askCredentials(ctx);
      token = await api.signIn(paths.apiUrl, creds.email, creds.password);
      const created = await api.createDeviceKey(paths.apiUrl, token, deviceKeyName());
      key = created.key;
      deviceKeyId = created.id;
    }

    const keepalive =
      typeof home.keepalive_id === 'string' && KEEPALIVE_SHAPE.test(home.keepalive_id)
        ? home.keepalive_id
        : newKeepaliveId();
    const gateway = {
      [cfg.BASE_URL_KEY]: `${paths.proxyUrl}/k/${keepalive}`,
      [cfg.HEADERS_KEY]: `${cfg.PROXY_TOKEN_HEADER}: ${key}`,
    };
    const { written, previous } = planWrite({ block, state, gateway, plan });
    const nextState = nextStateOf(paths, settings, block, state, written, previous);
    const nextBlock = withKeys(block, written);

    // The key id is recorded first, so a failure below can still be cleaned up.
    files.writePrivateJson(paths.home, {
      version: 1,
      keepalive_id: keepalive,
      device_key_id: deviceKeyId,
      updated_at: new Date().toISOString(),
    });
    persist(paths, settings, state, nextState, nextBlock);

    // A key from an earlier connect is ours. Revoke it now the new one is in place.
    if (token && home.device_key_id && home.device_key_id !== deviceKeyId) {
      try {
        await api.revokeDeviceKey(paths.apiUrl, token, home.device_key_id);
      } catch {
        ctx.out('Could not revoke the key from the earlier connect. Revoke it on the API Keys page.');
      }
    }
    if (!token && home.device_key_id) {
      ctx.out('The key from the earlier login is still active. Revoke it on the API Keys page.');
    }
  } catch (err) {
    // A key made in this run must not stay active when nothing points at it.
    if (token && deviceKeyId) {
      try {
        await api.revokeDeviceKey(paths.apiUrl, token, deviceKeyId);
      } catch {
        // Reported below with the original error; the key can be revoked on the API Keys page.
      }
    }
    throw err;
  } finally {
    if (token) await api.signOut(paths.apiUrl, token);
  }

  ctx.out(login ? 'Saved a new account key for this computer.' : 'Connected.');
  ctx.out(`Claude Code now sends its requests to ${new URL(paths.proxyUrl).host}.`);
  if (plan.action === 'write') ctx.out('Tool search is on (ENABLE_TOOL_SEARCH=true).');
  reportToolSearch(ctx, plan);
  ctx.out('Start a new Claude Code session to use the connection.');
  return 0;
}

/** Restores the settings this installer changed. A key the user edited is left alone. */
async function disconnect(ctx) {
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
  for (const key of cfg.OWNED_KEYS) {
    // No record means connect never wrote this key (or an older version did not set it).
    if (!hasOwn(state.written, key)) continue;
    if (hasOwn(work, key) && work[key] === state.written[key]) {
      const prev = state.previous[key];
      if (prev.present) work[key] = prev.value;
      else delete work[key];
      restored.push(key);
    } else {
      left.push(key);
    }
  }

  if (restored.length) {
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

  if (restored.length) ctx.out(`Restored: ${restored.join(', ')}.`);
  else ctx.out('No settings needed restoring.');
  if (left.length) {
    ctx.out(`Left alone because they changed since connect: ${left.join(', ')}.`);
  }
  ctx.out('Your account key stays active until you run logout or revoke it on the API Keys page.');
  return 0;
}

/** Reports the connection state. Never prints the key. */
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
    const host = displayHost(state.written[cfg.BASE_URL_KEY]);
    const match = /\/k\/([A-Za-z0-9_-]+)$/.exec(state.written[cfg.BASE_URL_KEY]);
    const idPrefix = match ? `${match[1].slice(0, 4)}...` : 'none';
    if (matchesWritten(block, state.written)) {
      ctx.out(`Connected to ${host} (keep-alive id ${idPrefix}).`);
    } else {
      ctx.out(`Changed: settings.json no longer matches what contextshrink wrote (${host}).`);
      ctx.out('Run "contextshrink disconnect" to restore them.');
    }
  } else if (hasText(block[cfg.BASE_URL_KEY])) {
    ctx.out(`Not connected by contextshrink. ANTHROPIC_BASE_URL points at ${displayHost(block[cfg.BASE_URL_KEY])}.`);
  } else {
    ctx.out('Not connected.');
  }
  ctx.out(toolSearchLine(block));
  return 0;
}

function toolSearchLine(block) {
  if (block[cfg.TOOL_SEARCH_KEY] === cfg.TOOL_SEARCH_VALUE) return 'Tool search: on (ENABLE_TOOL_SEARCH is "true").';
  if (hasOwn(block, cfg.TOOL_SEARCH_KEY)) {
    return 'Tool search: off by your choice (ENABLE_TOOL_SEARCH is not "true" in settings.json).';
  }
  return 'Tool search: not set (ENABLE_TOOL_SEARCH is absent; connect sets it to "true").';
}

/**
 * Disconnects, then revokes this computer's account key. Revoking needs a
 * sign-in, so the user is asked for their credentials again.
 */
async function logout(ctx) {
  const paths = cfg.resolvePaths(ctx.env);
  await disconnect(ctx);
  const saved = files.readOwnJson(paths.home, 'contextshrink account file');
  if (!saved) {
    ctx.out('No saved contextshrink state to remove.');
    return 0;
  }
  if (!saved.device_key_id) {
    files.removeFile(paths.home);
    ctx.out('The saved key was pasted with "login --key", so it was not revoked here. Revoke it on the API Keys page.');
    return 0;
  }

  ctx.out('Sign in once more to revoke this computer\'s account key.');
  let creds;
  try {
    creds = await askCredentials(ctx);
  } catch (err) {
    if (err instanceof Cancelled) {
      ctx.out('Skipped. The account key stays active until you revoke it on the API Keys page. Run logout again to retry.');
      return 0;
    }
    throw err;
  }
  const token = await api.signIn(paths.apiUrl, creds.email, creds.password);
  try {
    await api.revokeDeviceKey(paths.apiUrl, token, saved.device_key_id);
  } finally {
    await api.signOut(paths.apiUrl, token);
  }
  files.removeFile(paths.home);
  ctx.out('Revoked the account key and removed the saved sign-in state.');
  return 0;
}

module.exports = { Refusal, connect, disconnect, status, logout, isContextShrinkHost, displayHost };
