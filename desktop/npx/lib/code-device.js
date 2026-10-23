'use strict';

// This computer's ContextShrink device: its hash, device.json, the plan note
// (license.json), and the shared refcount that decides when uninstall releases the
// device. Both npx packages share one device.json, so a device is released only
// after the last ContextShrink tool is removed from it.
//
// The device hash is the only machine identifier sent to the server. Its rule is
// fixed (see the spec, section 1): sha256 of "contextshrink-device-v1\n" plus the
// machine ID, stripped and lower-cased.

const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { CsError, contextshrinkHome, apiUrlOf, codeSiteUrlOf } = require('./code-env');
const files = require('./files');
const login = require('./code-login');
const engine = require('./code-engine');

const DEVICE_VERSION = 1;
const HASH_PREFIX = 'contextshrink-device-v1\n';
const HASH_SHAPE = /^[0-9a-f]{64}$/;
const TOOL_LABELS = { claude: 'Claude Code', codex: 'Codex' };

function machineIdOf(env) {
  const override = env.CONTEXTSHRINK_MACHINE_ID;
  if (typeof override === 'string' && override !== '') return override;
  const unreadable = new CsError('This computer\'s ID could not be read. Nothing was changed.');
  if (process.platform === 'win32') {
    const reg = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'reg.exe');
    let out;
    try {
      out = execFileSync(reg, ['query', 'HKLM\\SOFTWARE\\Microsoft\\Cryptography', '/v', 'MachineGuid', '/reg:64'], {
        encoding: 'utf8',
        windowsHide: true,
        timeout: 10000,
      });
    } catch {
      throw unreadable;
    }
    const match = /MachineGuid\s+REG_SZ\s+(\S+)/.exec(out);
    if (match) return match[1];
    throw unreadable;
  }
  if (process.platform === 'darwin') {
    let out;
    try {
      out = execFileSync('ioreg', ['-rd1', '-c', 'IOPlatformExpertDevice'], { encoding: 'utf8', timeout: 10000 });
    } catch {
      throw unreadable;
    }
    const match = /"IOPlatformUUID"\s*=\s*"([^"]+)"/.exec(out);
    if (match) return match[1];
    throw unreadable;
  }
  for (const file of ['/etc/machine-id', '/var/lib/dbus/machine-id']) {
    try {
      const value = fs.readFileSync(file, 'utf8').trim();
      if (value) return value;
    } catch {
      // Try the next location.
    }
  }
  throw unreadable;
}

/** lowercase hex sha256 of "contextshrink-device-v1\n" + the normalized machine ID. */
function deviceHashOf(machineId) {
  const normalized = String(machineId).trim().toLowerCase();
  if (!normalized) throw new CsError('This computer\'s ID could not be read. Nothing was changed.');
  return crypto.createHash('sha256').update(HASH_PREFIX + normalized, 'utf8').digest('hex');
}

function devicePathsOf(env) {
  const home = contextshrinkHome(env);
  return {
    home,
    device: path.join(home, 'device.json'),
    license: path.join(home, 'license.json'),
    usage: path.join(home, 'usage-pending.json'),
  };
}

function validDevice(data) {
  return (
    data.version === DEVICE_VERSION &&
    typeof data.device_id === 'string' &&
    /^cs_dev_[A-Za-z0-9_-]{16,200}$/.test(data.device_key || '') &&
    typeof data.device_hash === 'string' &&
    HASH_SHAPE.test(data.device_hash) &&
    Array.isArray(data.tools) &&
    data.tools.every((tool) => typeof tool === 'string')
  );
}

/** device.json, or null when this computer is not connected. Throws when the file is broken. */
function readDevice(env) {
  const data = files.readOwnJson(devicePathsOf(env).device, 'ContextShrink device file');
  if (!data) return null;
  if (!validDevice(data)) {
    throw new files.FileError('device.json is not in the expected format. Remove it and run connect again. Nothing was changed.');
  }
  return data;
}

