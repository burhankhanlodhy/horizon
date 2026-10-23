'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const { withSandbox, runCli, writeSettingsFile, readSettings, readJson, adapterCalls } = require('./support');
const { MACHINE_ID, ENGINE_VERSION, sha256, ENGINE_ZIP } = require('./code-support');
const { deviceHashOf } = require('../lib/code-device');

const DEVICE_KEY_RE = /cs_dev_[A-Za-z0-9_-]+/;

function assertNoDeviceKeyText(api, texts) {
  for (const key of api.issued) {
    for (const text of texts) assert.ok(!text.includes(key), 'a device key was printed');
  }
  for (const text of texts) assert.ok(!DEVICE_KEY_RE.test(text), 'a device key-shaped value was printed');
}

test('connect signs in, installs the engine, and points Claude Code at it, keeping every other setting', () =>
  withSandbox(async ({ api, engine, sb }) => {
    writeSettingsFile(
      sb,
      [
        '{',
        '    "model": "opus",',
        '    "permissions": { "allow": ["Bash(npm test)"] },',
        '    "env": {',
        '        "FOO": "bar"',
        '    }',
        '}',
        '',
      ].join('\n')
    );

    const res = await runCli(sb, ['connect']);
    assert.strictEqual(res.code, 0, res.output);
    assert.match(res.stdout, /Connected\. Claude Code now sends its requests through the ContextShrink engine/);
    assert.match(res.stdout, /Free trial: 7 days left/);
    assert.match(res.stdout, /Start a new Claude Code session/);

    const settings = readSettings(sb);
    assert.deepStrictEqual(Object.keys(settings), ['model', 'permissions', 'env', 'hooks', 'statusLine']);
    assert.deepStrictEqual(settings.permissions, { allow: ['Bash(npm test)'] });
    assert.strictEqual(settings.model, 'opus');
    assert.deepStrictEqual(settings.env, {
      FOO: 'bar',
      ANTHROPIC_BASE_URL: engine.url,
      ENABLE_TOOL_SEARCH: 'true',
    });
    assert.ok(!('ANTHROPIC_CUSTOM_HEADERS' in settings.env), 'the old account key header is still set');
    assert.deepStrictEqual(settings.hooks, {
      SessionStart: [{ hooks: [{ type: 'command', command: `"${sb.engineExe}" code-engine ensure` }] }],
    });
    assert.ok(settings.statusLine, 'the savings bar was not set');

    const raw = fs.readFileSync(sb.settings, 'utf8');
    assert.ok(raw.startsWith('{\n    "model"'), 'indentation of the file was not kept');
    assert.ok(raw.endsWith('}\n'), 'trailing newline was not kept');

    const device = readJson(sb.deviceFile);
    assert.deepStrictEqual(device.tools, ['claude']);
    assert.strictEqual(device.device_hash, deviceHashOf(MACHINE_ID));
    assert.strictEqual(device.device_key, api.issued[0]);
    assert.strictEqual((fs.statSync(sb.deviceFile).mode & 0o777) === 0o600 || process.platform === 'win32', true);
    assert.ok(fs.existsSync(sb.licenseFile), 'license.json was not written');
    assert.ok(fs.existsSync(sb.engineExe), 'the engine was not installed');
    assert.ok(fs.existsSync(sb.startup), 'the Startup shortcut was not created');

    const calls = adapterCalls(sb).map((c) => c.fn);
    assert.ok(calls.includes('openBrowser'));
    assert.ok(calls.includes('signatureStatus'));
    assert.ok(calls.includes('writeShortcut'));
    assert.ok(fs.readFileSync(sb.log, 'utf8').includes('"code-engine","ensure"'));
    assertNoDeviceKeyText(api, [res.stdout, res.stderr]);
  }));

test('a second connect changes no settings and only refreshes the plan (no new sign-in)', () =>
  withSandbox(async ({ api, sb }) => {
    const first = await runCli(sb, ['connect']);
    assert.strictEqual(first.code, 0, first.output);
    const before = fs.readFileSync(sb.settings, 'utf8');
    const startsBefore = api.calls.filter((c) => c.path === '/code/cli/start').length;

    const second = await runCli(sb, ['connect']);
    assert.strictEqual(second.code, 0, second.output);
    assert.match(second.stdout, /Already connected\. Nothing to change in Claude Code settings\./);
    assert.strictEqual(fs.readFileSync(sb.settings, 'utf8'), before);
    assert.strictEqual(api.calls.filter((c) => c.path === '/code/cli/start').length, startsBefore, 'a second sign-in started');
    assert.ok(api.calls.some((c) => c.path === '/code/entitlement'));
  }));

