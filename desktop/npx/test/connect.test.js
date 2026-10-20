'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const { EMAIL, PASSWORD, startFakeApi, makeSandbox, runCli, readJson, writeSettingsFile } = require('./support');

const SIGN_IN = `${EMAIL}\n${PASSWORD}\n`;
const BASE_RE = /^http:\/\/127\.0\.0\.1:9\/k\/[0-9a-f]{16}$/;
const HEADER_RE = /^X-Horizon-Proxy-Token: cs_live_\S+$/;

// Runs `fn` with a fresh fake API and sandbox, and always cleans both up.
async function withSandbox(fn, options) {
  const api = await startFakeApi();
  const sb = makeSandbox(api, options);
  try {
    await fn({ api, sb });
  } finally {
    sb.cleanup();
    await api.close();
  }
}

function assertNoKeyLeak(api, outputs) {
  for (const key of api.issued) {
    for (const text of outputs) {
      assert.ok(!text.includes(key), 'an account key was printed');
    }
  }
}

test('connect writes exactly the two keys and keeps every other setting', () =>
  withSandbox(async ({ api, sb }) => {
    const original = [
      '{',
      '    "model": "opus",',
      '    "permissions": { "allow": ["Bash(npm test)"] },',
      '    "env": {',
      '        "FOO": "bar"',
      '    }',
      '}',
      '',
    ].join('\n');
    writeSettingsFile(sb, original);

    const res = await runCli(sb, [], { input: SIGN_IN });
    assert.strictEqual(res.code, 0, res.output);

    const settings = readJson(sb.settings);
    assert.deepStrictEqual(Object.keys(settings), ['model', 'permissions', 'env']);
    assert.deepStrictEqual(settings.permissions, { allow: ['Bash(npm test)'] });
    assert.strictEqual(settings.model, 'opus');
    assert.deepStrictEqual(Object.keys(settings.env), ['FOO', 'ANTHROPIC_BASE_URL', 'ANTHROPIC_CUSTOM_HEADERS']);
    assert.strictEqual(settings.env.FOO, 'bar');
    assert.match(settings.env.ANTHROPIC_BASE_URL, BASE_RE);
    assert.match(settings.env.ANTHROPIC_CUSTOM_HEADERS, HEADER_RE);
    assert.strictEqual(Object.keys(settings.env).length, 3);

    const raw = fs.readFileSync(sb.settings, 'utf8');
    assert.ok(raw.startsWith('{\n    "model"'), 'indentation of the file was not kept');
    assert.ok(raw.endsWith('}\n'), 'trailing newline was not kept');

    assert.deepStrictEqual(
      api.calls.map((c) => `${c.method} ${c.path}`),
      ['POST /auth/login', 'POST /keys', 'POST /auth/logout']
    );
    assertNoKeyLeak(api, [res.stdout, res.stderr]);
  }));

test('a second connect changes nothing and signs in again only once', () =>
  withSandbox(async ({ api, sb }) => {
    const first = await runCli(sb, [], { input: SIGN_IN });
    assert.strictEqual(first.code, 0, first.output);
    const before = fs.readFileSync(sb.settings, 'utf8');
    const callsBefore = api.calls.length;

    const second = await runCli(sb, [], { input: '' });
    assert.strictEqual(second.code, 0, second.output);
    assert.match(second.stdout, /Already connected/);
    assert.strictEqual(fs.readFileSync(sb.settings, 'utf8'), before);
    assert.strictEqual(api.calls.length, callsBefore, 'a second connect called the API');
  }));

test('disconnect removes the settings it created when the file did not exist', () =>
  withSandbox(async ({ api, sb }) => {
    assert.ok(!fs.existsSync(sb.settings));
    const res = await runCli(sb, [], { input: SIGN_IN });
    assert.strictEqual(res.code, 0, res.output);
    assert.ok(fs.existsSync(sb.settings));

    const off = await runCli(sb, ['disconnect']);
    assert.strictEqual(off.code, 0, off.output);
    assert.ok(!fs.existsSync(sb.settings), 'settings.json was left behind');
    assert.ok(!fs.existsSync(sb.state), 'state file was left behind');
    assertNoKeyLeak(api, [off.stdout, off.stderr]);
  }));

test('disconnect restores a value that was there before connect', () =>
  withSandbox(async ({ sb, api }) => {
    const manual = `http://127.0.0.1:9/k/manualid01`;
    writeSettingsFile(sb, JSON.stringify({ env: { FOO: 'bar', ANTHROPIC_BASE_URL: manual } }, null, 2) + '\n');

    const res = await runCli(sb, [], { input: SIGN_IN });
    assert.strictEqual(res.code, 0, res.output);
    assert.notStrictEqual(readJson(sb.settings).env.ANTHROPIC_BASE_URL, manual);

    const off = await runCli(sb, ['disconnect']);
    assert.strictEqual(off.code, 0, off.output);
    const settings = readJson(sb.settings);
    assert.deepStrictEqual(settings, { env: { FOO: 'bar', ANTHROPIC_BASE_URL: manual } });
    assertNoKeyLeak(api, [off.stdout, off.stderr]);
  }));

