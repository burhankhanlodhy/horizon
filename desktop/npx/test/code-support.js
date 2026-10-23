'use strict';

// Test support shared by both npx packages (identical copies in each). A fake
// ContextShrink API serves device sign-in, the plan note and usage. A fake engine
// answers the health check. Every path and URL a test touches lives under a temp
// folder, and the Windows work goes through test/fixtures/fake-windows-adapter.js,
// so no test runs PowerShell, opens a browser or reads the real profile.

const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { execFile } = require('node:child_process');

const ADAPTER = path.join(__dirname, 'fixtures', 'fake-windows-adapter.js');
const MACHINE_ID = '  TEST-MACHINE-1  ';
const SITE = 'https://code.contextshrink.com';
const ENGINE_VERSION = '9.9.9-test';
const ENGINE_ZIP = Buffer.from('fake engine archive for tests\n');

function sha256(bytes) {
  return crypto.createHash('sha256').update(bytes).digest('hex');
}

function b64url(text) {
  return Buffer.from(text, 'utf8').toString('base64url');
}

function engineManifestFor(apiUrl) {
  return {
    version: ENGINE_VERSION,
    url: `${apiUrl}/engine.zip`,
    sha256: sha256(ENGINE_ZIP),
    size: ENGINE_ZIP.length,
  };
}

/**
 * A fake ContextShrink API for /code/*. `api.state` is changed by tests:
 *   plan: 'trial' | 'active' | 'ended' (ended answers 402)
 *   pollsBeforeApproval: how many polls answer "pending" first
 *   deviceLimit: when an array, poll answers status "device_limit" with these devices
 *   expireAfterLimitPolls: with deviceLimit, answer "expired" after this many limit answers
 *   entitlementStatus: a status to answer instead of the plan note (e.g. 503)
 *   pollStatus: a status ('expired', 'denied') to answer every poll with
 *   verifyUrl: a sign-in link to send instead of the normal one
 *   summary: the usage numbers
 */