test('uninstall removes the settings it created, releases the device, and removes the engine and shortcut', () =>
  withSandbox(async ({ api, sb }) => {
    assert.ok(!fs.existsSync(sb.settings));
    const res = await runCli(sb, ['connect']);
    assert.strictEqual(res.code, 0, res.output);
    assert.ok(fs.existsSync(sb.settings));

    const off = await runCli(sb, ['uninstall']);
    assert.strictEqual(off.code, 0, off.output);
    assert.ok(!fs.existsSync(sb.settings), 'settings.json was left behind');
    assert.ok(!fs.existsSync(sb.state), 'state file was left behind');
    assert.ok(!fs.existsSync(sb.deviceFile), 'device.json was left behind');
    assert.ok(!fs.existsSync(sb.licenseFile), 'license.json was left behind');
    assert.ok(!fs.existsSync(sb.engineRoot), 'the engine was left behind');
    assert.ok(!fs.existsSync(sb.startup), 'the Startup shortcut was left behind');
    assert.strictEqual(api.devices.get(api.issued[0]).revoked, true, 'the device was not released');
    assert.match(off.stdout, /This device is disconnected\. You can connect another device now\./);
    assertNoDeviceKeyText(api, [off.stdout, off.stderr]);
  }));

test('disconnect and logout are aliases of uninstall', () =>
  withSandbox(async ({ sb }) => {
    for (const alias of ['disconnect', 'logout']) {
      assert.strictEqual((await runCli(sb, ['connect'])).code, 0);
      const off = await runCli(sb, [alias]);
      assert.strictEqual(off.code, 0, off.output);
      assert.ok(!fs.existsSync(sb.deviceFile), `${alias} did not release the device`);
    }
  }));

test('uninstall restores a value that was there before connect', () =>
  withSandbox(async ({ sb }) => {
    const manual = 'http://127.0.0.1:9/manual-gateway';
    writeSettingsFile(sb, JSON.stringify({ env: { FOO: 'bar', ANTHROPIC_BASE_URL: manual } }, null, 2) + '\n');

    const res = await runCli(sb, ['connect', '--force']);
    assert.strictEqual(res.code, 0, res.output);
    const off = await runCli(sb, ['uninstall']);
    assert.strictEqual(off.code, 0, off.output);
    const settings = readSettings(sb);
    assert.deepStrictEqual(settings, { env: { FOO: 'bar', ANTHROPIC_BASE_URL: manual } });
  }));

test('uninstall leaves a value the user edited alone, and says so', () =>
  withSandbox(async ({ sb }) => {
    const res = await runCli(sb, ['connect']);
    assert.strictEqual(res.code, 0, res.output);
    const settings = readSettings(sb);
    settings.env.ANTHROPIC_BASE_URL = 'https://edited.example/';
    fs.writeFileSync(sb.settings, JSON.stringify(settings, null, 2) + '\n');

    const off = await runCli(sb, ['uninstall']);
    assert.strictEqual(off.code, 0, off.output);
    assert.match(off.stdout, /Left alone because they changed since connect: ANTHROPIC_BASE_URL/);
    const after = readSettings(sb);
    assert.strictEqual(after.env.ANTHROPIC_BASE_URL, 'https://edited.example/');
    assert.ok(!('ENABLE_TOOL_SEARCH' in after.env) || after.env.ENABLE_TOOL_SEARCH === 'true');
  }));

test('a SessionStart hook the user already had is kept on uninstall', () =>
  withSandbox(async ({ sb }) => {
    const mine = { hooks: [{ type: 'command', command: 'echo mine' }] };
    writeSettingsFile(sb, JSON.stringify({ hooks: { SessionStart: [mine] } }, null, 2) + '\n');
    const res = await runCli(sb, ['connect']);
    assert.strictEqual(res.code, 0, res.output);
    assert.strictEqual(readSettings(sb).hooks.SessionStart.length, 2);

    const off = await runCli(sb, ['uninstall']);
    assert.strictEqual(off.code, 0, off.output);
    assert.deepStrictEqual(readSettings(sb), { hooks: { SessionStart: [mine] } });
  }));

test('refuses when the desktop app already manages the connection, before any sign-in', () =>
  withSandbox(async ({ api, sb }) => {
    writeSettingsFile(sb, '{"env":{"FOO":"bar"}}\n');
    fs.writeFileSync(sb.desktopState, '{"version":1}\n');
    const before = fs.readFileSync(sb.settings, 'utf8');

    const res = await runCli(sb, ['connect']);
    assert.strictEqual(res.code, 1);
    assert.match(res.stderr, /desktop app already connects Claude Code/);
    assert.strictEqual(fs.readFileSync(sb.settings, 'utf8'), before);
    assert.strictEqual(api.calls.length, 0, 'the API was called for a refused connect');
  }));

