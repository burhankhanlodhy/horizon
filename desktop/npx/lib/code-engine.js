'use strict';

// The local engine: the pinned release in lib/engine-manifest.json, its download
// (size and SHA-256 checked as it arrives), the Authenticode check, the start and
// stop calls, and the Windows Startup shortcut.
//
// The engine is installed to %LOCALAPPDATA%\ContextShrink\engine\<version>\.
// Windows work (browser, signature, unzip, shortcut) goes through an adapter.
// The real adapter runs PowerShell; tests inject a fake one.

const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const https = require('node:https');
const path = require('node:path');
const { Transform, pipeline } = require('node:stream');
const { execFile } = require('node:child_process');
const { CsError, testOverride, localAppData, appData, engineUrlOf } = require('./code-env');
const { USER_AGENT } = require('./api');
// The publisher pin (signer subject and certificate authority) is the launcher's,
// so the engine and the Code tab trust the same publisher.
const publisher = require('./code-publisher');

const DEFAULT_MANIFEST = path.join(__dirname, 'engine-manifest.json');
const ENGINE_EXE = 'horizon.exe';
const SHORTCUT_NAME = 'ContextShrink Engine.lnk';
const SHORTCUT_ARGS = 'code-engine run';
const VERSION_SHAPE = /^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$/;
const HASH_SHAPE = /^[0-9a-f]{64}$/;
const DOWNLOAD_IDLE_MS = 30000;
const ENSURE_TIMEOUT_MS = 20000;
const START_WAIT_MS = 5000;
const HEALTH_TIMEOUT_MS = 300;
const UNPUBLISHED = 'Engine not published yet. Nothing was changed.';

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// { published: false } while lib/engine-manifest.json has no url. Otherwise the pinned release.
function loadManifest(env) {
  const file = testOverride(env, 'CONTEXTSHRINK_NPX_ENGINE_MANIFEST') || DEFAULT_MANIFEST;
  let data;
  try {
    data = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    throw new CsError('The engine manifest could not be read. Nothing was changed.');
  }
  if (!isPlainObject(data)) throw new CsError('The engine manifest is not valid. Nothing was changed.');
  if (data.url === '' || data.url === undefined) return { published: false };
  let url;
  try {
    url = new URL(data.url);
  } catch {
    url = null;
  }
  const allowed = url && (url.protocol === 'https:' || (url.protocol === 'http:' && url.hostname === '127.0.0.1'));
  const ok =
    typeof data.version === 'string' &&
    VERSION_SHAPE.test(data.version) &&
    allowed &&
    typeof data.sha256 === 'string' &&
    HASH_SHAPE.test(data.sha256) &&
    Number.isSafeInteger(data.size) &&
    data.size > 0;
  if (!ok) throw new CsError('The engine manifest is not valid. Nothing was changed.');
  return { published: true, version: data.version, url: data.url, sha256: data.sha256, size: data.size };
}

function engineRootOf(env) {
  return path.join(localAppData(env), 'ContextShrink', 'engine');
}

function startupPathOf(env) {
  return path.join(appData(env), 'Microsoft', 'Windows', 'Start Menu', 'Programs', 'Startup', SHORTCUT_NAME);
}