function writeDevice(env, device) {
  files.writePrivateJson(devicePathsOf(env).device, device);
}

function writeLicense(env, token) {
  files.writePrivateJson(devicePathsOf(env).license, {
    version: 1,
    token,
    fetched_at: new Date().toISOString(),
  });
}

function removeDeviceFiles(env) {
  const paths = devicePathsOf(env);
  files.removeFile(paths.device);
  files.removeFile(paths.license);
  files.removeFile(paths.usage);
}

// Where calls for a device go: the address the device was registered with.
function baseOf(device, env) {
  return (device && device.api_url) || apiUrlOf(env);
}

// The plan in license.json, read from its payload only (the engine checks the signature).
function readPlanNote(env, now = Date.now()) {
  let data;
  try {
    data = files.readOwnJson(devicePathsOf(env).license, 'ContextShrink plan file');
  } catch {
    return null;
  }
  if (!data || typeof data.token !== 'string') return null;
  try {
    const payload = JSON.parse(Buffer.from(data.token.split('.')[0], 'base64url').toString('utf8'));
    if (payload.plan !== 'trial' && payload.plan !== 'active') return null;
    return { plan: payload.plan, until: Number(payload.until) * 1000, now };
  } catch {
    return null;
  }
}

function daysLeft(untilMs, now = Date.now()) {
  return Math.max(0, Math.ceil((untilMs - now) / 86400000));
}

/**
 * Shows the plan from a note: "Free trial: N days left", "Subscribed", or the
 * trial-ended text (402). Writes or removes license.json to match. Returns the plan.
 */
function applyPlanNote(ctx, env, note, toolName, now = Date.now()) {
  if (note.status === 200 && note.body && typeof note.body.token === 'string' && note.body.token) {
    const { plan, until } = note.body;
    if (plan === 'active') {
      writeLicense(env, note.body.token);
      ctx.out('Subscribed');
      return 'active';
    }
    if (plan === 'trial' && Number.isFinite(Number(until))) {
      writeLicense(env, note.body.token);
      const days = daysLeft(Number(until) * 1000, now);
      ctx.out(`Free trial: ${days} ${days === 1 ? 'day' : 'days'} left`);
      return 'trial';
    }
  }
  if (note.status === 402) {
    files.removeFile(devicePathsOf(env).license);
    ctx.out(
      `Your trial has ended. Subscribe for $5/month at ${codeSiteUrlOf(env)}/account. ` +
        `Until then, ${toolName} works normally without compression.`
    );
    return 'ended';
  }
  throw new CsError('ContextShrink could not confirm your plan right now. Nothing was changed. Run this command again.');
}

/**
 * Makes sure this computer has a valid device for `tool`. A device.json that the
 * server no longer accepts (401/403) is replaced by a fresh browser sign-in.
 * Returns { device, note } where note is the plan note answer.
 */
async function ensureDevice(ctx, env, { tool, toolName, commandName, deps = {} }) {
  const hash = deviceHashOf(machineIdOf(env));
  const adapter = deps.adapter || engine.resolveAdapter(env);
  let device = readDevice(env);
  let stale = null;
  let note = null;
  if (device && device.device_hash !== hash) {
    // The same file from another machine: it is a different device, so sign in again.
    stale = device;
    device = null;
  }
  if (device) {
    note = await login.fetchPlanNote(baseOf(device, env), device.device_key, hash);
    if (note.status === 401 || note.status === 403) {
      ctx.out("This computer's ContextShrink sign-in has expired. Signing in again.");
      device = null;
      note = null;
    }
  }
  if (!device) {
    const approved = await login.runBrowserLogin(ctx, {
      apiUrl: apiUrlOf(env),
      tool,
      deviceHash: hash,
      deviceName: os.hostname(),
      commandName,
      openBrowser: adapter.openBrowser,
      sleep: deps.sleep,
      env,
    });
    device = {
      version: DEVICE_VERSION,
      api_url: approved.api_url,
      device_id: approved.device_id,
      device_key: approved.device_key,
      device_hash: hash,
      tools: [],
      created_at: new Date().toISOString(),
    };
    note = await login.fetchPlanNote(baseOf(device, env), device.device_key, hash);
    if (note.status === 401 || note.status === 403) {
      throw new CsError('ContextShrink refused the new sign-in for this computer. Nothing was changed.');
    }
  }
  if (!device.tools.includes(tool)) device.tools = [...device.tools, tool];
  writeDevice(env, device);
  if (stale) {
    try {
      await login.releaseDevice(baseOf(stale, env), stale.device_key);
    } catch {
      ctx.out('The device this file belonged to could not be released. Remove it at the account page.');
    }
  }
  return { device, note };
}