test('refuses another gateway unless --force, and uninstall restores it', () =>
  withSandbox(async ({ api, sb, engine }) => {
    const vachi = 'https://gateway.vachi.example/v1';
    writeSettingsFile(sb, JSON.stringify({ env: { ANTHROPIC_BASE_URL: vachi } }, null, 2) + '\n');
    const before = fs.readFileSync(sb.settings, 'utf8');

    const refused = await runCli(sb, ['connect']);
    assert.strictEqual(refused.code, 1);
    assert.match(refused.stderr, /another gateway/);
    assert.strictEqual(fs.readFileSync(sb.settings, 'utf8'), before);
    assert.strictEqual(api.calls.length, 0);

    const forced = await runCli(sb, ['connect', '--force']);
    assert.strictEqual(forced.code, 0, forced.output);
    assert.strictEqual(readSettings(sb).env.ANTHROPIC_BASE_URL, engine.url);

    const off = await runCli(sb, ['uninstall']);
    assert.strictEqual(off.code, 0, off.output);
    assert.strictEqual(readSettings(sb).env.ANTHROPIC_BASE_URL, vachi);
  }));

test('an existing ANTHROPIC_CUSTOM_HEADERS is refused unless --force; --force removes it and uninstall puts it back', () =>
  withSandbox(async ({ api, sb }) => {
    writeSettingsFile(sb, JSON.stringify({ env: { ANTHROPIC_CUSTOM_HEADERS: 'X-Corp-Trace: 1' } }) + '\n');
    const refused = await runCli(sb, ['connect']);
    assert.strictEqual(refused.code, 1);
    assert.match(refused.stderr, /ANTHROPIC_CUSTOM_HEADERS is already set/);
    assert.strictEqual(api.calls.length, 0);

    const forced = await runCli(sb, ['connect', '--force']);
    assert.strictEqual(forced.code, 0, forced.output);
    assert.ok(!('ANTHROPIC_CUSTOM_HEADERS' in readSettings(sb).env));

    const off = await runCli(sb, ['uninstall']);
    assert.strictEqual(off.code, 0, off.output);
    assert.strictEqual(readSettings(sb).env.ANTHROPIC_CUSTOM_HEADERS, 'X-Corp-Trace: 1');
  }));

test('invalid JSON is refused and the file is never overwritten', () =>
  withSandbox(async ({ api, sb }) => {
    const broken = '{ "env": ';
    writeSettingsFile(sb, broken);
    const res = await runCli(sb, ['connect']);
    assert.strictEqual(res.code, 1);
    assert.match(res.stderr, /not valid JSON/);
    assert.strictEqual(fs.readFileSync(sb.settings, 'utf8'), broken);
    assert.strictEqual(api.calls.length, 0);
    assert.ok(!fs.existsSync(sb.state));
  }));

test('an empty settings file is treated as an empty object', () =>
  withSandbox(async ({ sb }) => {
    writeSettingsFile(sb, '');
    const res = await runCli(sb, ['connect']);
    assert.strictEqual(res.code, 0, res.output);
    const off = await runCli(sb, ['uninstall']);
    assert.strictEqual(off.code, 0, off.output);
    // The file existed (empty) before connect, so it stays, as an empty object.
    assert.deepStrictEqual(readSettings(sb), {});
  }));

test('a device limit exits with code 3 and names the two devices, with no settings changed', () =>
  withSandbox(async ({ api, sb }) => {
    api.state.deviceLimit = [{ name: 'Office PC' }, { name: 'Home laptop' }];
    const res = await runCli(sb, ['connect']);
    assert.strictEqual(res.code, 3, res.output);
    assert.strictEqual(
      res.stderr,
      'This account already has 2 devices connected: Office PC, Home laptop.\n' +
        'Disconnect one first: run "npx contextshrink-claude uninstall" on that device, ' +
        'or remove it at https://code.contextshrink.com/account. Then run this command again.\n'
    );
    assert.ok(!fs.existsSync(sb.settings));
    assert.ok(!fs.existsSync(sb.deviceFile));
    assert.ok(!fs.existsSync(sb.engineRoot), 'the engine was installed past the device limit');
  }));