test('disconnect leaves a value the user edited alone and says so', () =>
  withSandbox(async ({ sb }) => {
    const res = await runCli(sb, [], { input: SIGN_IN });
    assert.strictEqual(res.code, 0, res.output);

    const settings = readJson(sb.settings);
    settings.env.ANTHROPIC_BASE_URL = 'https://edited.example/';
    fs.writeFileSync(sb.settings, JSON.stringify(settings, null, 2) + '\n');

    const off = await runCli(sb, ['disconnect']);
    assert.strictEqual(off.code, 0, off.output);
    assert.match(off.stdout, /Left alone because they changed since connect: ANTHROPIC_BASE_URL/);
    const after = readJson(sb.settings);
    assert.strictEqual(after.env.ANTHROPIC_BASE_URL, 'https://edited.example/');
    assert.ok(!('ANTHROPIC_CUSTOM_HEADERS' in after.env), 'our unedited header was not removed');
  }));

test('refuses when the desktop app already manages the connection', () =>
  withSandbox(async ({ api, sb }) => {
    writeSettingsFile(sb, '{"env":{"FOO":"bar"}}\n');
    fs.writeFileSync(sb.desktopState, '{"version":1}\n');
    const before = fs.readFileSync(sb.settings, 'utf8');

    const res = await runCli(sb, [], { input: SIGN_IN });
    assert.strictEqual(res.code, 1);
    assert.match(res.stderr, /desktop app already connects/);
    assert.strictEqual(fs.readFileSync(sb.settings, 'utf8'), before);
    assert.ok(!fs.existsSync(sb.state));
    assert.strictEqual(api.calls.length, 0, 'the API was called for a refused connect');
  }));

test('refuses another gateway unless --force, and disconnect restores it', () =>
  withSandbox(async ({ api, sb }) => {
    const vachi = 'https://gateway.vachi.example/v1';
    writeSettingsFile(sb, JSON.stringify({ env: { ANTHROPIC_BASE_URL: vachi } }, null, 2) + '\n');
    const before = fs.readFileSync(sb.settings, 'utf8');

    const refused = await runCli(sb, [], { input: SIGN_IN });
    assert.strictEqual(refused.code, 1);
    assert.match(refused.stderr, /another gateway/);
    assert.strictEqual(fs.readFileSync(sb.settings, 'utf8'), before);
    assert.strictEqual(api.calls.length, 0);

    const forced = await runCli(sb, ['connect', '--force'], { input: SIGN_IN });
    assert.strictEqual(forced.code, 0, forced.output);
    assert.match(readJson(sb.settings).env.ANTHROPIC_BASE_URL, BASE_RE);

    const off = await runCli(sb, ['disconnect']);
    assert.strictEqual(off.code, 0, off.output);
    assert.strictEqual(readJson(sb.settings).env.ANTHROPIC_BASE_URL, vachi);
  }));

test('refuses existing custom headers unless --force', () =>
  withSandbox(async ({ api, sb }) => {
    writeSettingsFile(sb, JSON.stringify({ env: { ANTHROPIC_CUSTOM_HEADERS: 'X-Corp-Trace: 1' } }) + '\n');
    const refused = await runCli(sb, [], { input: SIGN_IN });
    assert.strictEqual(refused.code, 1);
    assert.match(refused.stderr, /ANTHROPIC_CUSTOM_HEADERS is already set/);
    assert.strictEqual(api.calls.length, 0);

    const forced = await runCli(sb, ['--force'], { input: SIGN_IN });
    assert.strictEqual(forced.code, 0, forced.output);
    const off = await runCli(sb, ['disconnect']);
    assert.strictEqual(off.code, 0, off.output);
    assert.strictEqual(readJson(sb.settings).env.ANTHROPIC_CUSTOM_HEADERS, 'X-Corp-Trace: 1');
  }));

test('invalid JSON is refused and the file is never overwritten', () =>
  withSandbox(async ({ api, sb }) => {
    const broken = '{ "env": ';
    writeSettingsFile(sb, broken);
    const res = await runCli(sb, [], { input: SIGN_IN });
    assert.strictEqual(res.code, 1);
    assert.match(res.stderr, /not valid JSON/);
    assert.strictEqual(fs.readFileSync(sb.settings, 'utf8'), broken);
    assert.strictEqual(api.calls.length, 0);
    assert.ok(!fs.existsSync(sb.state));
  }));

