'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { promisify } = require('node:util');
const execFile = promisify(require('node:child_process').execFile);
const { format } = require('../lib/savings-runtime.cjs');
const { EMAIL, PASSWORD, startFakeApi, makeSandbox, runCli, readJson, writeSettingsFile } = require('./support');
const SIGN_IN = `${EMAIL}\n${PASSWORD}\n`;
const MARKET = 'contextshrink-npx';
const PLUGIN = `${MARKET}@${MARKET}`;
const SUMMARY = { scope: 'key', period: 'today_utc', requests: 18, tokens_before: 1000, tokens_saved: 220, savings_usd: 0.41 };

async function fixture(fn) {
  const api = await startFakeApi();
  const sb = makeSandbox(api);
  // Model a Code-tab config owned by npx; the helper/launcher lifecycle is
  // exercised separately against fake binaries in codetab.test.js.
  fs.mkdirSync(path.join(sb.dataDir, 'claude-code'), { recursive: true });
  fs.writeFileSync(path.join(sb.dataDir, 'claude-code/codetab.json'), JSON.stringify({ owner: 'npx', enabled: true }));
  const dir = path.join(sb.home, '.contextshrink', 'npx-ui');
  try { await fn({ api, sb, dir }); } finally { sb.cleanup(); await api.close(); }
}
async function connect(sb) {
  const res = await runCli(sb, [], { input: SIGN_IN });
  assert.equal(res.code, 0, res.output);
  return res;
}
async function render(dir, sb) {
  const res = await execFile(process.execPath, [path.join(dir, 'savings-runtime.cjs')], { env: sb.env, timeout: 4000, windowsHide: true });
  assert.equal(res.stderr, '');
  return res.stdout.trim();
}
async function summaryServer(dir, fn) {
  const hits = [];
  const reply = { status: 200, data: SUMMARY, hang: false };
  const server = http.createServer((req, res) => {
    hits.push({ url: req.url, authorization: req.headers.authorization });
    if (reply.hang) return;
    res.writeHead(reply.status, { 'content-type': 'application/json' });
    res.end(JSON.stringify(reply.data));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const configPath = path.join(dir, 'ui-config.json');
  const config = readJson(configPath);
  config.apiUrl = `http://127.0.0.1:${server.address().port}`;
  fs.writeFileSync(configPath, JSON.stringify(config));
  try { await fn({ hits, reply }); } finally { await new Promise(resolve => server.close(resolve)); }
}

test('connect installs stable runtime, terminal status line and above-prompt plugin; disconnect removes them', () => fixture(async ({ sb, dir }) => {
  await connect(sb);
  const settings = readJson(sb.settings);
  assert.match(settings.statusLine.command, /npx-ui\/savings-runtime\.cjs/);
  assert.ok(!settings.statusLine.command.includes('npx '));
  const plugin = settings.extraKnownMarketplaces[MARKET].source.path;
  assert.equal(settings.enabledPlugins[PLUGIN], true);
  assert.equal(readJson(path.join(plugin, '.claude-plugin/plugin.json')).name, MARKET);
  assert.ok(fs.existsSync(path.join(plugin, 'hooks/register.js')));
  const off = await runCli(sb, ['disconnect']);
  assert.equal(off.code, 0, off.output);
  assert.ok(!fs.existsSync(sb.settings));
  assert.ok(!fs.existsSync(dir));
}));

test('existing status line, plugins, disabled entries and empty maps survive connect/disconnect', () => fixture(async ({ sb }) => {
  const original = { statusLine: { type: 'command', command: 'my-own-status' },
    extraKnownMarketplaces: { other: { source: { source: 'directory', path: 'mine' } } },
    enabledPlugins: { other: false, [PLUGIN]: false } };
  writeSettingsFile(sb, JSON.stringify(original));
  await connect(sb);
  assert.deepEqual(readJson(sb.settings).statusLine, original.statusLine);
  assert.equal(readJson(sb.settings).enabledPlugins[PLUGIN], false);
  await runCli(sb, ['disconnect']);
  assert.deepEqual(readJson(sb.settings), original);
}));

test('desktop-owned Code tab keeps its existing bar and receives no npx plugin', () => fixture(async ({ sb }) => {
  fs.mkdirSync(path.join(sb.dataDir, 'claude-code'), { recursive: true });
  fs.writeFileSync(path.join(sb.dataDir, 'claude-code/codetab.json'), JSON.stringify({ v: 1, enabled: true }));
  await connect(sb);
  const settings = readJson(sb.settings);
  assert.ok(settings.statusLine);
  assert.ok(!settings.extraKnownMarketplaces);
  assert.ok(!settings.enabledPlugins);
}));

test('conflicting marketplace is never replaced or implicitly enabled', () => fixture(async ({ sb }) => {
  const original = { extraKnownMarketplaces: { [MARKET]: { source: { source: 'directory', path: 'user-plugin' } } } };
  writeSettingsFile(sb, JSON.stringify(original));
  await connect(sb);
  assert.deepEqual(readJson(sb.settings).extraKnownMarketplaces, original.extraKnownMarketplaces);
  assert.ok(!readJson(sb.settings).enabledPlugins);
  await runCli(sb, ['disconnect']);
  assert.deepEqual(readJson(sb.settings), original);
}));

test('--no-code-tab installs only the terminal bar and status describes it', () => fixture(async ({ sb }) => {
  const res = await runCli(sb, ['--no-code-tab'], { input: SIGN_IN });
  assert.equal(res.code, 0, res.output);
  assert.ok(readJson(sb.settings).statusLine);
  assert.ok(!readJson(sb.settings).extraKnownMarketplaces);
  const status = await runCli(sb, ['status']);
  assert.match(status.stdout, /Savings bar: terminal configured; Code tab left alone/);
}));

test('new unrelated files survive cleanup even when no settings refer to the runtime', () => fixture(async ({ sb, dir }) => {
  await connect(sb);
  fs.writeFileSync(path.join(dir, 'user-note.txt'), 'keep me');
  await runCli(sb, ['disconnect']);
  assert.equal(fs.readFileSync(path.join(dir, 'user-note.txt'), 'utf8'), 'keep me');
  assert.ok(!fs.existsSync(path.join(dir, 'savings-runtime.cjs')));
}));

test('edited bar settings and unrelated files are preserved on disconnect', () => fixture(async ({ sb, dir }) => {
  await connect(sb);
  const settings = readJson(sb.settings);
  settings.statusLine.padding = 2;
  settings.enabledPlugins['someone@else'] = true;
  fs.writeFileSync(sb.settings, JSON.stringify(settings));
  fs.writeFileSync(path.join(dir, 'user-note.txt'), 'keep me');
  const off = await runCli(sb, ['disconnect']);
  assert.equal(off.code, 0, off.output);
  const after = readJson(sb.settings);
  assert.equal(after.statusLine.padding, 2);
  assert.deepEqual(after.enabledPlugins, { 'someone@else': true });
  assert.match(off.stdout, /Kept savings-bar settings/);
  assert.ok(fs.existsSync(path.join(dir, 'savings-runtime.cjs')));
  assert.equal(fs.readFileSync(path.join(dir, 'user-note.txt'), 'utf8'), 'keep me');
}));

test('login retains UI ownership so key rotation still disconnects cleanly', () => fixture(async ({ sb, dir }) => {
  await connect(sb);
  const oldKey = readJson(sb.state).written.ANTHROPIC_CUSTOM_HEADERS;
  const login = await runCli(sb, ['login'], { input: SIGN_IN });
  assert.equal(login.code, 0, login.output);
  assert.notEqual(readJson(sb.state).written.ANTHROPIC_CUSTOM_HEADERS, oldKey);
  assert.equal(readJson(sb.state).ui.length, 3);
  await runCli(sb, ['disconnect']);
  assert.ok(!fs.existsSync(sb.settings));
  assert.ok(!fs.existsSync(dir));
}));

test('0.2.0 connections gain the bar without signing in again', () => fixture(async ({ api, sb }) => {
  await connect(sb);
  const state = readJson(sb.state);
  delete state.ui;
  fs.writeFileSync(sb.state, JSON.stringify(state));
  const settings = readJson(sb.settings);
  for (const field of ['statusLine', 'enabledPlugins', 'extraKnownMarketplaces']) delete settings[field];
  fs.writeFileSync(sb.settings, JSON.stringify(settings));
  const before = api.calls.length;
  const res = await runCli(sb, []);
  assert.equal(res.code, 0, res.output);
  assert.equal(api.calls.length, before);
  assert.ok(readJson(sb.settings).statusLine);
  assert.equal(readJson(sb.state).ui.length, 3);
}));

test('renderer polls only the key summary, caches numeric data and never stores/prints the key', () => fixture(async ({ sb, dir }) => {
  await connect(sb);
  await summaryServer(dir, async ({ hits }) => {
    const line = await render(dir, sb);
    assert.equal(line, 'ContextShrink · trimmed 22% · net saved $0.41 · 18 req · this key today (UTC)');
    assert.equal(await render(dir, sb), line);
    assert.equal(hits.length, 1);
    assert.equal(hits[0].url, '/usage/key-summary');
    const key = readJson(sb.state).written.ANTHROPIC_CUSTOM_HEADERS.split(': ')[1];
    assert.equal(hits[0].authorization, `Bearer ${key}`);
    assert.ok(!line.includes(key));
    assert.ok(!fs.readFileSync(path.join(dir, 'summary-cache.json'), 'utf8').includes(key));
  });
}));

const bash = 'C:/Program Files/Git/bin/bash.exe';
test('the generated terminal command runs through Windows Git Bash with spaced Node paths',
  { skip: process.platform !== 'win32' || !fs.existsSync(bash) }, () => fixture(async ({ sb, dir }) => {
    await connect(sb);
    await summaryServer(dir, async () => {
      const command = readJson(sb.settings).statusLine.command;
      const res = await execFile(bash, ['--noprofile', '--norc', '-c', command], { env: sb.env, cwd: sb.root, timeout: 5000, windowsHide: true });
      assert.match(res.stdout, /trimmed 22% · net saved \$0\.41/);
      assert.equal(res.stderr, '');
    });
  }));

test('expired successful data becomes unavailable on authorization failure', () => fixture(async ({ sb, dir }) => {
  await connect(sb);
  await summaryServer(dir, async ({ reply, hits }) => {
    await render(dir, sb);
    const file = path.join(dir, 'summary-cache.json');
    const cache = readJson(file); cache.fetchedAt -= 60000;
    fs.writeFileSync(file, JSON.stringify(cache));
    reply.status = 401;
    assert.equal(await render(dir, sb), 'ContextShrink · savings unavailable');
    assert.equal(hits.length, 2);
  });
}));

test('key rotation invalidates the old key cache', () => fixture(async ({ sb, dir }) => {
  await connect(sb);
  await summaryServer(dir, async ({ reply, hits }) => {
    await render(dir, sb);
    const state = readJson(sb.state), settings = readJson(sb.settings);
    state.written.ANTHROPIC_CUSTOM_HEADERS = settings.env.ANTHROPIC_CUSTOM_HEADERS = 'X-Horizon-Proxy-Token: cs_live_NEWKEYabcdefghijklmnopqrstuv';
    fs.writeFileSync(sb.state, JSON.stringify(state)); fs.writeFileSync(sb.settings, JSON.stringify(settings));
    reply.data = { ...SUMMARY, requests: 0, tokens_before: 0, tokens_saved: 0, savings_usd: 0 };
    assert.match(await render(dir, sb), /net saved \$0\.00 · 0 req/);
    assert.equal(hits.length, 2);
    assert.notEqual(hits[0].authorization, hits[1].authorization);
  });
}));

test('edited route stops polling and does not show old savings', () => fixture(async ({ sb, dir }) => {
  await connect(sb);
  await summaryServer(dir, async ({ hits }) => {
    const settings = readJson(sb.settings); settings.env.ANTHROPIC_BASE_URL = 'https://another.example';
    fs.writeFileSync(sb.settings, JSON.stringify(settings));
    assert.equal(await render(dir, sb), 'ContextShrink · disconnected');
    assert.equal(hits.length, 0);
  });
}));

test('HTTP errors are cached and invalid summaries/slow services do not fail or hang the status command', () => fixture(async ({ sb, dir }) => {
  await connect(sb);
  await summaryServer(dir, async ({ reply, hits }) => {
    reply.status = 404;
    assert.equal(await render(dir, sb), 'ContextShrink · savings unavailable');
    assert.equal(await render(dir, sb), 'ContextShrink · savings unavailable');
    assert.equal(hits.length, 1);
    fs.rmSync(path.join(dir, 'summary-cache.json'));
    reply.status = 200; reply.data = { ...SUMMARY, savings_usd: 'secret or invalid' };
    assert.equal(await render(dir, sb), 'ContextShrink · savings unavailable');
    fs.rmSync(path.join(dir, 'summary-cache.json'));
    reply.hang = true;
    const start = Date.now();
    assert.equal(await render(dir, sb), 'ContextShrink · savings unavailable');
    assert.ok(Date.now() - start < 3500);
  });
}));

test('rendering reports negative net savings as cost; zeros are valid and non-finite data is unavailable', () => {
  assert.match(format({ ...SUMMARY, savings_usd: -0.08 }), /net cost \$0\.08/);
  assert.match(format({ ...SUMMARY, requests: 0, tokens_before: 0, tokens_saved: 0, savings_usd: 0 }), /trimmed 0% · net saved \$0\.00 · 0 req/);
  assert.equal(format({ ...SUMMARY, savings_usd: Infinity }), 'ContextShrink · savings unavailable');
  assert.equal(format({ ...SUMMARY, scope: 'account' }), 'ContextShrink · savings unavailable');
});

test('Code-tab hook renders the above-prompt band, preserves other content and refreshes', () => fixture(async ({ sb, dir }) => {
  await connect(sb);
  const source = fs.readFileSync(path.join(dir, 'plugin/hooks/register.js'), 'utf8');
  const { register } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
  const handlers = {};
  register((event, ...args) => { handlers[event] = args.at(-1); });
  let timer, runs = 0, invalidations = 0, line = 'ContextShrink · first';
  const $ = { process: { run: async argv => {
    assert.deepEqual(argv, [process.execPath, path.join(dir, 'savings-runtime.cjs')]);
    runs++; return { stdout: line + '\n' };
  } }, clock: { every: (ms, callback) => { assert.equal(ms, 30000); timer = callback; } },
  ui: { invalidate: () => { invalidations++; }, resolve: () => ({ Box: props => props, Text: props => props }) } };
  const next = async () => ({ below: true });
  await handlers['session.start']($, {}, next);
  const band = await handlers['ui.render']($, {}, next);
  assert.deepEqual(band.children[0].children, [line]);
  assert.deepEqual(band.children[1], { below: true });
  line = 'ContextShrink · updated';
  await handlers['turn.complete']($, {}, next);
  assert.equal((await handlers['ui.render']($, {}, next)).children[0].children[0], line);
  await timer();
  assert.equal(runs, 3); assert.equal(invalidations, 2);
}));
