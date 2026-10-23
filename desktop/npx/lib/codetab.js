'use strict';

// Claude Desktop's Code tab, set up by `npx contextshrink-claude` (Windows only).
//
// The Code tab ignores settings.json. It is routed by replacing claude.exe in each
// Claude Desktop claude-code folder with a small launcher; the real binary is kept
// beside it as claude.cs-real.exe. The launcher reads codetab.json (written here)
// and starts the real binary with the proxy settings. The patching itself is done
// by the helper, contextshrink-codetab.exe.
//
// Both files are downloaded from the ContextShrink releases, checked against the
// pinned lib/codetab-manifest.json (size and SHA-256), verified with Authenticode,
// and only then moved into place.
//
// codetab.json names the local engine address and holds no secret (the device key
// stays in device.json). The launcher forwards Code tab requests to the engine.
// On Windows the file takes the ACL of the ContextShrink data folder under the profile.

const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const https = require('node:https');
const os = require('node:os');
const path = require('node:path');
const { Transform, pipeline } = require('node:stream');
const { execFile } = require('node:child_process');
const cfg = require('./config');
const files = require('./files');
const { USER_AGENT } = require('./api');

class CodeTabError extends Error {}

const DEFAULT_RELEASES_URL = 'https://app.contextshrink.com/releases';
const TOOLS_PATH = 'claude-code-tools';
const LAUNCHER_NAME = 'claude-code-launcher.exe';
const HELPER_NAME = 'contextshrink-codetab.exe';
const FILE_NAMES = [LAUNCHER_NAME, HELPER_NAME];
const CONFIG_NAME = 'codetab.json';
const OWNER = 'npx';
const BIN_DIR = 'npx-bin';
const STAMP_NAME = 'npx-repair.stamp';
const PLACEHOLDER_VERSION = '0.0.0-unset';
const PLACEHOLDER_HASH = '0'.repeat(64);
const DEFAULT_MANIFEST = path.join(__dirname, 'codetab-manifest.json');
const SIGNER_SUBJECT = /(^|,\s*)CN=Muhammad Burhan Khan Lodhi(,|$)/;
const SIGNER_ISSUER = 'Microsoft ID Verified';
const VERSION_SHAPE = /^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$/;
const HASH_SHAPE = /^[0-9a-f]{64}$/;
const LOOPBACK = new Set(['127.0.0.1']);
const DOWNLOAD_IDLE_MS = 30000;
const HELPER_TIMEOUT_MS = 180000;
const CHECK_TIMEOUT_MS = 60000;
const UNREACHABLE = 'cannot download the Code tab files. Check your internet connection. Nothing was installed.';

// Test-only overrides. They are ignored unless CONTEXTSHRINK_NPX_TEST=1.
function testOverride(env, name) {
  if (env.CONTEXTSHRINK_NPX_TEST !== '1') return '';
  return (env[name] || '').trim();
}

function platformOf(env) {
  return testOverride(env, 'CONTEXTSHRINK_NPX_TEST_PLATFORM') || process.platform;
}

function isWindows(env) {
  return platformOf(env) === 'win32';
}

function allowedUrl(url) {
  if (url.protocol === 'https:') return true;
  return url.protocol === 'http:' && LOOPBACK.has(url.hostname);
}

function releasesBase(env) {
  const base = ((env.CONTEXTSHRINK_RELEASES_URL || '').trim() || DEFAULT_RELEASES_URL).replace(/\/+$/, '');
  let url;
  try {
    url = new URL(base);
  } catch {
    throw new CodeTabError('CONTEXTSHRINK_RELEASES_URL is not a valid URL.');
  }
  if (!allowedUrl(url)) throw new CodeTabError('CONTEXTSHRINK_RELEASES_URL must be an https URL.');
  return base;
}

// The signature check is skipped only in tests, and only against a loopback releases server.
function signatureCheckSkipped(env) {
  if (testOverride(env, 'CONTEXTSHRINK_NPX_TEST_SKIP_SIGNATURE') !== '1') return false;
  try {
    const url = new URL(releasesBase(env));
    return url.protocol === 'http:' && LOOPBACK.has(url.hostname);
  } catch {
    return false;
  }
}