test('an empty settings file is treated as an empty object', () =>
  withSandbox(async ({ sb }) => {
    writeSettingsFile(sb, '');
    const res = await runCli(sb, [], { input: SIGN_IN });
    assert.strictEqual(res.code, 0, res.output);
    assert.match(readJson(sb.settings).env.ANTHROPIC_BASE_URL, BASE_RE);

    const off = await runCli(sb, ['disconnect']);
    assert.strictEqual(off.code, 0, off.output);
    assert.deepStrictEqual(readJson(sb.settings), {});
  }));

test('a wrong password changes nothing', () =>
  withSandbox(async ({ sb }) => {
    const res = await runCli(sb, [], { input: `${EMAIL}\nnot-the-password\n` });
    assert.strictEqual(res.code, 1);
    assert.match(res.stderr, /Incorrect email or password/);
    assert.ok(!fs.existsSync(sb.settings));
    assert.ok(!fs.existsSync(sb.state));
    assert.ok(!fs.existsSync(sb.homeFile));
  }));

test('status shows the host and the keep-alive prefix, never the key', () =>
  withSandbox(async ({ api, sb }) => {
    const before = await runCli(sb, ['status']);
    assert.strictEqual(before.code, 0);
    assert.match(before.stdout, /Not connected/);

    const res = await runCli(sb, [], { input: SIGN_IN });
    assert.strictEqual(res.code, 0, res.output);
    const status = await runCli(sb, ['status']);
    assert.strictEqual(status.code, 0, status.output);
    assert.match(status.stdout, /Connected to 127\.0\.0\.1:9 \(keep-alive id [0-9a-f]{4}\.\.\.\)/);
    assertNoKeyLeak(api, [status.stdout, status.stderr]);
  }));

test('logout disconnects, revokes the key and removes the saved state', () =>
  withSandbox(async ({ api, sb }) => {
    const res = await runCli(sb, [], { input: SIGN_IN });
    assert.strictEqual(res.code, 0, res.output);
    assert.strictEqual(api.live.size, 1);
    assert.ok(fs.existsSync(sb.homeFile));

    const out = await runCli(sb, ['logout'], { input: SIGN_IN });
    assert.strictEqual(out.code, 0, out.output);
    assert.ok(!fs.existsSync(sb.settings), 'settings were not restored');
    assert.strictEqual(api.live.size, 0, 'the key was not revoked');
    assert.ok(!fs.existsSync(sb.homeFile), 'the saved state was not removed');
    assertNoKeyLeak(api, [out.stdout, out.stderr]);
  }));

test('login --key stores a pasted key without calling the API', () =>
  withSandbox(async ({ api, sb }) => {
    const pasted = 'cs_live_PASTEDKEY0123456789abcdefghijk';
    const res = await runCli(sb, ['login', '--key'], { input: `${pasted}\n` });
    assert.strictEqual(res.code, 0, res.output);
    assert.strictEqual(api.calls.length, 0);
    assert.strictEqual(readJson(sb.settings).env.ANTHROPIC_CUSTOM_HEADERS, `X-Horizon-Proxy-Token: ${pasted}`);
    assert.ok(!res.output.includes(pasted), 'the pasted key was printed');

    const bad = await runCli(sb, ['login', '--key', '--force'], { input: 'not-a-key\n' });
    assert.strictEqual(bad.code, 1);
    assert.match(bad.stderr, /not a ContextShrink account key/);
  }));

test('login mints a new key and revokes the one connect made', () =>
  withSandbox(async ({ api, sb }) => {
    const first = await runCli(sb, [], { input: SIGN_IN });
    assert.strictEqual(first.code, 0, first.output);
    const oldHeader = readJson(sb.settings).env.ANTHROPIC_CUSTOM_HEADERS;

    const again = await runCli(sb, ['login'], { input: SIGN_IN });
    assert.strictEqual(again.code, 0, again.output);
    const newHeader = readJson(sb.settings).env.ANTHROPIC_CUSTOM_HEADERS;
    assert.notStrictEqual(newHeader, oldHeader);
    assert.strictEqual(api.live.size, 1, 'the old key was not revoked');
    assertNoKeyLeak(api, [again.stdout, again.stderr]);
  }));

test('unknown commands and options exit with code 2', () =>
  withSandbox(async ({ sb }) => {
    assert.strictEqual((await runCli(sb, ['bogus'])).code, 2);
    assert.strictEqual((await runCli(sb, ['status', '--nope'])).code, 2);
    assert.strictEqual((await runCli(sb, ['--help'])).code, 0);
  }));

test('the lib directory keeps no absolute user paths', () => {
  const root = path.join(__dirname, '..');
  for (const file of ['bin/contextshrink.js', 'lib/cli.js', 'lib/config.js', 'lib/connect.js', 'lib/files.js', 'lib/api.js', 'lib/prompt.js']) {
    const text = fs.readFileSync(path.join(root, file), 'utf8');
    assert.ok(!/C:\\Users\\|\/Users\/|\/home\/[a-z]/i.test(text), `${file} contains a personal path`);
  }
});