function startCodeApi() {
  const api = {
    url: '',
    calls: [],
    issued: [],
    devices: new Map(),
    requests: new Map(),
    state: {
      plan: 'trial',
      trialDays: 7,
      pollsBeforeApproval: 1,
      deviceLimit: null,
      entitlementStatus: null,
      summary: { requests: 12, tokens_before: 1000, tokens_after: 780, tokens_saved: 220, pct: 22 },
    },
  };
  let seq = 0;

  function deviceOf(req) {
    const auth = req.headers.authorization || '';
    const key = auth.startsWith('Bearer ') ? auth.slice('Bearer '.length) : '';
    return api.devices.get(key) || null;
  }

  function route(req, body) {
    const { method, url } = req;
    if (method === 'POST' && url === '/code/cli/start') {
      if (!['claude', 'codex'].includes(body.tool) || !/^[0-9a-f]{64}$/.test(body.device_hash || '')) {
        return [422, { detail: 'Invalid sign-in request' }];
      }
      seq += 1;
      const requestId = `req-${seq}`;
      const pollSecret = `poll-${crypto.randomBytes(12).toString('hex')}`;
      const userCode = `ABCD-${String.fromCharCode(65 + (seq % 26))}FGH`;
      api.requests.set(requestId, {
        pollSecret,
        userCode,
        tool: body.tool,
        deviceHash: body.device_hash,
        deviceName: body.device_name,
        polls: 0,
        done: false,
      });
      return [
        200,
        {
          request_id: requestId,
          poll_secret: pollSecret,
          user_code: userCode,
          verify_url: api.state.verifyUrl || `${SITE}/connect?code=${userCode}`,
          interval: 0,
          expires_in: 600,
        },
      ];
    }
    if (method === 'POST' && url === '/code/cli/poll') {
      const request = api.requests.get(body.request_id);
      if (!request || request.done || request.pollSecret !== body.poll_secret) return [404, { detail: 'Not found' }];
      request.polls += 1;
      if (api.state.pollStatus) return [200, { status: api.state.pollStatus }];
      if (Array.isArray(api.state.deviceLimit)) {
        request.limitPolls = (request.limitPolls || 0) + 1;
        const after = api.state.expireAfterLimitPolls;
        if (after && request.limitPolls > after) return [200, { status: 'expired' }];
        return [200, { status: 'device_limit', devices: api.state.deviceLimit }];
      }
      if (request.polls <= api.state.pollsBeforeApproval) return [200, { status: 'pending' }];
      request.done = true;
      seq += 1;
      const deviceKey = `cs_dev_${crypto.randomBytes(16).toString('hex')}`;
      const deviceId = `dev-${seq}`;
      api.devices.set(deviceKey, {
        id: deviceId,
        name: request.deviceName,
        deviceHash: request.deviceHash,
        tools: [request.tool],
        revoked: false,
      });
      api.issued.push(deviceKey);
      return [200, { status: 'approved', device_id: deviceId, device_key: deviceKey, api_url: api.url }];
    }
    if (method === 'POST' && url === '/code/entitlement') {
      const device = deviceOf(req);
      if (!device || device.revoked) return [401, { detail: 'Invalid device key' }];
      if (body.device_hash !== device.deviceHash) return [403, { detail: 'Device does not match' }];
      if (api.state.entitlementStatus) return [api.state.entitlementStatus, { detail: 'Unavailable' }];
      if (api.state.plan === 'ended') {
        return [402, { detail: { code: 'plan_required', trial_ended_at: '2026-10-01T00:00:00Z' } }];
      }
      const now = Math.floor(Date.now() / 1000);
      const until = now + (api.state.plan === 'trial' ? api.state.trialDays * 86400 : 30 * 86400);
      const payload = {
        v: 1,
        iss: 'contextshrink-code',
        sub: 'test-user',
        dev: device.id,
        dh: device.deviceHash,
        plan: api.state.plan,
        iat: now,
        exp: Math.min(now + 72 * 3600, until),
        until,
      };
      const token = `${b64url(JSON.stringify(payload))}.${b64url('test-signature')}`;
      return [200, { token, plan: api.state.plan, until }];
    }
    if (method === 'GET' && url === '/code/usage/summary') {
      const device = deviceOf(req);
      if (!device || device.revoked) return [401, { detail: 'Invalid device key' }];
      const summary = api.state.summary;
      return [200, { today: summary, last_7d: summary, last_30d: summary }];
    }
    if (method === 'DELETE' && url === '/code/device') {
      const device = deviceOf(req);
      if (!device) return [401, { detail: 'Invalid device key' }];
      device.revoked = true;
      return [204, null];
    }
    if (method === 'GET' && url === '/engine.zip') return [200, ENGINE_ZIP, 'application/octet-stream'];
    return [404, { detail: 'Not found' }];
  }

  const server = http.createServer((req, res) => {
    let raw = '';
    req.on('data', (chunk) => {
      raw += chunk;
    });
    req.on('end', () => {
      let body = {};
      try {
        body = raw ? JSON.parse(raw) : {};
      } catch {
        body = {};
      }
      api.calls.push({ method: req.method, path: req.url });
      const [status, payload, type] = route(req, body);
      if (Buffer.isBuffer(payload)) {
        res.writeHead(status, { 'content-type': type || 'application/octet-stream' });
        res.end(payload);
        return;
      }
      res.writeHead(status, { 'content-type': 'application/json' });
      res.end(payload === null ? '' : JSON.stringify(payload));
    });
  });

  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      api.url = `http://127.0.0.1:${server.address().port}`;
      api.close = () => new Promise((done) => server.close(done));
      resolve(api);
    });
  });
}

/**
 * A fake local engine: answers /__cs/health with 200 while `state.up` is true.
 *   ok: false    the engine has no plan (trial ended): ok:false, plan "ended"
 *   body: string a raw body to answer with instead of the JSON
 */
function startFakeEngine() {
  const engine = { url: '', hits: 0, state: { up: true } };
  const server = http.createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/__cs/health') {
      engine.hits += 1;
      if (!engine.state.up) {
        res.writeHead(503);
        res.end();
        return;
      }
      const noPlan = engine.state.ok === false;
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(
        engine.state.body !== undefined
          ? engine.state.body
          : JSON.stringify({ ok: !noPlan, engine: ENGINE_VERSION, plan: noPlan ? 'ended' : 'trial' })
      );
      return;
    }
    res.writeHead(404);
    res.end();
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      engine.url = `http://127.0.0.1:${server.address().port}`;
      engine.close = () => new Promise((done) => server.close(done));
      resolve(engine);
    });
  });
}

