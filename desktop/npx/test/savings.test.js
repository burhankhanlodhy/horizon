'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { promisify } = require('node:util');
const execFile = promisify(require('node:child_process').execFile);
const { format, planOf } = require('../lib/savings-runtime.cjs');
const { withSandbox, runCli, writeSettingsFile, readJson } = require('./support');

const MARKET = 'contextshrink-npx';
const PLUGIN = `${MARKET}@${MARKET}`;
const UNAVAILABLE = 'ContextShrink · savings unavailable';
const SUMMARY = { today: { requests: 18, tokens_before: 1000, tokens_after: 780, tokens_saved: 220, pct: 22 } };

// A sandbox whose Code tab config is owned by npx, so the bar also gets its plugin.
async function fixture(fn) {
  return withSandbox(async ({ api, engine, sb }) => {
    const dir = path.join(sb.home, '.contextshrink', 'npx-ui');
    sb.dataDir = path.join(sb.localAppData, 'ContextShrink');
    fs.mkdirSync(path.join(sb.dataDir, 'claude-code'), { recursive: true });
    fs.writeFileSync(path.join(sb.dataDir, 'claude-code/codetab.json'), JSON.stringify({ owner: 'npx', enabled: true }));
    await fn({ api, engine, sb, dir });
  });
}

async function connect(sb) {
  const res = await runCli(sb, ['connect']);
  assert.equal(res.code, 0, res.output);
  return res;
}

async function render(dir, sb) {
  const res = await execFile(process.execPath, [path.join(dir, 'savings-runtime.cjs')], { env: sb.env, timeout: 4000, windowsHide: true });
  assert.equal(res.stderr, '');
  return res.stdout.trim();
}