test('a device.json the server no longer accepts sends the computer through sign-in again', () =>
  withSandbox(async ({ api, sb, engine }) => {
    fs.mkdirSync(path.dirname(sb.deviceFile), { recursive: true });
    fs.writeFileSync(
      sb.deviceFile,
      JSON.stringify({
        version: 1,
        api_url: api.url,
        device_id: 'dev-stale',
        device_key: 'cs_dev_staleStaleStaleStale00',
        device_hash: deviceHashOf(MACHINE_ID),
        tools: ['claude'],
        created_at: new Date().toISOString(),
      })
    );
    const res = await runCli(sb, ['connect']);
    assert.strictEqual(res.code, 0, res.output);
    assert.match(res.stdout, /sign-in has expired\. Signing in again\./);
    assert.strictEqual(readJson(sb.deviceFile).device_key, api.issued[0]);
    assert.strictEqual(readSettings(sb).env.ANTHROPIC_BASE_URL, engine.url);
  }));

test('a trial that has ended still connects the engine, and says Claude works without compression', () =>
  withSandbox(async ({ api, sb, engine }) => {
    api.state.plan = 'ended';
    const res = await runCli(sb, ['connect']);
    assert.strictEqual(res.code, 0, res.output);
    assert.ok(
      res.stdout.includes(
        'Your trial has ended. Subscribe for $5/month at https://code.contextshrink.com/account. ' +
          'Until then, Claude works normally without compression.'
      ),
      res.stdout
    );
    assert.ok(!fs.existsSync(sb.licenseFile), 'an expired note was kept');
    assert.strictEqual(readSettings(sb).env.ANTHROPIC_BASE_URL, engine.url);
  }));

test('the engine that does not start leaves Claude Code settings untouched', () =>
  withSandbox(async ({ sb, engine }) => {
    engine.state.up = false;
    const res = await runCli(sb, ['connect'], { env: { CONTEXTSHRINK_NPX_STUB_ENGINE: 'fail' } });
    assert.strictEqual(res.code, 1, res.output);
    assert.match(res.stderr, /The ContextShrink engine did not start\. Nothing was changed/);
    assert.ok(!fs.existsSync(sb.settings), 'settings were written although the engine did not start');
    engine.state.up = true;
  }));

test('on a system other than Windows connect exits 1 with the spec message and calls nothing', () =>
  withSandbox(async ({ api, sb }) => {
    const res = await runCli(sb, ['connect'], { env: { CONTEXTSHRINK_NPX_TEST_PLATFORM: 'linux' } });
    assert.strictEqual(res.code, 1);
    assert.match(res.stderr, /ContextShrink Code runs on Windows today\. macOS and Linux are coming\./);
    assert.strictEqual(api.calls.length, 0);
  }));

test('an engine manifest with no url says the engine is not published yet, and nothing is changed', () =>
  withSandbox(
    async ({ api, sb }) => {
      const res = await runCli(sb, ['connect']);
      assert.strictEqual(res.code, 1, res.output);
      assert.match(res.stderr, /Engine not published yet\. Nothing was changed\./);
      assert.strictEqual(api.calls.length, 0);
      assert.ok(!fs.existsSync(sb.settings));
    },
    { manifest: { version: '0.0.0', url: '', sha256: '', size: 0 } }
  ));

test('an engine download that does not match the manifest is refused and nothing is changed', () =>
  withSandbox(
    async ({ api, sb }) => {
      const res = await runCli(sb, ['connect']);
      assert.strictEqual(res.code, 1, res.output);
      assert.match(res.stderr, /did not match the release manifest/);
      assert.ok(!fs.existsSync(sb.settings));
      assert.ok(!fs.existsSync(path.join(sb.engineRoot, ENGINE_VERSION)));
      assert.ok(api.calls.some((c) => c.path === '/engine.zip'));
    },
    {
      // The size in the manifest is one byte more than the served engine.
      manifest: (api) => ({
        version: ENGINE_VERSION,
        url: `${api.url}/engine.zip`,
        sha256: sha256(ENGINE_ZIP),
        size: ENGINE_ZIP.length + 1,
      }),
    }
  ));