/**
 * A temp home, a temp LocalAppData and AppData, and an environment that points every
 * path and URL into them. The caller adds its own tool paths (CLAUDE_CONFIG_DIR, CODEX_HOME).
 */
function makeCodeSandbox(api, engine, extra = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'contextshrink-code-'));
  const home = path.join(root, 'home');
  fs.mkdirSync(home, { recursive: true });
  const localAppData = path.join(root, 'LocalAppData');
  const appData = path.join(root, 'AppData');
  const manifestFile = path.join(root, 'engine-manifest.json');
  fs.writeFileSync(manifestFile, JSON.stringify(engineManifestFor(api.url)));
  const log = path.join(root, 'adapter-log.jsonl');
  const env = {
    PATH: process.env.PATH || '',
    SystemRoot: process.env.SystemRoot || '',
    TEMP: root,
    TMP: root,
    HOME: home,
    USERPROFILE: home,
    CONTEXTSHRINK_HOME: path.join(home, '.contextshrink'),
    LOCALAPPDATA: localAppData,
    APPDATA: appData,
    CONTEXTSHRINK_API_URL: api.url,
    CONTEXTSHRINK_ENGINE_URL: engine.url,
    CONTEXTSHRINK_MACHINE_ID: MACHINE_ID,
    CONTEXTSHRINK_NPX_TEST: '1',
    CONTEXTSHRINK_NPX_TEST_PLATFORM: 'win32',
    CONTEXTSHRINK_NPX_TEST_ADAPTER: ADAPTER,
    CONTEXTSHRINK_NPX_ENGINE_MANIFEST: manifestFile,
    CONTEXTSHRINK_NPX_ADAPTER_LOG: log,
    ...extra,
  };
  return {
    root,
    home,
    localAppData,
    appData,
    env,
    log,
    manifestFile,
    engineExe: path.join(localAppData, 'ContextShrink', 'engine', ENGINE_VERSION, 'horizon.exe'),
    startup: path.join(appData, 'Microsoft', 'Windows', 'Start Menu', 'Programs', 'Startup', 'ContextShrink Engine.lnk'),
    deviceFile: path.join(home, '.contextshrink', 'device.json'),
    licenseFile: path.join(home, '.contextshrink', 'license.json'),
    usageFile: path.join(home, '.contextshrink', 'usage-pending.json'),
    engineRoot: path.join(localAppData, 'ContextShrink', 'engine'),
    cleanup: () => fs.rmSync(root, { recursive: true, force: true }),
  };
}

// On the Windows host used for these tests, a Node child process has been seen to die
// with STATUS_STACK_BUFFER_OVERRUN (0xC0000409) during an engine health check, before
// it writes its final output. Every installer step is safe to run again, so the same
// command is run again for that exit code only. Any other exit code is returned as is.
const NATIVE_FAST_FAIL = 3221226505;

function spawnOnce(bin, sb, args, env) {
  return new Promise((resolve) => {
    execFile(
      process.execPath,
      [bin, ...args],
      { env: { ...sb.env, ...env }, cwd: sb.root, timeout: 60000, windowsHide: true },
      (error, stdout, stderr) => {
        const code = error ? (typeof error.code === 'number' ? error.code : 1) : 0;
        resolve({ code, stdout, stderr, output: stdout + stderr });
      }
    );
  });
}

/** Runs one installer CLI as a child process with the sandbox environment. */
async function runBin(bin, sb, args, { env = {} } = {}) {
  let result = await spawnOnce(bin, sb, args, env);
  for (let attempt = 1; attempt < 3 && result.code === NATIVE_FAST_FAIL; attempt += 1) {
    result = await spawnOnce(bin, sb, args, env);
  }
  return result;
}

/** Each call the fake Windows adapter received, in order. */
function adapterCalls(sb) {
  if (!fs.existsSync(sb.log)) return [];
  return fs
    .readFileSync(sb.log, 'utf8')
    .split('\n')
    .filter(Boolean)
    .map((line) => JSON.parse(line));
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

module.exports = {
  MACHINE_ID,
  SITE,
  ENGINE_VERSION,
  ENGINE_ZIP,
  ADAPTER,
  sha256,
  b64url,
  startCodeApi,
  startFakeEngine,
  makeCodeSandbox,
  runBin,
  adapterCalls,
  readJson,
};
