'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { parse } = require('smol-toml');
const { connectText, restore, ID } = require('../lib/toml');
const code = require('./code-support');
const { MACHINE_ID } = code;
const { deviceHashOf } = require('../lib/code-device');

const BIN = path.join(__dirname, '../bin/contextshrink-codex.js');
const DEVICE_KEY_RE = /cs_dev_[A-Za-z0-9_-]+/;

// A sandbox with its own CODEX_HOME, a fake API and a fake engine.
async function fixture(fn) {
  const api = await code.startCodeApi();
  const engine = await code.startFakeEngine();
  const sb = code.makeCodeSandbox(api, engine);
  sb.dir = path.join(sb.home, '.codex');
  fs.mkdirSync(sb.dir, { recursive: true });
  sb.env.CODEX_HOME = sb.dir;
  sb.config = path.join(sb.dir, 'config.toml');
  sb.state = path.join(sb.dir, '.contextshrink-npx-codex.json');
  const run = (args = []) => code.runBin(BIN, sb, args);
  try {
    await fn({ api, engine, sb, run, dir: sb.dir, config: sb.config, state: sb.state });
  } finally {
    sb.cleanup();
    await engine.close();
    await api.close();
  }
}

function assertNoDeviceKey(api, texts) {
  for (const key of api.issued) for (const text of texts) assert.ok(!text.includes(key), 'a device key was printed');
  for (const text of texts) assert.ok(!DEVICE_KEY_RE.test(text), 'a device key-shaped value was printed');
}

test('connect points a ChatGPT-auth Responses provider at the engine, with no account key header', () => fixture(async f => {
  const original = '# keep comment\nmodel = "my-existing-model"\n[features]\nfoo = true\n';
  fs.writeFileSync(f.config, original);
  const res = await f.run(); assert.equal(res.code, 0, res.output);
  const raw = fs.readFileSync(f.config, 'utf8'), config = parse(raw), provider = config.model_providers[ID];
  assert.equal(config.model_provider, ID); assert.equal(config.model, 'my-existing-model'); assert.equal(config.features.foo, true);
  assert.ok(raw.includes(original));
  assert.equal(provider.requires_openai_auth, true); assert.equal(provider.supports_websockets, true);
  assert.equal(provider.wire_api, 'responses');
  assert.equal(provider.base_url, `${f.engine.url}/v1`);
  assert.deepEqual(Object.keys(provider.http_headers), ['X-Horizon-Model-Catalog']);
  assert.equal(provider.http_headers['X-Horizon-Model-Catalog'], 'codex');
  assert.ok(!raw.includes('X-Horizon-Proxy-Token'), 'the account key header is still written');
  assert.ok(!raw.includes('cs_dev_'), 'the device key was written to config.toml');
  assert.ok(!provider.env_key); assert.ok(!provider.auth);
  assert.match(res.stdout, /Codex configured for the ContextShrink engine/);
  assert.deepEqual(readDevice(f).tools, ['codex']);
  assert.equal(readDevice(f).device_hash, deviceHashOf(MACHINE_ID));
  assert.ok(fs.existsSync(f.sb.engineExe), 'the engine was not installed');
  assert.ok(fs.existsSync(f.sb.startup), 'the Startup shortcut was not created');
  assert.equal(f.api.issued.length, 1);
  assertNoDeviceKey(f.api, [res.stdout, res.stderr]);
  const status = await f.run(['status']); assert.equal(status.code, 0, status.output);
  assert.match(status.stdout, /Configured for 127\.0\.0\.1/); assert.match(status.stdout, /Device: .*\(tools: codex\)/);
  assert.match(status.stdout, /Free trial: 7 days left/); assert.match(status.stdout, /Usage saved today: 22%/);
  assertNoDeviceKey(f.api, [status.stdout, status.stderr]);
  const off = await f.run(['uninstall']); assert.equal(off.code, 0, off.output);
  assert.equal(fs.readFileSync(f.config, 'utf8'), original); assert.ok(!fs.existsSync(f.state));
  assert.ok(!fs.existsSync(f.sb.deviceFile), 'device.json was left behind');
  assert.equal(f.api.devices.get(f.api.issued[0]).revoked, true, 'the device was not released');
  assert.ok(!fs.existsSync(f.sb.engineRoot), 'the engine was left behind');
  assert.match(off.stdout, /This device is disconnected/);
}));

test('a second connect needs no sign-in and leaves the config byte-for-byte the same', () => fixture(async f => {
  assert.equal((await f.run()).code, 0);
  const before = fs.readFileSync(f.config, 'utf8'), starts = f.api.calls.filter(c => c.path === '/code/cli/start').length;
  const again = await f.run(); assert.equal(again.code, 0, again.output);
  assert.match(again.stdout, /Already configured/);
  assert.equal(fs.readFileSync(f.config, 'utf8'), before);
  assert.equal(f.api.calls.filter(c => c.path === '/code/cli/start').length, starts);
}));