function codeTabPaths(env) {
  const localAppData = (env.LOCALAPPDATA || '').trim() || path.join(os.homedir(), 'AppData', 'Local');
  const dataDir = (env.CONTEXTSHRINK_DATA_DIR || '').trim() || path.join(localAppData, 'ContextShrink');
  const dir = path.join(dataDir, 'claude-code');
  const bin = path.join(dir, BIN_DIR);
  return {
    dir,
    config: path.join(dir, CONFIG_NAME),
    stamp: path.join(dir, STAMP_NAME),
    bin,
    launcher: path.join(bin, LAUNCHER_NAME),
    helper: path.join(bin, HELPER_NAME),
  };
}

function isDirectory(file) {
  try {
    return fs.statSync(file).isDirectory();
  } catch {
    return false;
  }
}

// Claude Desktop's claude-code folders, the same places the desktop app looks.
function desktopRoots(env) {
  const candidates = [];
  const localAppData = (env.LOCALAPPDATA || '').trim();
  if (localAppData) {
    const packages = path.join(localAppData, 'Packages');
    let names = [];
    try {
      names = fs.readdirSync(packages);
    } catch {
      names = [];
    }
    for (const name of names.sort()) {
      if (name.startsWith('Claude_')) {
        candidates.push(path.join(packages, name, 'LocalCache', 'Roaming', 'Claude', 'claude-code'));
      }
    }
  }
  const appData = (env.APPDATA || '').trim();
  if (appData) candidates.push(path.join(appData, 'Claude', 'claude-code'));
  return candidates.filter(isDirectory);
}

// null when there is no codetab.json. Throws when the file is there but not usable.
function readConfig(file) {
  if (!fs.existsSync(file)) return null;
  let data;
  try {
    data = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    throw new CodeTabError(`${file} is not valid JSON, so the Code tab was not touched. Remove it and try again.`);
  }
  if (!files.isPlainObject(data)) {
    throw new CodeTabError(`${file} is not in the expected format, so the Code tab was not touched.`);
  }
  return data;
}

function ownedByUs(config) {
  return config.owner === OWNER;
}

// Returns { placeholder: true } until the first release fills the manifest in.
function loadManifest(env) {
  const file = testOverride(env, 'CONTEXTSHRINK_NPX_CODETAB_MANIFEST') || DEFAULT_MANIFEST;
  let data;
  try {
    data = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    throw new CodeTabError('the release manifest could not be read.');
  }
  if (!files.isPlainObject(data)) throw new CodeTabError('the release manifest is not valid.');
  const entries = files.isPlainObject(data.files) ? data.files : {};
  const placeholder =
    data.version === PLACEHOLDER_VERSION ||
    FILE_NAMES.some((name) => files.isPlainObject(entries[name]) && entries[name].sha256 === PLACEHOLDER_HASH);
  if (placeholder) return { placeholder: true };
  if (typeof data.version !== 'string' || !VERSION_SHAPE.test(data.version)) {
    throw new CodeTabError('the release manifest is not valid.');
  }
  const pinned = {};
  for (const name of FILE_NAMES) {
    const entry = entries[name];
    const ok =
      files.isPlainObject(entry) &&
      typeof entry.sha256 === 'string' &&
      HASH_SHAPE.test(entry.sha256) &&
      Number.isSafeInteger(entry.size) &&
      entry.size > 0;
    if (!ok) throw new CodeTabError('the release manifest is not valid.');
    pinned[name] = { sha256: entry.sha256, size: entry.size };
  }
  return { placeholder: false, version: data.version, files: pinned };
}