/**
 * Uninstall for one tool. Removes the tool from device.json. When no tool is left,
 * releases the device on the server (a network failure is reported, not fatal),
 * stops the engine, removes the Startup shortcut and the engine, and deletes the
 * device files. Returns true when the device was released locally.
 */
async function leaveDevice(ctx, env, { tool, deps = {} }) {
  const device = readDevice(env);
  const others = device ? device.tools.filter((name) => name !== tool) : [];
  if (others.length) {
    if (others.length !== device.tools.length) writeDevice(env, { ...device, tools: others });
    const labels = others.map((name) => TOOL_LABELS[name] || name).join(', ');
    ctx.out(`${labels} still connected on this device, so the device and the engine stay in place.`);
    return false;
  }
  const adapter = deps.adapter || engine.resolveAdapter(env);
  if (device) {
    try {
      await login.releaseDevice(baseOf(device, env), device.device_key);
    } catch {
      ctx.out(`Could not reach ContextShrink to release this device. Release it at ${codeSiteUrlOf(env)}/account.`);
    }
  }
  await engine.stopEngine(env, adapter);
  engine.removeStartup(env);
  engine.removeEngine(env);
  removeDeviceFiles(env);
  if (device) ctx.out('This device is disconnected. You can connect another device now.');
  return true;
}

/**
 * Status lines for the device and plan, and today's usage. Makes no changes except
 * refreshing license.json. Network failures are shown as lines, not thrown.
 */
async function reportDevice(ctx, env, { toolName }) {
  const device = readDevice(env);
  if (!device) {
    ctx.out('Device: not connected. Run connect to set it up.');
    return;
  }
  ctx.out(`Device: ${login.cleanName(os.hostname())} (tools: ${device.tools.join(', ') || 'none'})`);
  try {
    const note = await login.fetchPlanNote(baseOf(device, env), device.device_key, device.device_hash);
    if (note.status === 401 || note.status === 403) {
      ctx.out('Plan: sign-in expired. Run connect again.');
    } else {
      applyPlanNote(ctx, env, note, toolName);
    }
  } catch {
    ctx.out('Plan: could not reach ContextShrink right now.');
  }
  try {
    const usage = await login.fetchUsageSummary(baseOf(device, env), device.device_key, { timeoutMs: 3000 });
    const pct = usage && usage.today ? Math.round(Number(usage.today.pct) || 0) : 0;
    ctx.out(`Usage saved today: ${pct}%`);
  } catch {
    ctx.out('Usage saved today: not available right now.');
  }
  const health = await engine.engineHealth(env);
  ctx.out(health ? 'Engine: running.' : 'Engine: not running. It starts at sign-in or with Claude Code.');
}

module.exports = {
  HASH_PREFIX,
  machineIdOf,
  deviceHashOf,
  devicePathsOf,
  readDevice,
  writeDevice,
  removeDeviceFiles,
  readPlanNote,
  applyPlanNote,
  ensureDevice,
  leaveDevice,
  reportDevice,
  daysLeft,
};
