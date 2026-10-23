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
const SHORTCUT_COMMAND = ['code-engine', 'run'];
const HOOK_COMMAND = ['code-engine', 'ensure'];
// The bundle's Python flags (the same as horizon.exe passes to its python.exe).
const PYTHON_FLAGS = ['-I', '-s', '-X', 'utf8'];

// The engine command with no console window. The bundle's python\pythonw.exe runs
// app\horizon_main.py, which is the same CLI as horizon.exe. pythonw.exe is built for
// the GUI subsystem, so it never has a console window; horizon.exe is a console program.
// horizon.exe is used only when the bundle has no pythonw.exe (an older layout).
// Returns { file, args }.
function windowlessInvocation(exe, args) {
  const folder = path.dirname(exe);
  const pythonw = path.join(folder, 'python', 'pythonw.exe');
  const script = path.join(folder, 'app', 'horizon_main.py');
  if (fs.existsSync(pythonw) && fs.existsSync(script)) {
    return { file: pythonw, args: [...PYTHON_FLAGS, script, ...args] };
  }
  return { file: exe, args: [...args] };
}

// One command-line argument: quoted when it contains whitespace.
function quoteArg(value) {
  return /\s/.test(value) ? `"${value}"` : value;
}

// The SessionStart hook command: the windowless invocation as one command line.
function hookCommandOf(exe) {
  const { file, args } = windowlessInvocation(exe, HOOK_COMMAND);
  return [`"${file}"`, ...args.map(quoteArg)].join(' ');
}

// The shortcut's arguments: the windowless invocation's arguments, quoted for a command line.
function shortcutArgumentsOf(exe) {
  return windowlessInvocation(exe, SHORTCUT_COMMAND).args.map(quoteArg).join(' ');
}
const VERSION_SHAPE = /^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$/;
const HASH_SHAPE = /^[0-9a-f]{64}$/;
const DOWNLOAD_IDLE_MS = 30000;
const ENSURE_TIMEOUT_MS = 20000;
// Antivirus scanning can slow extraction of the ~9,400-entry engine zip a lot.
const UNZIP_TIMEOUT_MS = 300000;
// How long a start waits for /__cs/health after `ensure`. The first start of a fresh
// install loads the engine's Python dependencies while antivirus scans the new files,
// which can take minutes; later starts are normally a few seconds.
const START_WAIT_FRESH_MS = 180000;
const START_WAIT_MS = 60000;
const START_POLL_MS = 200;
const STARTING_LINE = 'Starting the ContextShrink engine (the first start can take a minute)...';
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
function runPowerShell(script, vars, timeoutMs = ENSURE_TIMEOUT_MS) {
  const encoded = Buffer.from(script, 'utf16le').toString('base64');
  return new Promise((resolve, reject) => {
    execFile(
      powerShellPath(),
      ['-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-EncodedCommand', encoded],
      { env: { ...process.env, ...vars }, windowsHide: true, timeout: timeoutMs, maxBuffer: 1 << 20 },
      (error, stdout) => {
        if (error) reject(error);
        else resolve(String(stdout || '').trim());
      }
    );
  });
}

// True when execFile killed the process because its timeout ran out.
function isTimeout(error) {
  return Boolean(error) && (error.killed === true || error.code === 'ETIMEDOUT');
}