function hashOf(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function alreadyInstalled(file, entry) {
  try {
    return fs.statSync(file).size === entry.size && hashOf(file) === entry.sha256;
  } catch {
    return false;
  }
}

// Streams a release file to dest, checking its size and SHA-256 as it arrives.
function downloadTo(url, dest, entry, name, redirectsLeft = 3) {
  return new Promise((resolve, reject) => {
    const fail = (message) => reject(new CodeTabError(message));
    let target;
    try {
      target = new URL(url);
    } catch {
      fail(`the download address for ${name} is not valid.`);
      return;
    }
    const lib = target.protocol === 'https:' ? https : http;
    const req = lib.get(target, { headers: { 'user-agent': USER_AGENT } }, (res) => {
      const status = res.statusCode || 0;
      if (status >= 300 && status < 400 && res.headers.location) {
        res.resume();
        let next = null;
        try {
          next = new URL(res.headers.location, target);
        } catch {
          next = null;
        }
        if (!next || !allowedUrl(next) || redirectsLeft <= 0) {
          fail(`the download of ${name} was redirected to an address that is not allowed. Nothing was installed.`);
          return;
        }
        downloadTo(next.href, dest, entry, name, redirectsLeft - 1).then(resolve, reject);
        return;
      }
      if (status !== 200) {
        res.resume();
        fail(`could not download ${name} (HTTP ${status}). Nothing was installed.`);
        return;
      }
      const hash = crypto.createHash('sha256');
      let received = 0;
      const meter = new Transform({
        transform(chunk, _encoding, callback) {
          received += chunk.length;
          if (received > entry.size) {
            callback(new CodeTabError(`${name} is larger than the release manifest says. Nothing was installed.`));
            return;
          }
          hash.update(chunk);
          callback(null, chunk);
        },
      });
      pipeline(res, meter, fs.createWriteStream(dest, { flags: 'wx', mode: 0o600 }), (error) => {
        if (error) {
          fail(error instanceof CodeTabError ? error.message : UNREACHABLE);
          return;
        }
        if (received !== entry.size || hash.digest('hex') !== entry.sha256) {
          fail(`${name} did not match the release manifest (size or SHA-256). Nothing was installed.`);
          return;
        }
        resolve();
      });
    });
    req.setTimeout(DOWNLOAD_IDLE_MS, () => req.destroy(new Error('timeout')));
    req.on('error', () => fail(UNREACHABLE));
  });
}

// The publisher check. Status must be Valid, the signer must be the ContextShrink
// publisher and the certificate must come from Microsoft's ID Verified authority.
function signatureProblem(info) {
  if (!files.isPlainObject(info) || info.Status !== 'Valid') {
    const status = files.isPlainObject(info) && info.Status ? info.Status : 'unknown';
    return `signature status is ${status}`;
  }
  if (!SIGNER_SUBJECT.test(String(info.Subject || ''))) return 'the signer is not the expected publisher';
  if (!String(info.Issuer || '').includes(SIGNER_ISSUER)) return 'the certificate is not from the expected authority';
  return null;
}

// The file path goes in an environment variable, never in the script text.
const SIGNATURE_SCRIPT = [
  "$ErrorActionPreference = 'Stop'",
  '$sig = Get-AuthenticodeSignature -LiteralPath $env:CONTEXTSHRINK_CHECK_PATH',
  "$subject = ''",
  "$issuer = ''",
  '$cert = $sig.SignerCertificate',
  'if ($null -ne $cert) { $subject = $cert.Subject; $issuer = $cert.Issuer }',
  '[pscustomobject]@{ Status = [string]$sig.Status; Subject = $subject; Issuer = $issuer } | ConvertTo-Json -Compress',
].join('\n');

function powerShellPath() {
  const root = process.env.SystemRoot || process.env.windir || 'C:\\Windows';
  return path.join(root, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
}

function readSignature(file) {
  const encoded = Buffer.from(SIGNATURE_SCRIPT, 'utf16le').toString('base64');
  return new Promise((resolve, reject) => {
    execFile(
      powerShellPath(),
      ['-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
      {
        env: { ...process.env, CONTEXTSHRINK_CHECK_PATH: file },
        windowsHide: true,
        timeout: CHECK_TIMEOUT_MS,
        maxBuffer: 1 << 20,
      },
      (error, stdout) => {
        const line = String(stdout || '').trim().split(/\r?\n/).pop() || '';
        let info = null;
        try {
          info = JSON.parse(line);
        } catch {
          info = null;
        }
        if (error || !files.isPlainObject(info)) {
          reject(new CodeTabError('could not check the signature of a Code tab file. Nothing was installed.'));
        } else {
          resolve(info);
        }
      }
    );
  });
}

async function checkSignature(file, name) {
  const problem = signatureProblem(await readSignature(file));
  if (problem) {
    throw new CodeTabError(`${name} failed the publisher check (${problem}). Nothing was installed.`);
  }
}

function renameInto(temp, target, name) {
  try {
    fs.renameSync(temp, target);
  } catch (err) {
    if (err.code === 'EPERM' || err.code === 'EBUSY' || err.code === 'EACCES') {
      throw new CodeTabError(
        `${name} is in use (Claude Desktop or a Code tab session may be running). Close it and run connect again.`
      );
    }
    throw new CodeTabError(`could not put ${name} in place (${err.code || 'write error'}).`);
  }
}

// Puts one release file in npx-bin. A copy already there with the pinned hash is reused.
async function fetchFile(env, paths, base, manifest, name) {
  const target = path.join(paths.bin, name);
  const entry = manifest.files[name];
  if (alreadyInstalled(target, entry)) return;
  const url = `${base}/${TOOLS_PATH}/${manifest.version}/${name}`;
  const temp = path.join(paths.bin, `.${name}.${process.pid}.${crypto.randomBytes(4).toString('hex')}.download`);
  try {
    await downloadTo(url, temp, entry, name);
    if (!signatureCheckSkipped(env)) await checkSignature(temp, name);
    renameInto(temp, target, name);
  } finally {
    fs.rmSync(temp, { force: true });
  }
}

// Where the helper runs. The test override may be a node script.
function helperCommand(env, paths) {
  const override = testOverride(env, 'CONTEXTSHRINK_NPX_CODETAB_HELPER');
  if (override) {
    return override.endsWith('.js') ? { file: process.execPath, prefix: [override] } : { file: override, prefix: [] };
  }
  return { file: paths.helper, prefix: [] };
}

function helperPresent(env, paths) {
  return fs.existsSync(testOverride(env, 'CONTEXTSHRINK_NPX_CODETAB_HELPER') || paths.helper);
}

function runHelper(env, paths, args) {
  const cmd = helperCommand(env, paths);
  return new Promise((resolve) => {
    execFile(
      cmd.file,
      [...cmd.prefix, ...args],
      { env, windowsHide: true, timeout: HELPER_TIMEOUT_MS, maxBuffer: 4 << 20 },
      (error, stdout, stderr) => {
        const code = error ? (typeof error.code === 'number' ? error.code : 1) : 0;
        resolve({ code, stdout: String(stdout || ''), stderr: String(stderr || '') });
      }
    );
  });
}

// Keeps the account key out of any text this module prints.
function redact(text) {
  return String(text).replace(/cs_live_[A-Za-z0-9_-]+/g, '[key]');
}

function helperError(what, result) {
  const line = result.stderr.trim().split(/\r?\n/).pop() || '';
  const detail = redact(line).slice(0, 200);
  return new CodeTabError(`the helper could not ${what} (exit code ${result.code}).${detail ? ` ${detail}` : ''}`);
}

function helperJson(result, what) {
  let data = null;
  try {
    data = JSON.parse(result.stdout.trim());
  } catch {
    data = null;
  }
  if (!files.isPlainObject(data)) {
    throw new CodeTabError(`the helper gave an unexpected answer to ${what}. Nothing else was changed.`);
  }
  return data;
}

function folderStates(data) {
  const folders = Array.isArray(data.folders) ? data.folders : [];
  return folders.map((folder) => (folder && typeof folder.state === 'string' ? folder.state : 'unknown'));
}

function countOf(states, state) {
  return states.filter((s) => s === state).length;
}

// The Code tab uses the same engine address as the terminal. Tool search follows the
// terminal rule: on unless the user set it to another value.
function gatewayFromSettings(terminal) {
  const settings = files.readSettings(terminal.settings);
  const block = files.isPlainObject(settings.payload.env) ? settings.payload.env : {};
  if (block[cfg.BASE_URL_KEY] !== terminal.engineUrl) {
    throw new CodeTabError('ANTHROPIC_BASE_URL in settings.json is not the ContextShrink engine address, so it was not set up.');
  }
  const toolSearchSetByUser =
    files.hasOwn(block, cfg.TOOL_SEARCH_KEY) && block[cfg.TOOL_SEARCH_KEY] !== cfg.TOOL_SEARCH_VALUE;
  return {
    base: terminal.engineUrl,
    env: toolSearchSetByUser ? {} : { [cfg.TOOL_SEARCH_KEY]: cfg.TOOL_SEARCH_VALUE },
  };
}

// The forwarder format: no launch mode and no key. The launcher forwards to the engine.
function configFor(gateway) {
  return {
    owner: OWNER,
    enabled: true,
    base_url: gateway.base,
    health_port: Number(new URL(gateway.base).port),
    env: gateway.env,
  };
}

function restoreConfig(file, previous) {
  try {
    if (previous) files.writePrivateJson(file, previous);
    else files.removeFile(file);
  } catch {
    // Best effort: the original error is the one the user needs to see.
  }
}

async function patchFolders(env, paths) {
  const install = await runHelper(env, paths, ['install', '--json', '--launcher', paths.launcher]);
  if (install.code !== 0) throw helperError('patch Claude Desktop', install);
  const states = folderStates(helperJson(install, 'install'));
  return { patched: countOf(states, 'patched'), locked: countOf(states, 'locked') };
}

function reportSetUp(ctx, { patched, locked }) {
  if (patched > 0) {
    ctx.out(`Code tab: patched ${patched} Claude Code version(s); it stays patched after Claude Desktop updates.`);
    ctx.out('Open a new session in the Code tab to use it.');
  } else {
    ctx.out('Code tab: set up. No Claude Code version was found yet; the startup shortcut patches new ones when they appear.');
  }
  if (locked > 0) {
    ctx.out(
      `Code tab: ${locked} Claude Code folder(s) are in use by a running Claude Desktop session, ` +
        'so they will be patched at the next sign-in or the next session.'
    );
  }
}

function failureLine(err) {
  if (err instanceof CodeTabError || err instanceof files.FileError) return `Code tab: ${redact(err.message)}`;
  return `Code tab: unexpected error (${redact(err && err.message ? err.message : 'unknown')}).`;
}

/**
 * Sets up the Code tab after connect. Returns 0 when it is set up, or skipped for a
 * reason that is not an error, and 1 when a step failed. Never throws.
 */
async function setUpCodeTab(ctx, terminal, { skip = false } = {}) {
  const env = ctx.env;
  try {
    if (!isWindows(env)) {
      ctx.out('Code tab is only set up on Windows; skipped.');
      return 0;
    }
    const paths = codeTabPaths(env);
    const existing = readConfig(paths.config);
    if (existing && !ownedByUs(existing)) {
      ctx.out('Code tab left alone: the ContextShrink desktop app manages it.');
      return 0;
    }
    if (skip) {
      if (existing) {
        // Brings an older codetab.json (before the forwarder format) up to date.
        files.writePrivateJson(paths.config, configFor(gatewayFromSettings(terminal)));
        ctx.out('Code tab left as it is (--no-code-tab).');
      } else {
        ctx.out('Code tab skipped (--no-code-tab).');
      }
      return 0;
    }
    if (desktopRoots(env).length === 0) {
      ctx.out('Claude Desktop not found; Code tab not set up.');
      return 0;
    }
    const manifest = loadManifest(env);
    if (manifest.placeholder) {
      ctx.out('Code tab support is not released yet; Code tab not set up.');
      return 0;
    }
    const gateway = gatewayFromSettings(terminal);
    const base = releasesBase(env);
    fs.mkdirSync(paths.bin, { recursive: true, mode: 0o700 });
    for (const name of FILE_NAMES) {
      await fetchFile(env, paths, base, manifest, name);
    }
    files.writePrivateJson(paths.config, configFor(gateway));
    let outcome;
    try {
      outcome = await patchFolders(env, paths);
    } catch (err) {
      // The patch did not finish, so the previous config goes back in place.
      restoreConfig(paths.config, existing);
      throw err;
    }
    // The patch is in place now, so the config stays even if the shortcut fails.
    const autostart = await runHelper(env, paths, ['autostart', 'on']);
    if (autostart.code !== 0) {
      throw new CodeTabError(
        `Claude Desktop is patched, but the startup shortcut could not be added (exit code ${autostart.code}). ` +
          'Updates will not be patched until you run connect again.'
      );
    }
    reportSetUp(ctx, outcome);
    return 0;
  } catch (err) {
    ctx.err(failureLine(err));
    return 1;
  }
}

/**
 * Restores the Code tab during disconnect or logout. Only a codetab.json owned by npx
 * is removed; a desktop-app config is left alone. Returns 0, or 1 when something is
 * still patched or a step failed (the files that are still needed are kept).
 */
async function undoCodeTab(ctx) {
  const env = ctx.env;
  if (!isWindows(env)) return 0;
  try {
    const paths = codeTabPaths(env);
    const config = readConfig(paths.config);
    if (config && !ownedByUs(config)) {
      ctx.out('Code tab left alone: the ContextShrink desktop app manages it.');
      return 0;
    }
    const haveBin = fs.existsSync(paths.bin);
    if (!config && !haveBin) return 0;
    if (!helperPresent(env, paths)) {
      if (haveBin) {
        throw new CodeTabError(
          'the helper is missing, so Claude Desktop cannot be restored from here. Run connect again, then disconnect.'
        );
      }
      files.removeFile(paths.config);
      return 0;
    }

    const remove = await runHelper(env, paths, ['remove', '--json']);
    if (remove.code !== 0) throw helperError('restore Claude Desktop', remove);
    const states = folderStates(helperJson(remove, 'remove'));
    const stillPatched = countOf(states, 'patched') + countOf(states, 'locked');
    if (stillPatched > 0) {
      throw new CodeTabError(
        `${stillPatched} Claude Code folder(s) are still patched, probably because Claude Desktop is running. ` +
          'Close it and run disconnect again. The Code tab files were kept.'
      );
    }
    const restored = countOf(states, 'real');

    const autostart = await runHelper(env, paths, ['autostart', 'off']);
    if (autostart.code !== 0) throw helperError('remove the startup shortcut', autostart);

    files.removeFile(paths.config);
    files.removeFile(paths.stamp);
    fs.rmSync(paths.bin, { recursive: true, force: true });
    ctx.out(`Code tab: restored ${restored} Claude Code folder(s), removed the startup shortcut and the Code tab files.`);
    return 0;
  } catch (err) {
    ctx.err(failureLine(err));
    return 1;
  }
}

/** One status line for the Code tab. Never prints the key. */
async function codeTabStatus(ctx) {
  const env = ctx.env;
  if (!isWindows(env)) {
    ctx.out('Code tab: Windows only.');
    return 0;
  }
  try {
    const paths = codeTabPaths(env);
    const config = readConfig(paths.config);
    if (config && !ownedByUs(config)) {
      ctx.out('Code tab: managed by the ContextShrink desktop app.');
      return 0;
    }
    if (!config) {
      ctx.out(
        desktopRoots(env).length > 0
          ? 'Code tab: not set up. Run npx contextshrink-claude to set it up.'
          : 'Code tab: Claude Desktop not found.'
      );
      return 0;
    }
    if (!helperPresent(env, paths)) {
      ctx.out('Code tab: set up, but its helper is missing. Run connect again.');
      return 0;
    }
    const result = await runHelper(env, paths, ['status', '--json']);
    if (result.code !== 0) {
      ctx.out('Code tab: could not read its status from the helper.');
      return 0;
    }
    const data = helperJson(result, 'status');
    const states = folderStates(data);
    const locked = countOf(states, 'locked');
    const lockedText = locked > 0 ? `, ${locked} waiting for the next session` : '';
    const autostart = data.autostart === true ? 'on' : 'off';
    ctx.out(
      `Code tab: set up. ${countOf(states, 'patched')} Claude Code version(s) patched${lockedText}; ` +
        `startup re-patch ${autostart}.`
    );
    return 0;
  } catch (err) {
    ctx.out(failureLine(err));
    return 1;
  }
}

module.exports = {
  CodeTabError,
  codeTabPaths,
  setUpCodeTab,
  undoCodeTab,
  codeTabStatus,
  signatureProblem,
};