// Points the installed runtime at a local summary server (the device file holds its address).
async function summaryServer(sb, fn) {
  const hits = [];
  const reply = { status: 200, data: SUMMARY, hang: false };
  const server = http.createServer((req, res) => {
    hits.push({ url: req.url, authorization: req.headers.authorization });
    if (reply.hang) return;
    res.writeHead(reply.status, { 'content-type': 'application/json' });
    res.end(JSON.stringify(reply.data));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const device = readJson(sb.deviceFile);
  device.api_url = `http://127.0.0.1:${server.address().port}`;
  fs.writeFileSync(sb.deviceFile, JSON.stringify(device));
  try { await fn({ hits, reply }); } finally { await new Promise(resolve => server.close(resolve)); }
}

test('connect installs the stable runtime and the status line; uninstall removes them', () => fixture(async ({ sb, dir }) => {
  await connect(sb);
  const settings = readJson(sb.settings);
  assert.match(settings.statusLine.command, /npx-ui\/savings-runtime\.cjs/);
  assert.ok(!settings.statusLine.command.includes('npx '));
  assert.ok(fs.existsSync(path.join(dir, 'savings-runtime.cjs')));
  const off = await runCli(sb, ['uninstall']);
  assert.equal(off.code, 0, off.output);
  assert.ok(!fs.existsSync(sb.settings));
  assert.ok(!fs.existsSync(dir));
}));

test('existing status line and plugin entries survive connect and uninstall', () => fixture(async ({ sb }) => {
  const original = { statusLine: { type: 'command', command: 'my-own-status' },
    extraKnownMarketplaces: { other: { source: { source: 'directory', path: 'mine' } } },
    enabledPlugins: { other: false, [PLUGIN]: false } };
  writeSettingsFile(sb, JSON.stringify(original));
  await connect(sb);
  assert.deepEqual(readJson(sb.settings).statusLine, original.statusLine);
  assert.equal(readJson(sb.settings).enabledPlugins[PLUGIN], false);
  await runCli(sb, ['uninstall']);
  assert.deepEqual(readJson(sb.settings), original);
}));

test('a Code tab owned by the desktop app still gets the npx line, and uninstall restores exactly', () => fixture(async ({ sb }) => {
  fs.mkdirSync(path.join(sb.dataDir, 'claude-code'), { recursive: true });
  fs.writeFileSync(path.join(sb.dataDir, 'claude-code/codetab.json'), JSON.stringify({ v: 1, enabled: true }));
  const original = { model: 'opus' };
  writeSettingsFile(sb, JSON.stringify(original));
  const res = await connect(sb);
  assert.match(res.stdout, /Savings line added above the prompt in Claude Code and the Desktop Code tab \(new sessions\)\./);
  const settings = readJson(sb.settings);
  assert.ok(settings.statusLine);
  assert.equal(settings.enabledPlugins[PLUGIN], true);
  assert.equal(settings.extraKnownMarketplaces[MARKET].source.source, 'directory');
  await runCli(sb, ['uninstall']);
  assert.deepEqual(readJson(sb.settings), original);
}));

test('a conflicting marketplace is never replaced or implicitly enabled, and the reason is printed', () => fixture(async ({ sb }) => {
  const original = { extraKnownMarketplaces: { [MARKET]: { source: { source: 'directory', path: 'user-plugin' } } } };
  writeSettingsFile(sb, JSON.stringify(original));
  const res = await connect(sb);
  assert.match(res.stdout, new RegExp(`Savings line not added: a "${MARKET}" marketplace in Claude Code settings already points elsewhere\\.`));
  assert.deepEqual(readJson(sb.settings).extraKnownMarketplaces, original.extraKnownMarketplaces);
  assert.ok(!readJson(sb.settings).enabledPlugins);
  await runCli(sb, ['uninstall']);
  assert.deepEqual(readJson(sb.settings), original);
}));

test('when the desktop app already shows its line, the npx line is skipped with one reason and uninstall restores exactly', () => fixture(async ({ sb }) => {
  const original = { enabledPlugins: { 'contextshrink@contextshrink': true } };
  writeSettingsFile(sb, JSON.stringify(original));
  const res = await connect(sb);
  assert.match(res.stdout, /Savings line not added: the ContextShrink desktop app already shows its savings line above the prompt\./);
  assert.ok(!res.stdout.includes('Savings line added'));
  const settings = readJson(sb.settings);
  assert.ok(!settings.extraKnownMarketplaces);
  assert.deepEqual(settings.enabledPlugins, original.enabledPlugins);
  await runCli(sb, ['uninstall']);
  assert.deepEqual(readJson(sb.settings), original);
}));

test('a fresh connect prints that the line was added, and uninstall removes everything it added', () => bareFixture(async ({ sb }) => {
  const res = await connect(sb);
  assert.match(res.stdout, /Savings line added above the prompt in Claude Code and the Desktop Code tab \(new sessions\)\./);
  const off = await runCli(sb, ['uninstall']);
  assert.equal(off.code, 0, off.output);
  assert.ok(!fs.existsSync(sb.settings));
}));

test('--no-code-tab installs only the terminal bar, and status describes it', () => fixture(async ({ sb }) => {
  const res = await runCli(sb, ['connect', '--no-code-tab']);
  assert.equal(res.code, 0, res.output);
  assert.ok(readJson(sb.settings).statusLine);
  assert.ok(!readJson(sb.settings).extraKnownMarketplaces);
  const status = await runCli(sb, ['status']);
  assert.match(status.stdout, /Savings bar: terminal configured; Code tab left alone/);
}));

test('unrelated files in the runtime folder survive uninstall, and the runtime is removed', () => fixture(async ({ sb, dir }) => {
  await connect(sb);
  fs.writeFileSync(path.join(dir, 'user-note.txt'), 'keep me');
  await runCli(sb, ['uninstall']);
  assert.equal(fs.readFileSync(path.join(dir, 'user-note.txt'), 'utf8'), 'keep me');
  assert.ok(!fs.existsSync(path.join(dir, 'savings-runtime.cjs')));
}));

test('edited bar settings and unrelated files are preserved on uninstall', () => fixture(async ({ sb, dir }) => {
  await connect(sb);
  const settings = readJson(sb.settings);
  settings.statusLine.padding = 2;
  settings.enabledPlugins = { 'someone@else': true };
  fs.writeFileSync(sb.settings, JSON.stringify(settings));
  fs.writeFileSync(path.join(dir, 'user-note.txt'), 'keep me');
  const off = await runCli(sb, ['uninstall']);
  assert.equal(off.code, 0, off.output);
  const after = readJson(sb.settings);
  assert.equal(after.statusLine.padding, 2);
  assert.deepEqual(after.enabledPlugins, { 'someone@else': true });
  assert.match(off.stdout, /Kept savings-bar settings/);
  assert.ok(fs.existsSync(path.join(dir, 'savings-runtime.cjs')));
  assert.equal(fs.readFileSync(path.join(dir, 'user-note.txt'), 'utf8'), 'keep me');
}));

test('a second connect keeps the bar records, so uninstall still removes them', () => fixture(async ({ sb, dir }) => {
  await connect(sb);
  const records = readJson(sb.state).ui.length;
  await connect(sb);
  assert.equal(readJson(sb.state).ui.length, records);
  await runCli(sb, ['uninstall']);
  assert.ok(!fs.existsSync(sb.settings));
  assert.ok(!fs.existsSync(dir));
}));

test('a connection missing its bar gains it on connect, without signing in again', () => fixture(async ({ api, sb }) => {
  await connect(sb);
  const state = readJson(sb.state);
  delete state.ui;
  fs.writeFileSync(sb.state, JSON.stringify(state));
  const settings = readJson(sb.settings);
  for (const field of ['statusLine', 'enabledPlugins', 'extraKnownMarketplaces']) delete settings[field];
  fs.writeFileSync(sb.settings, JSON.stringify(settings));
  const starts = api.calls.filter(c => c.path === '/code/cli/start').length;
  const res = await runCli(sb, ['connect']);
  assert.equal(res.code, 0, res.output);
  assert.equal(api.calls.filter(c => c.path === '/code/cli/start').length, starts, 'the computer was signed in again');
  assert.ok(readJson(sb.settings).statusLine);
  assert.equal(readJson(sb.state).ui.length, 3);
}));

test('the renderer polls only the usage summary, caches the numbers, and never stores or prints the device key', () => fixture(async ({ api, sb, dir }) => {
  await connect(sb);
  await summaryServer(sb, async ({ hits }) => {
    const line = await render(dir, sb);
    assert.equal(line, 'ContextShrink · usage saved 22% today · free week 7d left');
    assert.equal(await render(dir, sb), line);
    assert.equal(hits.length, 1);
    assert.equal(hits[0].url, '/code/usage/summary');
    const key = readJson(sb.deviceFile).device_key;
    assert.equal(hits[0].authorization, `Bearer ${key}`);
    assert.ok(!line.includes(key));
    assert.ok(!fs.readFileSync(path.join(dir, 'summary-cache.json'), 'utf8').includes(key));
    assert.ok(api.issued.length === 1);
  });
}));

test('the generated status line runs through Windows Git Bash with spaced Node paths',
  { skip: process.platform !== 'win32' || !fs.existsSync('C:/Program Files/Git/bin/bash.exe') }, () => fixture(async ({ sb, dir }) => {
    await connect(sb);
    await summaryServer(sb, async () => {
      const command = readJson(sb.settings).statusLine.command;
      const res = await execFile('C:/Program Files/Git/bin/bash.exe', ['--noprofile', '--norc', '-c', command], { env: sb.env, cwd: sb.root, timeout: 5000, windowsHide: true });
      assert.match(res.stdout, /usage saved 22% today/);
      assert.equal(res.stderr, '');
    });
  }));

test('a summary the server refuses (401) shows as unavailable, and the cached numbers are not kept', () => fixture(async ({ sb, dir }) => {
  await connect(sb);
  await summaryServer(sb, async ({ reply, hits }) => {
    await render(dir, sb);
    const file = path.join(dir, 'summary-cache.json');
    const cache = readJson(file); cache.fetchedAt -= 60000;
    fs.writeFileSync(file, JSON.stringify(cache));
    reply.status = 401;
    assert.equal(await render(dir, sb), UNAVAILABLE);
    assert.equal(hits.length, 2);
  });
}));

test('a new device key invalidates the cache', () => fixture(async ({ sb, dir }) => {
  await connect(sb);
  await summaryServer(sb, async ({ reply, hits }) => {
    await render(dir, sb);
    const device = readJson(sb.deviceFile);
    device.device_key = 'cs_dev_NEWKEYabcdefghijklmnopqrstuv';
    fs.writeFileSync(sb.deviceFile, JSON.stringify(device));
    reply.data = { today: { requests: 0, tokens_before: 0, tokens_saved: 0, pct: 0 } };
    assert.equal(await render(dir, sb), 'ContextShrink · usage saved 0.0% today · free week 7d left');
    assert.equal(hits.length, 2);
    assert.notEqual(hits[0].authorization, hits[1].authorization);
  });
}));

test('an edited route stops polling and shows the runtime as disconnected', () => fixture(async ({ sb, dir }) => {
  await connect(sb);
  await summaryServer(sb, async ({ hits }) => {
    const settings = readJson(sb.settings); settings.env.ANTHROPIC_BASE_URL = 'https://another.example';
    fs.writeFileSync(sb.settings, JSON.stringify(settings));
    assert.equal(await render(dir, sb), 'ContextShrink · disconnected');
    assert.equal(hits.length, 0);
  });
}));

test('HTTP errors are cached, and a hanging service does not hang the status line', () => fixture(async ({ sb, dir }) => {
  await connect(sb);
  await summaryServer(sb, async ({ reply, hits }) => {
    reply.status = 404;
    assert.equal(await render(dir, sb), UNAVAILABLE);
    assert.equal(await render(dir, sb), UNAVAILABLE);
    assert.equal(hits.length, 1);
    fs.rmSync(path.join(dir, 'summary-cache.json'));
    reply.hang = true;
    const start = Date.now();
    assert.equal(await render(dir, sb), UNAVAILABLE);
    assert.ok(Date.now() - start < 3500);
  });
}));

test('the status line text carries no dollar amounts, and free-week days come from the plan note', () => {
  const now = Date.parse('2026-10-20T00:00:00Z');
  const payload = (plan, until) => Buffer.from(JSON.stringify({ plan, until: until / 1000 })).toString('base64url') + '.c2ln';
  assert.equal(format(SUMMARY.today.pct, planOf(writeTemp(payload('trial', now + 5 * 86400000)), now)), 'ContextShrink · usage saved 22% today · free week 5d left');
  assert.equal(format(SUMMARY.today.pct, planOf(writeTemp(payload('trial', now - 1000)), now)), 'ContextShrink · usage saved 22% today · free week ended');
  assert.equal(format(SUMMARY.today.pct, planOf(writeTemp(payload('active', now + 5 * 86400000)), now)), 'ContextShrink · usage saved 22% today · Subscribed');
  assert.equal(format(SUMMARY.today.pct, null), 'ContextShrink · usage saved 22% today');
  assert.equal(format(150, null), 'ContextShrink · usage saved 100% today');
  assert.equal(format(4.26, null), 'ContextShrink · usage saved 4.3% today');
  assert.equal(format(9.96, null), 'ContextShrink · usage saved 10% today');
  assert.equal(format(0, null), 'ContextShrink · usage saved 0.0% today');
  assert.doesNotMatch(format(SUMMARY, 'free week 5d left'), /\$/);
});

function writeTemp(token) {
  const dir = fs.mkdtempSync(require('node:os').tmpdir() + path.sep + 'cs-plan-');
  const file = path.join(dir, 'license.json');
  fs.writeFileSync(file, JSON.stringify({ version: 1, token, fetched_at: new Date().toISOString() }));
  return file;
}

test('the Code-tab hook renders the above-prompt band, keeps other content, and refreshes', () => fixture(async ({ sb, dir }) => {
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


// A sandbox with no Code tab config at all: Claude Desktop's Code tab is not patched yet.
async function bareFixture(fn) {
  return withSandbox(async ({ api, engine, sb }) => {
    sb.dataDir = path.join(sb.localAppData, 'ContextShrink');
    await fn({ api, engine, sb, dir: path.join(sb.home, '.contextshrink', 'npx-ui') });
  });
}

function todayHour(offsetDays = 0) {
  const day = new Date(Date.now() + offsetDays * 86400000).toISOString().slice(0, 10);
  return `${day}T00:00:00Z`;
}

function writeUsage(sb, buckets) {
  fs.writeFileSync(path.join(sb.home, '.contextshrink', 'usage-pending.json'),
    JSON.stringify({ version: 1, buckets }));
}

function planToken(plan, until) {
  return Buffer.from(JSON.stringify({ plan, until: until / 1000 })).toString('base64url') + '.c2ln';
}

test('a fresh connect registers the Code-tab plugin even before the Code tab is patched', () => bareFixture(async ({ sb }) => {
  await connect(sb);
  const settings = readJson(sb.settings);
  assert.deepEqual(settings.extraKnownMarketplaces[MARKET], {
    source: { source: 'directory', path: path.join(sb.home, '.contextshrink', 'npx-ui', 'plugin') },
  });
  assert.equal(settings.enabledPlugins[PLUGIN], true);
  assert.ok(fs.existsSync(path.join(sb.home, '.contextshrink', 'npx-ui', 'plugin', '.claude-plugin', 'marketplace.json')));
  const off = await runCli(sb, ['uninstall']);
  assert.equal(off.code, 0, off.output);
  assert.ok(!fs.existsSync(sb.settings));
}));

test('an upgrade from a 0.3.2 connection (bar only) gains the plugin entries', () => bareFixture(async ({ sb, dir }) => {
  await connect(sb);
  const settings = readJson(sb.settings);
  delete settings.extraKnownMarketplaces;
  delete settings.enabledPlugins;
  fs.writeFileSync(sb.settings, JSON.stringify(settings));
  const state = readJson(sb.state);
  state.ui = state.ui.filter(entry => entry.group === 'statusLine');
  fs.writeFileSync(sb.state, JSON.stringify(state));

  const res = await runCli(sb, ['connect']);
  assert.equal(res.code, 0, res.output);
  const upgraded = readJson(sb.settings);
  assert.ok(upgraded.statusLine);
  assert.equal(upgraded.enabledPlugins[PLUGIN], true);
  assert.equal(upgraded.extraKnownMarketplaces[MARKET].source.path, path.join(dir, 'plugin'));
  const off = await runCli(sb, ['uninstall']);
  assert.equal(off.code, 0, off.output);
  assert.ok(!fs.existsSync(sb.settings));
}));

test('the status line reads today from the local usage file, with no network call', () => fixture(async ({ sb, dir }) => {
  await connect(sb);
  writeUsage(sb, [
    { hour: todayHour(), tool: 'claude', requests: 40, tokens_before: 1000, tokens_after: 957 },
    { hour: todayHour(-1), tool: 'claude', requests: 9, tokens_before: 9000, tokens_after: 0 },
    { hour: todayHour(), tool: 'codex', requests: 9, tokens_before: 9000, tokens_after: 0 },
  ]);
  await summaryServer(sb, async ({ hits }) => {
    assert.match(await render(dir, sb), /^ContextShrink · usage saved 4\.3% today · free week \d+d left$/);
    assert.equal(hits.length, 0);
  });
}));

test('a subscription shows Subscribed, and whole percentages from 10 up', () => fixture(async ({ sb, dir }) => {
  await connect(sb);
  fs.writeFileSync(path.join(sb.home, '.contextshrink', 'license.json'),
    JSON.stringify({ version: 1, token: planToken('active', Date.now() + 86400000), fetched_at: new Date().toISOString() }));
  writeUsage(sb, [{ hour: todayHour(), tool: 'claude', requests: 5, tokens_before: 1000, tokens_after: 573 }]);
  assert.equal(await render(dir, sb), 'ContextShrink · usage saved 43% today · Subscribed');
}));

test('without a local usage file the bar falls back to the summary API once', () => fixture(async ({ sb, dir }) => {
  await connect(sb);
  await summaryServer(sb, async ({ hits }) => {
    assert.equal(await render(dir, sb), 'ContextShrink · usage saved 22% today · free week 7d left');
    assert.equal(hits.length, 1);
  });
}));

test('the status-bar helper is byte-identical in both packages', () => {
  const other = path.join(__dirname, '..', '..', 'npx-codex', 'lib', 'local-savings.cjs');
  if (!fs.existsSync(other)) return;
  assert.ok(fs.readFileSync(path.join(__dirname, '..', 'lib', 'local-savings.cjs')).equals(fs.readFileSync(other)));
});