for (const original of ['', 'model_provider = "openai"', '# settings\r\nmodel_provider = "openai" # mine\r\n[features]\r\nfoo = true\r\n']) {
  test('round-trip restores original formatting: ' + JSON.stringify(original.slice(0, 35)), () => fixture(async f => {
    fs.writeFileSync(f.config, original);
    assert.equal((await f.run()).code, 0);
    const off = await f.run(['uninstall']); assert.equal(off.code, 0, off.output);
    assert.equal(fs.readFileSync(f.config, 'utf8'), original);
  }));
}

test('a new config is removed on uninstall without touching auth or other files', () => fixture(async f => {
  fs.writeFileSync(path.join(f.dir, 'auth.json'), '{"auth_mode":"chatgpt","tokens":{"fake":"unchanged"}}');
  assert.equal((await f.run()).code, 0); assert.equal((await f.run(['uninstall'])).code, 0);
  assert.ok(!fs.existsSync(f.config));
  assert.equal(fs.readFileSync(path.join(f.dir, 'auth.json'), 'utf8'), '{"auth_mode":"chatgpt","tokens":{"fake":"unchanged"}}');
}));

for (const original of ['bad = [', 'model_provider = "horizon"\n', 'openai_base_url = "http://other.example"\n', '[profiles.work]\nmodel_provider="mine"\n', '[model_providers.contextshrink_npx]\nname="mine"\n']) {
  test('refuses conflicting or invalid routing before sign-in: ' + original.split('\n')[0], () => fixture(async f => {
    fs.writeFileSync(f.config, original);
    const r = await f.run(); assert.equal(r.code, 1); assert.equal(f.api.calls.length, 0);
    assert.equal(fs.readFileSync(f.config, 'utf8'), original); assert.ok(!fs.existsSync(f.state));
  }));
}

test('API-key Codex auth is refused without reading or changing its key', () => fixture(async f => {
  const auth = '{"auth_mode":"apikey","OPENAI_API_KEY":"fake-private-api-key"}';
  fs.writeFileSync(path.join(f.dir, 'auth.json'), auth);
  const r = await f.run(); assert.equal(r.code, 1); assert.equal(f.api.calls.length, 0);
  assert.ok(!r.output.includes('fake-private-api-key')); assert.equal(fs.readFileSync(path.join(f.dir, 'auth.json'), 'utf8'), auth);
}));

test('a device limit exits with code 3 and leaves the config untouched', () => fixture(async f => {
  const original = 'model = "keep"\n'; fs.writeFileSync(f.config, original);
  f.api.state.deviceLimit = [{ name: 'Office PC' }, { name: 'Home laptop' }];
  const r = await f.run(); assert.equal(r.code, 3, r.output);
  assert.match(r.stderr, /This account already has 2 devices connected: Office PC, Home laptop\./);
  assert.match(r.stderr, /run "npx contextshrink-codex uninstall" on that device/);
  assert.equal(fs.readFileSync(f.config, 'utf8'), original); assert.ok(!fs.existsSync(f.state));
}));

test('a sign-in the server no longer accepts (401) starts sign-in again', () => fixture(async f => {
  fs.mkdirSync(path.dirname(f.sb.deviceFile), { recursive: true });
  fs.writeFileSync(f.sb.deviceFile, JSON.stringify({ version: 1, api_url: f.api.url, device_id: 'dev-old', device_key: 'cs_dev_staleStaleStaleStale00', device_hash: deviceHashOf(MACHINE_ID), tools: ['codex'], created_at: new Date().toISOString() }));
  const r = await f.run(); assert.equal(r.code, 0, r.output);
  assert.match(r.stdout, /sign-in has expired/);
  assert.equal(readDevice(f).device_key, f.api.issued[0]);
}));

test('a trial that has ended still points Codex at the engine, and says Codex works without compression', () => fixture(async f => {
  f.api.state.plan = 'ended';
  const r = await f.run(); assert.equal(r.code, 0, r.output);
  assert.match(r.stdout, /Your trial has ended\. Subscribe for \$5\/month at https:\/\/code\.contextshrink\.com\/account\. Until then, Codex works normally without compression\./);
  assert.match(fs.readFileSync(f.config, 'utf8'), /127\.0\.0\.1:\d+\/v1/);
}));

test('unrelated edits before and after the block survive uninstall', () => fixture(async f => {
  const original = 'model_provider="openai"\nmodel="keep"\n'; fs.writeFileSync(f.config, original);
  assert.equal((await f.run()).code, 0);
  fs.writeFileSync(f.config, fs.readFileSync(f.config, 'utf8').replace('model="keep"', 'model="edited"') + '\n[features]\nnew_feature=true\n');
  const off = await f.run(['uninstall']); assert.equal(off.code, 0, off.output);
  const raw = fs.readFileSync(f.config, 'utf8'); assert.ok(raw.includes('model="edited"')); assert.ok(raw.includes('new_feature=true'));
  assert.equal(parse(raw).model_provider, 'openai'); assert.ok(!parse(raw).model_providers);
}));