function powerShellPath() {
  const root = process.env.SystemRoot || process.env.windir || 'C:\\Windows';
  return path.join(root, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
}

// Paths go in environment variables, never in the script text.
function runPowerShell(script, vars) {
  const encoded = Buffer.from(script, 'utf16le').toString('base64');
  return new Promise((resolve, reject) => {
    execFile(
      powerShellPath(),
      ['-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
      { env: { ...process.env, ...vars }, windowsHide: true, timeout: ENSURE_TIMEOUT_MS, maxBuffer: 1 << 20 },
      (error, stdout) => {
        if (error) reject(error);
        else resolve(String(stdout || '').trim());
      }
    );
  });
}

// The real Windows adapter. Every method rejects on failure.
function realAdapter() {
  return {
    openBrowser(url) {
      return new Promise((resolve, reject) => {
        // `start ""` takes the first quoted argument as the window title, so the URL is not mistaken for one.
        execFile(
          process.env.ComSpec || 'cmd.exe',
          ['/c', 'start', '""', url],
          { windowsHide: true, windowsVerbatimArguments: true, timeout: 15000 },
          (error) => (error ? reject(error) : resolve())
        );
      });
    },
    // { Status, Subject, Issuer } of the Authenticode signature, read the same way as the Code tab's.
    async signatureStatus(file) {
      try {
        return await publisher.readSignature(file);
      } catch {
        throw new CsError('The ContextShrink engine signature could not be checked. Nothing was installed.');
      }
    },
    async expandZip(zip, dest) {
      await runPowerShell(
        "$ErrorActionPreference = 'Stop'\nExpand-Archive -LiteralPath $env:CONTEXTSHRINK_ZIP -DestinationPath $env:CONTEXTSHRINK_DEST -Force",
        { CONTEXTSHRINK_ZIP: zip, CONTEXTSHRINK_DEST: dest }
      );
    },
    async writeShortcut(file, { target, args, workdir }) {
      await runPowerShell(
        [
          "$ErrorActionPreference = 'Stop'",
          '$shell = New-Object -ComObject WScript.Shell',
          '$link = $shell.CreateShortcut($env:CONTEXTSHRINK_LNK)',
          '$link.TargetPath = $env:CONTEXTSHRINK_TARGET',
          '$link.Arguments = $env:CONTEXTSHRINK_ARGS',
          '$link.WorkingDirectory = $env:CONTEXTSHRINK_WORKDIR',
          '$link.WindowStyle = 0',
          '$link.Save()',
        ].join('\n'),
        { CONTEXTSHRINK_LNK: file, CONTEXTSHRINK_TARGET: target, CONTEXTSHRINK_ARGS: args, CONTEXTSHRINK_WORKDIR: workdir }
      );
    },
    runEngine(exe, args) {
      return new Promise((resolve, reject) => {
        execFile(exe, args, { windowsHide: true, timeout: ENSURE_TIMEOUT_MS }, (error) =>
          error ? reject(error) : resolve()
        );
      });
    },
  };
}

// Tests may replace the Windows work with a fake adapter module (CONTEXTSHRINK_NPX_TEST_ADAPTER).
function resolveAdapter(env, overrides = {}) {
  const file = testOverride(env, 'CONTEXTSHRINK_NPX_TEST_ADAPTER');
  const fake = file ? require(file)(env) : {};
  return { ...realAdapter(), ...fake, ...overrides };
}

function allowedDownload(url) {
  return url.protocol === 'https:' || (url.protocol === 'http:' && url.hostname === '127.0.0.1');
}

// Streams the zip to dest, checking its size and SHA-256 as it arrives.
function downloadTo(url, dest, expected, redirectsLeft = 3) {
  return new Promise((resolve, reject) => {
    const fail = (message) => reject(new CsError(message));
    let target;
    try {
      target = new URL(url);
    } catch {
      fail('The engine download address is not valid. Nothing was installed.');
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
        if (!next || !allowedDownload(next) || redirectsLeft <= 0) {
          fail('The engine download was redirected to an address that is not allowed. Nothing was installed.');
          return;
        }
        downloadTo(next.href, dest, expected, redirectsLeft - 1).then(resolve, reject);
        return;
      }
      if (status !== 200) {
        res.resume();
        fail(`Could not download the ContextShrink engine (HTTP ${status}). Nothing was installed.`);
        return;
      }
      const hash = crypto.createHash('sha256');
      let received = 0;
      const meter = new Transform({
        transform(chunk, _encoding, callback) {
          received += chunk.length;
          if (received > expected.size) {
            callback(new CsError('The ContextShrink engine is larger than the manifest says. Nothing was installed.'));
            return;
          }
          hash.update(chunk);
          callback(null, chunk);
        },
      });
      pipeline(res, meter, fs.createWriteStream(dest, { flags: 'wx', mode: 0o600 }), (error) => {
        if (error) {
          fail(error instanceof CsError ? error.message : 'The engine download was interrupted. Nothing was installed.');
          return;
        }
        if (received !== expected.size || hash.digest('hex') !== expected.sha256) {
          fail('The ContextShrink engine did not match the release manifest (size or SHA-256). Nothing was installed.');
          return;
        }
        resolve();
      });
    });
    req.setTimeout(DOWNLOAD_IDLE_MS, () => req.destroy(new Error('timeout')));
    req.on('error', () => fail('Cannot download the ContextShrink engine. Check your internet connection. Nothing was installed.'));
  });
}

/**
 * Makes sure the pinned engine is installed and returns the path of horizon.exe.
 * A version that is already installed is reused without a download.
 */
