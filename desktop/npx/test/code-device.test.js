'use strict';

// Device identity, device.json and the uninstall refcount. Shared by both npx
// packages (identical copies). In-process, against the fake API and adapter.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const device = require('../lib/code-device');
const login = require('../lib/code-login');
const engine = require('../lib/code-engine');
const files = require('../lib/files');
const { startCodeApi, startFakeEngine, makeCodeSandbox, MACHINE_ID, ADAPTER, readJson } = require('./code-support');

const VECTOR_ID = '  ABC-123  ';
const VECTOR_HASH = 'fb7d953bd09b6360c7f94e789471ddf1ce6973baefe7b6da392179260b08c323';

async function withEnv(fn) {
  const api = await startCodeApi();
  const engineServer = await startFakeEngine();
  const sb = makeCodeSandbox(api, engineServer);
  const lines = [];
  const ctx = { env: sb.env, out: (line = '') => lines.push(line), err: (line = '') => lines.push(line) };
  const adapter = require(ADAPTER)(sb.env);
  try {
    await fn({ api, engineServer, sb, ctx, lines, adapter, deps: { adapter, sleep: async () => {} } });
  } finally {
    sb.cleanup();
    await engineServer.close();
    await api.close();
  }
}

// Seeds a device on the fake API and writes a matching device.json.
function seedDevice(api, sb, tools) {
  const key = `cs_dev_${crypto.randomBytes(16).toString('hex')}`;
  const deviceHash = device.deviceHashOf(MACHINE_ID);
  api.devices.set(key, { id: 'dev-seed', name: 'This PC', deviceHash, tools, revoked: false });
  const record = {
    version: 1,
    api_url: api.url,
    device_id: 'dev-seed',
    device_key: key,
    device_hash: deviceHash,
    tools,
    created_at: new Date().toISOString(),
  };
  device.writeDevice(sb.env, record);
  return record;
}

test('the device hash matches the spec test vector, and normalizes case and spaces', () => {
  assert.equal(device.deviceHashOf(VECTOR_ID), VECTOR_HASH);
  assert.equal(device.deviceHashOf('abc-123'), VECTOR_HASH);
  assert.equal(device.HASH_PREFIX, 'contextshrink-device-v1\n');
  assert.throws(() => device.deviceHashOf('   '), /could not be read/);
});

test('the machine ID comes from CONTEXTSHRINK_MACHINE_ID when it is set', () => {
  assert.equal(device.machineIdOf({ CONTEXTSHRINK_MACHINE_ID: VECTOR_ID }), VECTOR_ID);
});

test('a device.json that is broken or not in the expected format is refused, not used', () =>
  withEnv(async ({ sb }) => {
    fs.mkdirSync(path.dirname(sb.deviceFile), { recursive: true });
    fs.writeFileSync(sb.deviceFile, '{ not json');
    assert.throws(() => device.readDevice(sb.env), files.FileError);
    fs.writeFileSync(sb.deviceFile, JSON.stringify({ version: 1, device_key: 'nope' }));
    assert.throws(() => device.readDevice(sb.env), /not in the expected format/);
  }));

test('device.json is written with owner-only permissions where the OS allows it', () =>
  withEnv(async ({ api, sb }) => {
    seedDevice(api, sb, ['claude']);
    if (process.platform !== 'win32') {
      assert.equal(fs.statSync(sb.deviceFile).mode & 0o777, 0o600);
    }
    assert.deepEqual(Object.keys(readJson(sb.deviceFile)).sort(), [
      'api_url', 'created_at', 'device_hash', 'device_id', 'device_key', 'tools', 'version',
    ]);
  }));

