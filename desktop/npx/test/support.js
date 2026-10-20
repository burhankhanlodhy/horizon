'use strict';

// Test helpers. Every path and URL a test run touches lives under a temp
// directory, and the API is a fake server on 127.0.0.1. Nothing here reads the
// real ~/.claude or calls the real ContextShrink services.

const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { execFile } = require('node:child_process');

const BIN = path.join(__dirname, '..', 'bin', 'contextshrink.js');
const EMAIL = 'tester@example.test';
const PASSWORD = 'test-only-password';
const SESSION = 'test-session-token';

/** A fake control plane: sign-in, device keys and sign-out, as the real API serves them. */
function startFakeApi() {
  const calls = [];
  const issued = [];
  const live = new Map();
  let seq = 0;

  const server = http.createServer((req, res) => {
    let raw = '';
    req.on('data', (chunk) => {
      raw += chunk;
    });
    req.on('end', () => {
      calls.push({ method: req.method, path: req.url });
      const send = (status, body) => {
        res.writeHead(status, { 'content-type': 'application/json' });
        res.end(body === undefined ? '' : JSON.stringify(body));
      };
      let body = {};
      try {
        body = raw ? JSON.parse(raw) : {};
      } catch {
        body = {};
      }

      if (req.method === 'POST' && req.url === '/auth/login') {
        if (body.email === EMAIL && body.password === PASSWORD) {
          return send(200, { token: SESSION, user: { id: 'u1', name: 'Tester', email: EMAIL } });
        }
        return send(401, { detail: 'Invalid email or password' });
      }
      if (req.method === 'POST' && req.url === '/auth/logout') return send(204);
      if (req.headers.authorization !== `Bearer ${SESSION}`) {
        return send(401, { detail: 'Not authenticated' });
      }
      if (req.method === 'POST' && req.url === '/keys') {
        seq += 1;
        const id = `00000000-0000-4000-8000-${String(seq).padStart(12, '0')}`;
        const key = `cs_live_TESTKEY${String(seq).padStart(2, '0')}abcdefghijklmnopqrstuv`;
        live.set(id, key);
        issued.push(key);
        return send(201, { id, name: body.name, key, prefix: key.slice(0, 16), scopes: body.scopes });
      }
      const match = /^\/keys\/([0-9a-f-]+)$/.exec(req.url);
      if (req.method === 'DELETE' && match) {
        if (live.has(match[1])) {
          live.delete(match[1]);
          return send(204);
        }
        return send(404, { detail: 'Key not found' });
      }
      return send(404, { detail: 'Not found' });
    });
  });

  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      resolve({
        url: `http://127.0.0.1:${server.address().port}`,
        calls,
        issued,
        live,
        close: () => new Promise((done) => server.close(done)),
      });
    });
  });
}

/** A temp HOME with a .claude folder, and an env that points every path and URL at it. */
function makeSandbox(api, { proxyUrl = 'http://127.0.0.1:9' } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'contextshrink-npx-'));
  const home = path.join(root, 'home');
  const configDir = path.join(home, '.claude');
  fs.mkdirSync(configDir, { recursive: true });
  // The Windows folders the Code tab reads are inside the temp root too, so no test
  // can see or patch the real Claude Desktop folders.
  const localAppData = path.join(root, 'LocalAppData');
  const appData = path.join(root, 'AppData');
  const dataDir = path.join(root, 'ContextShrinkData');
  const env = {
    PATH: process.env.PATH || '',
    SystemRoot: process.env.SystemRoot || '',
    TEMP: root,
    TMP: root,
    HOME: home,
    USERPROFILE: home,
    CLAUDE_CONFIG_DIR: configDir,
    LOCALAPPDATA: localAppData,
    APPDATA: appData,
    CONTEXTSHRINK_DATA_DIR: dataDir,
    CONTEXTSHRINK_API_URL: api.url,
    CONTEXTSHRINK_PROXY_URL: proxyUrl,
  };
  return {
    root,
    home,
    configDir,
    localAppData,
    appData,
    dataDir,
    settings: path.join(configDir, 'settings.json'),
    state: path.join(configDir, '.contextshrink-npx.json'),
    desktopState: path.join(configDir, '.horizon-vscode-claude.json'),
    homeFile: path.join(home, '.contextshrink', 'npx.json'),
    env,
    cleanup: () => fs.rmSync(root, { recursive: true, force: true }),
  };
}

/** Runs the CLI as a child process. `input` is piped to stdin, one answer per line. */
function runCli(sb, args, { input = '' } = {}) {
  return new Promise((resolve) => {
    const child = execFile(
      process.execPath,
      [BIN, ...args],
      { env: sb.env, cwd: sb.root, timeout: 30000, windowsHide: true },
      (error, stdout, stderr) => {
        const code = error ? (typeof error.code === 'number' ? error.code : 1) : 0;
        resolve({ code, stdout, stderr, output: stdout + stderr });
      }
    );
    child.stdin.end(input);
  });
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

function writeSettingsFile(sb, text) {
  fs.writeFileSync(sb.settings, text, 'utf8');
}

module.exports = {
  EMAIL,
  PASSWORD,
  startFakeApi,
  makeSandbox,
  runCli,
  readJson,
  writeSettingsFile,
};