async function installEngine(ctx, env, manifest, adapter) {
  const root = engineRootOf(env);
  const finalDir = path.join(root, manifest.version);
  const exe = path.join(finalDir, ENGINE_EXE);
  if (fs.existsSync(exe)) return exe;

  fs.mkdirSync(root, { recursive: true, mode: 0o700 });
  const nonce = crypto.randomBytes(4).toString('hex');
  const zip = path.join(root, `.download-${process.pid}-${nonce}.zip`);
  const staging = path.join(root, `.staging-${process.pid}-${nonce}`);
  try {
    ctx.out(`Downloading the ContextShrink engine ${manifest.version}...`);
    await downloadTo(manifest.url, zip, manifest);
    await adapter.expandZip(zip, staging);
    const staged = path.join(staging, ENGINE_EXE);
    if (!fs.existsSync(staged)) throw new CsError('The ContextShrink engine archive has no engine in it. Nothing was installed.');
    const check = await adapter.signatureStatus(staged);
    if (!check || check.Status !== 'Valid') {
      throw new CsError(
        `The ContextShrink engine failed the signature check (status: ${check && check.Status ? check.Status : 'unknown'}). Nothing was installed.`
      );
    }
    // Valid is not enough: the signer must be the ContextShrink publisher.
    const problem = publisher.signatureProblem(check);
    if (problem) {
      throw new CsError(`The ContextShrink engine failed the publisher check (${problem}). Nothing was installed.`);
    }
    fs.rmSync(finalDir, { recursive: true, force: true });
    fs.renameSync(staging, finalDir);
    ctx.out(`Engine ${manifest.version} installed.`);
    return exe;
  } catch (err) {
    if (err instanceof CsError) throw err;
    throw new CsError('The ContextShrink engine could not be unpacked. Nothing was installed.');
  } finally {
    fs.rmSync(zip, { force: true });
    fs.rmSync(staging, { recursive: true, force: true });
  }
}

/** The engine's /__cs/health answer, or null when nothing answers. */
function engineHealth(env, timeoutMs = HEALTH_TIMEOUT_MS) {
  return new Promise((resolve) => {
    let url;
    try {
      url = new URL('/__cs/health', engineUrlOf(env));
    } catch {
      resolve(null);
      return;
    }
    const req = http.get(url, { timeout: timeoutMs }, (res) => {
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('error', () => resolve(null));
      res.on('end', () => {
        if (res.statusCode !== 200) {
          resolve(null);
          return;
        }
        try {
          resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));
        } catch {
          resolve(null);
        }
      });
    });
    req.on('timeout', () => req.destroy());
    req.on('error', () => resolve(null));
  });
}

/**
 * Runs `horizon.exe code-engine ensure` (it starts the engine detached and returns
 * quickly), then waits for the health check. Throws when the engine does not answer.
 */
async function startEngine(env, exe, adapter) {
  try {
    await adapter.runEngine(exe, ['code-engine', 'ensure']);
  } catch {
    // The health check below decides; a failed ensure is reported the same way.
  }
  const deadline = Date.now() + START_WAIT_MS;
  for (;;) {
    if (await engineHealth(env)) return;
    if (Date.now() >= deadline) break;
    await sleep(200);
  }
  throw new CsError('The ContextShrink engine did not start. Nothing was changed. Run this command again.');
}

// Every installed engine's horizon.exe. Used to stop the engine on uninstall.
function installedExes(env) {
  const root = engineRootOf(env);
  let names = [];
  try {
    names = fs.readdirSync(root);
  } catch {
    return [];
  }
  return names
    .filter((name) => !name.startsWith('.'))
    .map((name) => path.join(root, name, ENGINE_EXE))
    .filter((file) => fs.existsSync(file));
}

async function stopEngine(env, adapter) {
  for (const exe of installedExes(env)) {
    try {
      await adapter.runEngine(exe, ['code-engine', 'stop']);
    } catch {
      // Best effort: the engine may already be stopped.
    }
  }
}

async function installStartup(env, exe, adapter) {
  const file = startupPathOf(env);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  await adapter.writeShortcut(file, { target: exe, args: SHORTCUT_ARGS, workdir: path.dirname(exe) });
}

function removeStartup(env) {
  fs.rmSync(startupPathOf(env), { force: true });
}

function removeEngine(env) {
  fs.rmSync(engineRootOf(env), { recursive: true, force: true });
}

module.exports = {
  ENGINE_EXE,
  loadManifest,
  engineRootOf,
  startupPathOf,
  resolveAdapter,
  realAdapter,
  installEngine,
  engineHealth,
  startEngine,
  stopEngine,
  installStartup,
  removeStartup,
  removeEngine,
  installedExes,
  downloadTo,
  UNPUBLISHED,
};