test('a first connect signs in through the browser and writes device.json with the tool', () =>
  withEnv(async ({ api, sb, ctx, lines, deps }) => {
    const result = await device.ensureDevice(ctx, sb.env, {
      tool: 'claude',
      toolName: 'Claude',
      commandName: 'contextshrink-claude',
      deps,
    });
    assert.deepEqual(result.device.tools, ['claude']);
    assert.equal(result.device.device_hash, device.deviceHashOf(MACHINE_ID));
    assert.equal(readJson(sb.deviceFile).device_key, api.issued[0]);
    assert.ok(lines.some((line) => line.includes('https://code.contextshrink.com/connect?code=ABCD-BFGH')));
    device.applyPlanNote(ctx, sb.env, result.note, 'Claude');
    assert.ok(lines.includes('Free trial: 7 days left'), lines.join('\n'));
    assert.ok(fs.existsSync(sb.licenseFile), 'license.json was not written');
    assert.ok(!lines.join('\n').includes(api.issued[0]), 'the device key was printed');
    assert.ok(
      fs.readFileSync(sb.log, 'utf8').includes('"fn":"openBrowser"'),
      'the browser was not opened through the adapter'
    );
  }));

test('a device.json the server no longer accepts (401) sends the computer through sign-in again', () =>
  withEnv(async ({ api, sb, ctx, lines, deps }) => {
    device.writeDevice(sb.env, {
      version: 1,
      api_url: api.url,
      device_id: 'dev-old',
      device_key: 'cs_dev_stalestalestalestale00',
      device_hash: device.deviceHashOf(MACHINE_ID),
      tools: ['claude'],
      created_at: new Date().toISOString(),
    });
    const result = await device.ensureDevice(ctx, sb.env, { tool: 'claude', toolName: 'Claude', commandName: 'contextshrink-claude', deps });
    assert.ok(lines.some((line) => /sign-in has expired/.test(line)));
    assert.notEqual(result.device.device_key, 'cs_dev_stalestalestalestale00');
    assert.equal(readJson(sb.deviceFile).device_key, api.issued[0]);
    assert.ok(api.calls.some((c) => c.method === 'POST' && c.path === '/code/cli/start'));
  }));

test('a trial that has ended prints the subscribe message and removes license.json', () =>
  withEnv(async ({ api, sb, ctx, lines, deps }) => {
    api.state.plan = 'ended';
    const { note } = await device.ensureDevice(ctx, sb.env, { tool: 'codex', toolName: 'Codex', commandName: 'contextshrink-codex', deps });
    device.applyPlanNote(ctx, sb.env, note, 'Codex');
    assert.ok(
      lines.includes(
        'Your trial has ended. Subscribe for $5/month at https://code.contextshrink.com/account. ' +
          'Until then, Codex works normally without compression.'
      ),
      lines.join('\n')
    );
    assert.ok(!fs.existsSync(sb.licenseFile));
  }));

test('a subscribed account prints Subscribed', () =>
  withEnv(async ({ api, sb, ctx, lines, deps }) => {
    api.state.plan = 'active';
    const { note } = await device.ensureDevice(ctx, sb.env, { tool: 'claude', toolName: 'Claude', commandName: 'contextshrink-claude', deps });
    device.applyPlanNote(ctx, sb.env, note, 'Claude');
    assert.ok(lines.includes('Subscribed'));
  }));

test('the refcount: uninstall of one tool keeps the device and engine; the last tool releases them', () =>
  withEnv(async ({ api, sb, ctx, lines, adapter }) => {
    const record = seedDevice(api, sb, ['claude', 'codex']);
    // Stand-ins for what connect leaves behind: an installed engine and a Startup shortcut.
    fs.mkdirSync(path.join(sb.engineRoot, '9.9.9-test'), { recursive: true });
    fs.writeFileSync(path.join(sb.engineRoot, '9.9.9-test', 'horizon.exe'), 'fake\n');
    fs.mkdirSync(path.dirname(sb.startup), { recursive: true });
    fs.writeFileSync(sb.startup, 'fake\n');
    fs.writeFileSync(sb.licenseFile, '{}\n');

    const released = await device.leaveDevice(ctx, sb.env, { tool: 'claude', deps: { adapter } });
    assert.equal(released, false);
    assert.deepEqual(readJson(sb.deviceFile).tools, ['codex']);
    assert.ok(!api.calls.some((c) => c.method === 'DELETE'), 'the device was released while codex was still connected');
    assert.ok(fs.existsSync(sb.engineExe), 'the engine was deleted while codex was still connected');
    assert.ok(fs.existsSync(sb.startup), 'the Startup shortcut was removed while codex was still connected');
    assert.ok(lines.some((line) => line.includes('Codex still connected on this device')));

    const last = await device.leaveDevice(ctx, sb.env, { tool: 'codex', deps: { adapter } });
    assert.equal(last, true);
    assert.equal(api.devices.get(record.device_key).revoked, true, 'the device was not released on the server');
    assert.ok(!fs.existsSync(sb.deviceFile));
    assert.ok(!fs.existsSync(sb.licenseFile));
    assert.ok(!fs.existsSync(sb.engineRoot), 'the engine was not deleted');
    assert.ok(!fs.existsSync(sb.startup), 'the Startup shortcut was not removed');
    assert.ok(lines.includes('This device is disconnected. You can connect another device now.'));
    assert.ok(fs.readFileSync(sb.log, 'utf8').includes('"code-engine","stop"'), 'the engine was not stopped');
  }));