test('an older 0.2.x connection is taken over: the account header is removed and the old key record retired', () =>
  withSandbox(async ({ api, sb, engine }) => {
    const oldBase = 'https://proxy.contextshrink.com/k/0123456789abcdef';
    const oldHeader = 'X-Horizon-Proxy-Token: cs_live_OLDKEY0123456789abcdefghijk';
    writeSettingsFile(
      sb,
      JSON.stringify({ env: { FOO: 'bar', ANTHROPIC_BASE_URL: oldBase, ANTHROPIC_CUSTOM_HEADERS: oldHeader, ENABLE_TOOL_SEARCH: 'true' } }, null, 2) + '\n'
    );
    fs.writeFileSync(
      sb.state,
      JSON.stringify({
        version: 1,
        settings_existed: true,
        env_existed: true,
        previous: {
          ANTHROPIC_BASE_URL: { present: false },
          ANTHROPIC_CUSTOM_HEADERS: { present: false },
          ENABLE_TOOL_SEARCH: { present: false },
        },
        written: { ANTHROPIC_BASE_URL: oldBase, ANTHROPIC_CUSTOM_HEADERS: oldHeader, ENABLE_TOOL_SEARCH: 'true' },
        proxy_host: 'proxy.contextshrink.com',
        connected_at: new Date().toISOString(),
      }, null, 2) + '\n'
    );
    fs.mkdirSync(path.dirname(sb.legacyHome), { recursive: true });
    fs.writeFileSync(sb.legacyHome, JSON.stringify({ version: 1, keepalive_id: '0123456789abcdef', device_key_id: 'old-key-id' }));

    const res = await runCli(sb, ['connect']);
    assert.strictEqual(res.code, 0, res.output);
    assert.match(res.stdout, /The account key from the earlier version is no longer used/);
    const env = readSettings(sb).env;
    assert.deepStrictEqual(env, { FOO: 'bar', ANTHROPIC_BASE_URL: engine.url, ENABLE_TOOL_SEARCH: 'true' });
    assert.ok(!fs.existsSync(sb.legacyHome), 'the old record was kept');
    assert.strictEqual(readJson(sb.state).version, 2);

    const off = await runCli(sb, ['uninstall']);
    assert.strictEqual(off.code, 0, off.output);
    // The old connect wrote all three keys, so uninstall removes them again.
    assert.deepStrictEqual(readSettings(sb).env, { FOO: 'bar' });
    assert.ok(api.calls.some((c) => c.path === '/code/cli/start'), 'the takeover did not sign the computer in');
  }));

test('status shows the engine, the device, the plan and usage, and never the key', () =>
  withSandbox(async ({ api, sb, engine }) => {
    const before = await runCli(sb, ['status']);
    assert.strictEqual(before.code, 0, before.output);
    assert.match(before.stdout, /Not connected\./);
    assert.match(before.stdout, /Device: not connected\./);

    const res = await runCli(sb, ['connect']);
    assert.strictEqual(res.code, 0, res.output);
    const status = await runCli(sb, ['status']);
    assert.strictEqual(status.code, 0, status.output);
    assert.match(status.stdout, /Connected through the ContextShrink engine at 127\.0\.0\.1:\d+\./);
    assert.match(status.stdout, /Tool search: on/);
    assert.match(status.stdout, /Device: .* \(tools: claude\)/);
    assert.match(status.stdout, /Free trial: 7 days left/);
    assert.match(status.stdout, /Usage saved today: 22%/);
    assert.match(status.stdout, /Engine: running\./);
    assertNoDeviceKeyText(api, [status.stdout, status.stderr]);
  }));

test('the command line rejects what 0.3.0 removed, and unknown commands, with exit code 2', () =>
  withSandbox(async ({ sb }) => {
    assert.strictEqual((await runCli(sb, ['login'])).code, 2);
    assert.strictEqual((await runCli(sb, ['connect', '--key'])).code, 2);
    assert.strictEqual((await runCli(sb, ['bogus'])).code, 2);
    assert.strictEqual((await runCli(sb, ['status', '--nope'])).code, 2);
    assert.strictEqual((await runCli(sb, ['uninstall', '--force'])).code, 2);
    assert.strictEqual((await runCli(sb, ['--help'])).code, 0);
    assert.strictEqual((await runCli(sb, ['--version'])).code, 0);
  }));

test('the lib directory keeps no absolute user paths', () => {
  const root = path.join(__dirname, '..');
  for (const file of [
    'bin/contextshrink.js',
    'lib/cli.js',
    'lib/config.js',
    'lib/connect.js',
    'lib/files.js',
    'lib/api.js',
    'lib/codetab.js',
    'lib/code-env.js',
    'lib/code-login.js',
    'lib/code-engine.js',
    'lib/code-device.js',
    'lib/engine-manifest.json',
    'lib/codetab-manifest.json',
  ]) {
    const text = fs.readFileSync(path.join(root, file), 'utf8');
    assert.ok(!/C:\\Users\\|\/Users\/|\/home\/[a-z]/i.test(text), `${file} contains a personal path`);
  }
});