// The real Windows adapter. Every method rejects on failure.
// `powerShell` replaces runPowerShell in tests only.
function realAdapter({ powerShell = runPowerShell } = {}) {
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
    // .NET, not Expand-Archive: Expand-Archive takes ~93 s for the engine zip on Windows PowerShell 5.1.
    async expandZip(zip, dest) {
      await powerShell(
        "$ErrorActionPreference = 'Stop'\nAdd-Type -AssemblyName System.IO.Compression.FileSystem\n[System.IO.Compression.ZipFile]::ExtractToDirectory($env:CONTEXTSHRINK_ZIP, $env:CONTEXTSHRINK_DEST)",
        { CONTEXTSHRINK_ZIP: zip, CONTEXTSHRINK_DEST: dest },
        UNZIP_TIMEOUT_MS
      );
    },
    async writeShortcut(file, { target, args, workdir }) {
      await powerShell(
        [
          "$ErrorActionPreference = 'Stop'",
          '$shell = New-Object -ComObject WScript.Shell',
          '$link = $shell.CreateShortcut($env:CONTEXTSHRINK_LNK)',
          '$link.TargetPath = $env:CONTEXTSHRINK_TARGET',
          '$link.Arguments = $env:CONTEXTSHRINK_ARGS',
          '$link.WorkingDirectory = $env:CONTEXTSHRINK_WORKDIR',
          // 7 = minimized. The target is windowless (pythonw.exe), so nothing shows at logon.
          '$link.WindowStyle = 7',
          '$link.Save()',
        ].join('\n'),
        { CONTEXTSHRINK_LNK: file, CONTEXTSHRINK_TARGET: target, CONTEXTSHRINK_ARGS: args, CONTEXTSHRINK_WORKDIR: workdir }
      );
    },
    runEngine(exe, args) {
      const invocation = windowlessInvocation(exe, args);
      return new Promise((resolve, reject) => {
        execFile(invocation.file, invocation.args, { windowsHide: true, timeout: ENSURE_TIMEOUT_MS }, (error) =>
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
    if (isTimeout(err)) {
      throw new CsError(
        'The ContextShrink engine took too long to unpack (antivirus scanning can slow this). Nothing was installed. Run this command again.'
      );
    }
    throw new CsError('The ContextShrink engine could not be unpacked. Nothing was installed.');
  } finally {
    fs.rmSync(zip, { force: true });
    fs.rmSync(staging, { recursive: true, force: true });
  }
}

// The JSON object in a health body, or {} when the body is not one.
function healthBodyOf(chunks) {
  try {
    const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    return body !== null && typeof body === 'object' ? body : {};
  } catch {
    return {};
  }
}

/**
 * The engine's /__cs/health answer for any HTTP 200: its body object, or {} when the
 * body is not one. null when nothing answers with 200. `ok` is not read: ok:false
 * means no plan (trial ended), and the engine still passes requests through.
 */
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
      res.on('end', () => resolve(res.statusCode === 200 ? healthBodyOf(chunks) : null));
    });
    req.on('timeout', () => req.destroy());
    req.on('error', () => resolve(null));
  });
}

// The wait after `ensure`. CONTEXTSHRINK_NPX_TEST_START_WAIT_MS shortens it in tests
// that run the CLI as a child process (it is read only in test mode).
function startWaitOf(env, freshInstall) {
  const forced = Number(testOverride(env, 'CONTEXTSHRINK_NPX_TEST_START_WAIT_MS'));
  if (forced > 0) return forced;
  return freshInstall ? START_WAIT_FRESH_MS : START_WAIT_MS;
}

const systemClock = { now: () => Date.now(), sleep };

/** True when the pinned engine is already installed, so installEngine reuses it. */
function engineInstalled(env, manifest) {
  return fs.existsSync(path.join(engineRootOf(env), manifest.version, ENGINE_EXE));
}

/**
 * Runs `horizon.exe code-engine ensure` (it starts the engine detached and returns
 * quickly), then waits until /__cs/health answers with HTTP 200. Throws when it does
 * not answer within the wait (START_WAIT_FRESH_MS after a fresh install, else
 * START_WAIT_MS). An engine that already answers prints nothing.
 * options: out (prints one line), freshInstall (the engine was just installed),
 * clock ({ now, sleep }; tests inject a fake one).
 */
async function startEngine(env, exe, adapter, { out = () => {}, freshInstall = false, clock = systemClock } = {}) {
  try {
    await adapter.runEngine(exe, ['code-engine', 'ensure']);
  } catch {
    // The health check below decides; a failed ensure is reported the same way.
  }
  if (await engineHealth(env)) return;
  out(STARTING_LINE);
  const deadline = clock.now() + startWaitOf(env, freshInstall);
  for (;;) {
    await clock.sleep(START_POLL_MS);
    if (await engineHealth(env)) return;
    if (clock.now() >= deadline) break;
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
  // The target is the windowless invocation, so logon shows nothing.
  const { file: target } = windowlessInvocation(exe, SHORTCUT_COMMAND);
  await adapter.writeShortcut(file, { target, args: shortcutArgumentsOf(exe), workdir: path.dirname(exe) });
}

function removeStartup(env) {
  fs.rmSync(startupPathOf(env), { force: true });
}

function removeEngine(env) {
  fs.rmSync(engineRootOf(env), { recursive: true, force: true });
}

module.exports = {
  ENGINE_EXE,
  windowlessInvocation,
  hookCommandOf,
  UNZIP_TIMEOUT_MS,
  loadManifest,
  engineRootOf,
  startupPathOf,
  resolveAdapter,
  realAdapter,
  installEngine,
  engineInstalled,
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