test('when the server cannot be reached on uninstall, the device is still removed locally and that is said', () =>
  withEnv(async ({ api, sb, ctx, lines, adapter }) => {
    seedDevice(api, sb, ['claude']);
    await api.close();
    // The API is closed: the release call fails, and the local cleanup still runs.
    const cleared = await device.leaveDevice(ctx, sb.env, { tool: 'claude', deps: { adapter } });
    assert.equal(cleared, true);
    assert.ok(!fs.existsSync(sb.deviceFile));
    assert.ok(lines.some((line) => line.startsWith('Could not reach ContextShrink to release this device.')));
  }));

test('reportDevice shows the device, the plan and usage, and never the key', () =>
  withEnv(async ({ api, sb, ctx, lines }) => {
    const seeded = seedDevice(api, sb, ['claude']);
    await device.reportDevice(ctx, sb.env, { toolName: 'Claude' });
    const text = lines.join('\n');
    assert.match(text, /Device: .*\(tools: claude\)/);
    assert.match(text, /Free trial: 7 days left/);
    assert.match(text, /Usage saved today: 22%/);
    assert.match(text, /Engine: running\./);
    assert.ok(!text.includes(seeded.device_key), 'the device key was printed');
  }));

test('applyPlanNote refuses an answer it cannot use, and writes nothing', () =>
  withEnv(async ({ sb, ctx }) => {
    assert.throws(() => device.applyPlanNote(ctx, sb.env, { status: 503, body: null }, 'Claude'), /could not confirm your plan/);
    assert.ok(!fs.existsSync(sb.licenseFile));
  }));

test('the shared login error for device_limit has exit code 3 and names the devices', () =>
  withEnv(async ({ api, sb, ctx }) => {
    api.state.deviceLimit = [{ name: 'Laptop' }, { name: 'Desktop\u0007PC' }];
    await assert.rejects(
      device.ensureDevice(ctx, sb.env, { tool: 'claude', toolName: 'Claude', commandName: 'contextshrink-claude', deps: { adapter: require(ADAPTER)(sb.env), sleep: async () => {} } }),
      (error) => {
        assert.ok(error instanceof login.DeviceLimitError);
        assert.equal(error.exitCode, 3);
        assert.equal(
          error.message,
          'This account already has 2 devices connected: Laptop, Desktop PC.\n' +
            'Disconnect one first: run "npx contextshrink-claude uninstall" on that device, ' +
            'or remove it at https://code.contextshrink.com/account. Then run this command again.'
        );
        return true;
      }
    );
    assert.ok(!fs.existsSync(sb.deviceFile), 'a device.json was written after the limit');
  }));

test('engine helpers: the shipped manifest is either the unpublished placeholder or a pinned release', () => {
  const shipped = engine.loadManifest({});
  if (shipped.published) {
    assert.match(shipped.version, /^\d+\.\d+\.\d+/);
    assert.match(shipped.sha256, /^[0-9a-f]{64}$/);
    assert.ok(shipped.size > 0);
  } else {
    assert.equal(shipped.published, false);
  }
});