test('edits to the managed routing block stop uninstall, and nothing is removed', () => fixture(async f => {
  assert.equal((await f.run()).code, 0);
  const raw = fs.readFileSync(f.config, 'utf8').replace('supports_websockets = true', 'supports_websockets = false'); fs.writeFileSync(f.config, raw);
  const off = await f.run(['uninstall']); assert.equal(off.code, 1);
  assert.equal(fs.readFileSync(f.config, 'utf8'), raw); assert.ok(fs.existsSync(f.state));
  assert.ok(fs.existsSync(f.sb.deviceFile), 'the device was released after a failed restore');
}));

test('uninstall shares the device with Claude Code: the device and engine stay while Claude is still connected', () => fixture(async f => {
  assert.equal((await f.run()).code, 0);
  const device = readDevice(f);
  device.tools = ['claude', 'codex'];
  fs.writeFileSync(f.sb.deviceFile, JSON.stringify(device));
  const off = await f.run(['uninstall']); assert.equal(off.code, 0, off.output);
  assert.deepEqual(readDevice(f).tools, ['claude']);
  assert.ok(!f.api.devices.get(device.device_key).revoked, 'the device was released while Claude Code was still connected');
  assert.ok(fs.existsSync(f.sb.engineExe), 'the engine was deleted while Claude Code was still connected');
  assert.match(off.stdout, /Claude Code still connected on this device/);
}));

test('the last tool releases the device; disconnect and logout are aliases of uninstall', () => fixture(async f => {
  for (const alias of ['disconnect', 'logout']) {
    assert.equal((await f.run()).code, 0);
    const off = await f.run([alias]); assert.equal(off.code, 0, off.output);
    assert.ok(!fs.existsSync(f.sb.deviceFile), `${alias} did not release the device`);
  }
}));

test('savings shows usage saved and the trial days, with no dollar amounts and no key', () => fixture(async f => {
  assert.equal((await f.run()).code, 0);
  const r = await f.run(['savings']); assert.equal(r.code, 0, r.output);
  assert.equal(r.stdout.trim(), 'ContextShrink · usage saved 22% today · trial 7d left');
  assertNoDeviceKey(f.api, [r.stdout, r.stderr]);
}));

test('a connector lock prevents a second writer without deleting the other lock', () => fixture(async f => {
  fs.writeFileSync(f.state + '.lock', 'other operation');
  assert.equal((await f.run()).code, 1); assert.equal(f.api.calls.length, 0);
  assert.equal(fs.readFileSync(f.state + '.lock', 'utf8'), 'other operation');
}));

test('on a system other than Windows connect refuses with the spec message, before any call', () => fixture(async f => {
  const r = await code.runBin(BIN, f.sb, ['connect'], { env: { CONTEXTSHRINK_NPX_TEST_PLATFORM: 'linux' } });
  assert.equal(r.code, 1);
  assert.match(r.stderr, /ContextShrink Code runs on Windows today\. macOS and Linux are coming\./);
  assert.equal(f.api.calls.length, 0);
}));

test('an engine that does not start leaves the Codex config untouched', () => fixture(async f => {
  f.engine.state.up = false;
  const original = 'model = "keep"\n'; fs.writeFileSync(f.config, original);
  const r = await code.runBin(BIN, f.sb, [], { env: { CONTEXTSHRINK_NPX_STUB_ENGINE: 'fail' } });
  assert.equal(r.code, 1, r.output);
  assert.match(r.stderr, /The ContextShrink engine did not start\. Nothing was changed/);
  assert.equal(fs.readFileSync(f.config, 'utf8'), original);
  f.engine.state.up = true;
}));

test('invalid arguments do not initialize or read personal config', () => fixture(async f => {
  for (const args of [['no-such-command'], ['login'], ['connect', '--key'], ['uninstall', '--force'], ['chats'], ['chats', 'list', '--apply'], ['chats', 'migrate'], ['chats', 'migrate', '--all', 'bad-id']]) {
    assert.equal((await f.run(args)).code, 2, args.join(' '));
  }
  assert.equal((await f.run(['--version'])).stdout.trim(), require('../package.json').version);
  assert.equal(f.api.calls.length, 0);
}));

test('version-one preview connections remain readable, and disconnect cleanly', () => fixture(async f => {
  assert.equal((await f.run()).code, 0);
  const state = JSON.parse(fs.readFileSync(f.state)); state.version = 1; fs.writeFileSync(f.state, JSON.stringify(state));
  assert.equal((await f.run(['status'])).code, 0);
  assert.equal((await f.run(['uninstall'])).code, 0);
  assert.ok(!fs.existsSync(f.state));
}));

function readDevice(f) { return JSON.parse(fs.readFileSync(f.sb.deviceFile, 'utf8')); }
