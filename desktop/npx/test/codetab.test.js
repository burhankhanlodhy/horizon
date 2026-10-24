'use strict';

// Code tab (Windows) tests. Everything is local: a fake control plane, a fake releases
// server on 127.0.0.1, a fake Claude Desktop folder in a temp LOCALAPPDATA, and the
// stub helper in test/fixtures. The overrides below are honoured only with
// CONTEXTSHRINK_NPX_TEST=1, and no test touches the real Claude Desktop folders.

const test = require('node:test');
const assert = require('node:assert');
const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { runCli, readJson } = require('./support');
const code = require('./code-support');
const { signatureProblem, configFor } = require('../lib/codetab');

const STUB = path.join(__dirname, 'fixtures', 'fake-codetab-helper.js');
const SHIPPED_MANIFEST = path.join(__dirname, '..', 'lib', 'codetab-manifest.json');
const VERSION = '0.9.0-test';
const LAUNCHER_BYTES = Buffer.from('test launcher bytes\n');
const HELPER_BYTES = Buffer.from('test helper bytes\n');
const TAMPERED_LAUNCHER = Buffer.from(LAUNCHER_BYTES.toString().toUpperCase());
const BASE_RE = /^http:\/\/127\.0\.0\.1:\d+$/;
const KEY_RE = /cs_dev_[A-Za-z0-9_-]+/;
// A desktop-app codetab.json that is switched off. Its 4-space indent and CRLF line
// ending make a restore that only re-serialises the JSON fail the byte-for-byte checks.
const DESKTOP_OFF_BYTES = Buffer.from(
  JSON.stringify(
    { v: 1, enabled: false, base_url: 'http://127.0.0.1:18788/k/desktopid', health_port: 18788, env: {} },
    null,
    4
  ) + '\r\n'
);
const PLACEHOLDER = {
  version: '0.0.0-unset',
  files: {
    'claude-code-launcher.exe': { sha256: '0'.repeat(64), size: 0 },
    'contextshrink-codetab.exe': { sha256: '0'.repeat(64), size: 0 },
  },
};

function sha256(bytes) {
  return crypto.createHash('sha256').update(bytes).digest('hex');
}

function manifestFor(launcher, helper) {
  return {
    version: VERSION,
    files: {
      'claude-code-launcher.exe': { sha256: sha256(launcher), size: launcher.length },
      'contextshrink-codetab.exe': { sha256: sha256(helper), size: helper.length },
    },
  };
}

function servedFiles(launcher = LAUNCHER_BYTES, helper = HELPER_BYTES) {
  return {
    [`/claude-code-tools/${VERSION}/claude-code-launcher.exe`]: launcher,
    [`/claude-code-tools/${VERSION}/contextshrink-codetab.exe`]: helper,
  };
}

// A fake releases server that records every path it is asked for.
function startReleases(served) {
  const hits = [];
  const server = http.createServer((req, res) => {
    hits.push(req.url);
    const body = served[req.url];
    if (!body) {
      res.writeHead(404);
      res.end();
      return;
    }
    res.writeHead(200, { 'content-type': 'application/octet-stream' });
    res.end(body);
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () =>
      resolve({
        url: `http://127.0.0.1:${server.address().port}`,
        hits,
        close: () => new Promise((done) => server.close(done)),
      })
    );
  });
}

// A sandbox with a fake Claude Desktop folder, a releases server and the stub helper.
async function withCodeTab(fn, { served = servedFiles(), manifest, platform = 'win32', signature = false } = {}) {
  const api = await code.startCodeApi();
  const engine = await code.startFakeEngine();
  const releases = await startReleases(served);
  const sb = code.makeCodeSandbox(api, engine);
  sb.configDir = path.join(sb.home, '.claude');
  fs.mkdirSync(sb.configDir, { recursive: true });
  sb.env.CLAUDE_CONFIG_DIR = sb.configDir;
  sb.settings = path.join(sb.configDir, 'settings.json');
  sb.dataDir = path.join(sb.root, 'ContextShrinkData');
  sb.env.CONTEXTSHRINK_DATA_DIR = sb.dataDir;
  const log = path.join(sb.root, 'helper-log.jsonl');
  const manifestFile = path.join(sb.root, 'manifest.json');
  fs.writeFileSync(manifestFile, JSON.stringify(manifest || manifestFor(LAUNCHER_BYTES, HELPER_BYTES)));

  const desktopRoot = path.join(
    sb.localAppData,
    'Packages',
    'Claude_test',
    'LocalCache',
    'Roaming',
    'Claude',
    'claude-code',
    '1.0.0',
    'abc123'
  );
  fs.mkdirSync(desktopRoot, { recursive: true });
  const claudeExe = path.join(desktopRoot, 'claude.exe');
  fs.writeFileSync(claudeExe, 'real claude bytes');

  Object.assign(sb.env, {
    CONTEXTSHRINK_NPX_TEST: '1',
    CONTEXTSHRINK_NPX_TEST_PLATFORM: platform,
    CONTEXTSHRINK_NPX_CODETAB_MANIFEST: manifestFile,
    CONTEXTSHRINK_NPX_CODETAB_HELPER: STUB,
    CONTEXTSHRINK_RELEASES_URL: releases.url,
    CONTEXTSHRINK_NPX_STUB_LOG: log,
  });
  if (!signature) sb.env.CONTEXTSHRINK_NPX_TEST_SKIP_SIGNATURE = '1';

  const codeTabDir = path.join(sb.dataDir, 'claude-code');
  const bin = path.join(codeTabDir, 'npx-bin');
  const paths = {
    dir: codeTabDir,
    config: path.join(codeTabDir, 'codetab.json'),
    takeover: path.join(codeTabDir, 'npx-takeover.json'),
    stamp: path.join(codeTabDir, 'npx-repair.stamp'),
    bin,
    launcher: path.join(bin, 'claude-code-launcher.exe'),
    helper: path.join(bin, 'contextshrink-codetab.exe'),
  };
  try {
    await fn({ sb, api, engine, releases, log, claudeExe, paths });
  } finally {
    sb.cleanup();
    await releases.close();
    await engine.close();
    await api.close();
  }
}

// Each call the stub helper received, with whether codetab.json existed at that moment.
function stubCalls(log) {
  if (!fs.existsSync(log)) return [];
  return fs
    .readFileSync(log, 'utf8')
    .split('\n')
    .filter(Boolean)
    .map((line) => JSON.parse(line));
}

function commands(log) {
  return stubCalls(log).map((call) => call.args[0]);
}

function assertNoKeyText(texts) {
  for (const text of texts) {
    assert.ok(!KEY_RE.test(text), 'an account key was printed');
  }
}

test('connect sets up the Code tab: patches Claude Desktop and writes codetab.json owned by npx', () =>
  withCodeTab(async ({ sb, engine, log, paths, releases, claudeExe }) => {
    const res = await runCli(sb, []);
    assert.strictEqual(res.code, 0, res.output);
    assert.match(
      res.stdout,
      /Code tab: patched 1 Claude Code version\(s\); it stays patched after Claude Desktop updates\./
    );

    const env = readJson(sb.settings).env;
    assert.strictEqual(env.ANTHROPIC_BASE_URL, engine.url);
    // The forwarder format: v 1 and enabled (the launcher routes only then), owner npx,
    // no launch mode, no account key.
    assert.deepStrictEqual(readJson(paths.config), {
      v: 1,
      owner: 'npx',
      enabled: true,
      base_url: engine.url,
      health_port: Number(new URL(engine.url).port),
      env: { ENABLE_TOOL_SEARCH: 'true' },
    });

    assert.ok(fs.readFileSync(paths.launcher).equals(LAUNCHER_BYTES));
    assert.ok(fs.readFileSync(paths.helper).equals(HELPER_BYTES));
    assert.deepStrictEqual(fs.readdirSync(paths.bin).sort(), ['claude-code-launcher.exe', 'contextshrink-codetab.exe']);

    assert.deepStrictEqual(stubCalls(log), [
      { args: ['install', '--json', '--launcher', paths.launcher], configExists: true },
      { args: ['autostart', 'on'], configExists: true },
    ]);
    assert.deepStrictEqual(releases.hits, [
      `/claude-code-tools/${VERSION}/claude-code-launcher.exe`,
      `/claude-code-tools/${VERSION}/contextshrink-codetab.exe`,
    ]);
    assert.strictEqual(fs.readFileSync(claudeExe, 'utf8'), 'real claude bytes', 'Claude Desktop was touched by the test');
    assertNoKeyText([res.stdout, res.stderr]);
  }));

test('a second connect reuses the downloaded files and runs the helper again', () =>
  withCodeTab(async ({ sb, log, releases }) => {
    const first = await runCli(sb, []);
    assert.strictEqual(first.code, 0, first.output);
    const hitsBefore = releases.hits.length;

    const second = await runCli(sb, [], { input: '' });
    assert.strictEqual(second.code, 0, second.output);
    assert.match(second.stdout, /Already connected/);
    assert.strictEqual(releases.hits.length, hitsBefore, 'the files were downloaded again');
    assert.deepStrictEqual(commands(log), ['install', 'autostart', 'install', 'autostart']);
  }));

test('a placeholder manifest sets nothing up and says the Code tab is not released yet', () =>
  withCodeTab(
    async ({ sb, paths, releases, log }) => {
      const res = await runCli(sb, []);
      assert.strictEqual(res.code, 0, res.output);
      assert.match(res.stdout, /Code tab support is not released yet; Code tab not set up\./);
      assert.ok(!fs.existsSync(paths.config));
      assert.deepStrictEqual(releases.hits, []);
      assert.deepStrictEqual(stubCalls(log), []);
      assert.match(readJson(sb.settings).env.ANTHROPIC_BASE_URL, BASE_RE, 'the terminal part did not connect');
    },
    { manifest: PLACEHOLDER }
  ));

test('the shipped manifest is either the unreleased placeholder or a pinned release', () => {
  const manifest = readJson(SHIPPED_MANIFEST);
  const names = ['claude-code-launcher.exe', 'contextshrink-codetab.exe'];
  if (manifest.version === '0.0.0-unset') {
    for (const name of names) assert.strictEqual(manifest.files[name].sha256, '0'.repeat(64));
    return;
  }
  assert.match(manifest.version, /^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$/);
  for (const name of names) {
    assert.match(manifest.files[name].sha256, /^[0-9a-f]{64}$/);
    assert.ok(manifest.files[name].size > 0);
  }
});

test('a failed startup shortcut keeps the patch and its config, and says updates will not be patched', () =>
  withCodeTab(async ({ sb, paths }) => {
    sb.env.CONTEXTSHRINK_NPX_STUB_AUTOSTART_FAIL = 'on';
    const res = await runCli(sb, []);
    assert.strictEqual(res.code, 1, res.output);
    assert.match(res.stderr, /startup shortcut could not be added/);
    assert.doesNotMatch(res.stdout, /stays patched/, 'a failed shortcut was reported as success');
    assert.ok(fs.existsSync(paths.config), 'the config was removed while folders were patched');
    assert.ok(fs.existsSync(paths.launcher));
    assert.match(readJson(sb.settings).env.ANTHROPIC_BASE_URL, BASE_RE, 'the terminal part did not connect');
    assertNoKeyText([res.stdout, res.stderr]);
  }));

test('a file that does not match the manifest is refused and nothing is installed', () =>
  withCodeTab(
    async ({ sb, paths, log, claudeExe }) => {
      const res = await runCli(sb, []);
      assert.strictEqual(res.code, 1, res.output);
      assert.match(res.stderr, /did not match the release manifest/);
      assert.ok(!fs.existsSync(paths.launcher), 'a mismatched launcher was put in place');
      assert.ok(!fs.existsSync(paths.helper));
      assert.deepStrictEqual(
        fs.readdirSync(paths.bin).filter((name) => name.endsWith('.download')),
        [],
        'a temp download was left behind'
      );
      assert.ok(!fs.existsSync(paths.config));
      assert.deepStrictEqual(stubCalls(log), []);
      assert.strictEqual(fs.readFileSync(claudeExe, 'utf8'), 'real claude bytes');
      assert.match(readJson(sb.settings).env.ANTHROPIC_BASE_URL, BASE_RE, 'the terminal part did not connect');
      assertNoKeyText([res.stdout, res.stderr]);
    },
    { served: servedFiles(TAMPERED_LAUNCHER, HELPER_BYTES), manifest: manifestFor(LAUNCHER_BYTES, HELPER_BYTES) }
  ));

test('a codetab.json owned by the desktop app is left alone and the terminal part still connects', () =>
  withCodeTab(async ({ sb, paths, releases, log }) => {
    fs.mkdirSync(paths.dir, { recursive: true });
    const desktopConfig = JSON.stringify({ v: 1, enabled: true, base_url: 'https://proxy.contextshrink.com/k/desktopid' }) + '\n';
    fs.writeFileSync(paths.config, desktopConfig);

    const res = await runCli(sb, []);
    assert.strictEqual(res.code, 0, res.output);
    assert.match(res.stdout, /Code tab left alone: the ContextShrink desktop app manages it\./);
    assert.strictEqual(fs.readFileSync(paths.config, 'utf8'), desktopConfig);
    assert.deepStrictEqual(releases.hits, []);
    assert.deepStrictEqual(stubCalls(log), []);
    assert.match(readJson(sb.settings).env.ANTHROPIC_BASE_URL, BASE_RE);
  }));

test('--no-code-tab connects the terminal only and downloads nothing', () =>
  withCodeTab(async ({ sb, paths, releases, log }) => {
    const res = await runCli(sb, ['--no-code-tab']);
    assert.strictEqual(res.code, 0, res.output);
    assert.match(res.stdout, /Code tab skipped \(--no-code-tab\)\./);
    assert.ok(!fs.existsSync(paths.config));
    assert.deepStrictEqual(releases.hits, []);
    assert.deepStrictEqual(stubCalls(log), []);
    assert.match(readJson(sb.settings).env.ANTHROPIC_BASE_URL, BASE_RE);

    const misuse = await runCli(sb, ['disconnect', '--no-code-tab']);
    assert.strictEqual(misuse.code, 2);
    assert.match(misuse.stderr, /--no-code-tab only applies to connect\./);
  }));

test('connect rewrites a codetab.json from before the forwarder format, and it is still owned by npx', () =>
  withCodeTab(async ({ sb, paths, engine }) => {
    fs.mkdirSync(paths.dir, { recursive: true });
    fs.writeFileSync(
      paths.config,
      JSON.stringify({ v: 1, enabled: true, owner: 'npx', mode: 'direct', base_url: 'http://127.0.0.1:9/k/old', headers: {}, env: {} })
    );
    const res = await runCli(sb, []);
    assert.strictEqual(res.code, 0, res.output);
    assert.strictEqual(readJson(paths.config).base_url, engine.url);
    assert.strictEqual(readJson(paths.config).mode, undefined, 'the launch mode was kept');
    assert.strictEqual(readJson(paths.config).owner, 'npx');
    assertNoKeyText([res.stdout, res.stderr]);
  }));

test('disconnect restores Claude Desktop, removes the Code tab files, and keeps unrelated files', () =>
  withCodeTab(async ({ sb, paths, log, claudeExe }) => {
    const res = await runCli(sb, []);
    assert.strictEqual(res.code, 0, res.output);
    const unrelated = path.join(paths.dir, 'notes.txt');
    fs.writeFileSync(unrelated, 'not ours');

    const off = await runCli(sb, ['disconnect']);
    assert.strictEqual(off.code, 0, off.output);
    assert.match(off.stdout, /Code tab: restored 1 Claude Code folder\(s\)/);
    assert.deepStrictEqual(commands(log).slice(-2), ['remove', 'autostart']);
    assert.deepStrictEqual(stubCalls(log).slice(-1)[0].args, ['autostart', 'off']);
    assert.ok(!fs.existsSync(paths.config), 'codetab.json was left behind');
    assert.ok(!fs.existsSync(paths.bin), 'npx-bin was left behind');
    assert.ok(fs.existsSync(unrelated), 'a file that is not ours was removed');
    assert.ok(!fs.existsSync(sb.settings), 'the terminal settings were not restored');
    assert.strictEqual(fs.readFileSync(claudeExe, 'utf8'), 'real claude bytes');
    assertNoKeyText([off.stdout, off.stderr]);
  }));

test('disconnect leaves a codetab.json owned by the desktop app alone and runs no helper', () =>
  withCodeTab(async ({ sb, paths, log }) => {
    const res = await runCli(sb, ['--no-code-tab']);
    assert.strictEqual(res.code, 0, res.output);
    const desktopConfig = JSON.stringify({ v: 1, enabled: true, base_url: 'https://proxy.contextshrink.com/k/desktopid' });
    fs.mkdirSync(paths.dir, { recursive: true });
    fs.writeFileSync(paths.config, desktopConfig);

    const off = await runCli(sb, ['disconnect']);
    assert.strictEqual(off.code, 0, off.output);
    assert.match(off.stdout, /Code tab left alone/);
    assert.strictEqual(fs.readFileSync(paths.config, 'utf8'), desktopConfig);
    assert.deepStrictEqual(stubCalls(log), []);
  }));

test('a Code tab that is still patched is kept, and disconnect says so', () =>
  withCodeTab(async ({ sb, paths, log }) => {
    const res = await runCli(sb, []);
    assert.strictEqual(res.code, 0, res.output);

    sb.env.CONTEXTSHRINK_NPX_STUB_REMOVE_STATE = 'locked';
    const off = await runCli(sb, ['disconnect']);
    assert.strictEqual(off.code, 1, off.output);
    assert.match(off.stderr, /still patched/);
    assert.ok(fs.existsSync(paths.config), 'codetab.json was removed while folders were patched');
    assert.ok(fs.existsSync(paths.launcher), 'the launcher was removed while folders were patched');
    assert.deepStrictEqual(commands(log).slice(-1), ['remove'], 'the shortcut was removed too early');
    assert.ok(!fs.existsSync(sb.settings), 'the terminal settings were not restored');
  }));

test('status shows one Code tab line and never the key', () =>
  withCodeTab(async ({ sb }) => {
    const before = await runCli(sb, ['status']);
    assert.strictEqual(before.code, 0, before.output);
    assert.match(before.stdout, /Code tab: not set up\. Run npx contextshrink-claude to set it up\./);

    const res = await runCli(sb, []);
    assert.strictEqual(res.code, 0, res.output);
    const status = await runCli(sb, ['status']);
    assert.strictEqual(status.code, 0, status.output);
    assert.match(status.stdout, /Code tab: routed \(npx\)\. 1 Claude Code version\(s\) patched; startup re-patch on\./);
    assertNoKeyText([status.stdout, status.stderr]);
  }));

test('without Claude Desktop the Code tab is skipped in one line', () =>
  withCodeTab(async ({ sb, paths, releases }) => {
    fs.rmSync(path.join(sb.localAppData, 'Packages'), { recursive: true, force: true });
    const res = await runCli(sb, []);
    assert.strictEqual(res.code, 0, res.output);
    assert.match(res.stdout, /Claude Desktop not found; Code tab not set up\./);
    assert.deepStrictEqual(releases.hits, []);
    assert.ok(!fs.existsSync(paths.config));
  }));

test('the releases address must be https, or http on 127.0.0.1', () =>
  withCodeTab(async ({ sb, paths, releases }) => {
    sb.env.CONTEXTSHRINK_RELEASES_URL = 'http://releases.example.test';
    const res = await runCli(sb, []);
    assert.strictEqual(res.code, 1, res.output);
    assert.match(res.stderr, /must be an https URL/);
    assert.ok(!fs.existsSync(paths.config));
    assert.deepStrictEqual(releases.hits, []);
  }));

test(
  'a file without the publisher signature is refused before it is put in place',
  // Runs the real Authenticode check through Windows PowerShell, so it is opt-in.
  { skip: process.env.CONTEXTSHRINK_NPX_LIVE_SIGNATURE !== '1' && 'set CONTEXTSHRINK_NPX_LIVE_SIGNATURE=1 to run the live check' },
  () =>
    withCodeTab(
      async ({ sb, paths, log }) => {
        const res = await runCli(sb, []);
        assert.strictEqual(res.code, 1, res.output);
        assert.match(res.stderr, /failed the publisher check/);
        assert.ok(!fs.existsSync(paths.launcher));
        assert.ok(!fs.existsSync(paths.config));
        assert.deepStrictEqual(stubCalls(log), []);
      },
      { signature: true }
    )
);

test('signatureProblem accepts only the ContextShrink publisher, from the expected authority', () => {
  const subject = 'CN=Muhammad Burhan Khan Lodhi, O=Muhammad Burhan Khan Lodhi, L=Somewhere, C=GB';
  const issuer = 'CN=Microsoft ID Verified CS EOC CA 01, O=Microsoft Corporation, C=US';
  assert.strictEqual(signatureProblem({ Status: 'Valid', Subject: subject, Issuer: issuer }), null);
  assert.match(signatureProblem({ Status: 'NotSigned', Subject: '', Issuer: '' }), /NotSigned/);
  assert.ok(signatureProblem({ Status: 'Valid', Subject: 'CN=Someone Else', Issuer: issuer }));
  assert.ok(signatureProblem({ Status: 'Valid', Subject: 'CN=Muhammad Burhan Khan Lodhi Extra, O=x', Issuer: issuer }));
  assert.ok(signatureProblem({ Status: 'Valid', Subject: subject, Issuer: 'CN=Some Other CA' }));
  assert.ok(signatureProblem(null));
});

test('the forwarder config the launcher reads has v 1 and enabled, and health_port is the engine port', () => {
  // The launcher routes only when v is 1 and enabled is true, and checks
  // http://127.0.0.1:<health_port>/__cs/health. This is the exact object it reads.
  assert.deepStrictEqual(
    configFor({ base: 'http://127.0.0.1:18793', env: { ENABLE_TOOL_SEARCH: 'true' } }),
    {
      v: 1,
      owner: 'npx',
      enabled: true,
      base_url: 'http://127.0.0.1:18793',
      health_port: 18793,
      env: { ENABLE_TOOL_SEARCH: 'true' },
    }
  );
});

// Takeover: the desktop app's codetab.json is switched off, so npx takes the Code tab over.
// The stub reports the folders as "real" (nothing patched) unless a test says otherwise.
function desktopOff(sb, paths) {
  sb.env.CONTEXTSHRINK_NPX_STUB_STATUS_STATE = 'real';
  sb.env.CONTEXTSHRINK_NPX_STUB_AUTOSTART = 'off';
  fs.mkdirSync(paths.dir, { recursive: true });
  fs.writeFileSync(paths.config, DESKTOP_OFF_BYTES);
}

test('a switched-off desktop codetab.json is taken over: npx writes its config and records the desktop bytes', () =>
  withCodeTab(async ({ sb, engine, log, paths }) => {
    desktopOff(sb, paths);
    const res = await runCli(sb, []);
    assert.strictEqual(res.code, 0, res.output);
    assert.match(
      res.stdout,
      /Code tab: routed through the ContextShrink engine \(the desktop app's Code-tab routing was off; uninstall hands it back\)\./
    );
    assert.doesNotMatch(res.stdout, /left alone/);
    assert.deepStrictEqual(readJson(paths.config), {
      v: 1,
      owner: 'npx',
      enabled: true,
      base_url: engine.url,
      health_port: Number(new URL(engine.url).port),
      env: { ENABLE_TOOL_SEARCH: 'true' },
    });
    const record = readJson(paths.takeover);
    assert.ok(Buffer.from(record.previous.base64, 'base64').equals(DESKTOP_OFF_BYTES), 'the desktop bytes were not recorded exactly');
    assert.strictEqual(record.patched_by_npx, true, 'nothing was patched before, so npx owns the patch');
    assert.strictEqual(record.autostart_by_npx, true, 'the startup re-patch was off before, so npx owns it');
    assert.deepStrictEqual(commands(log), ['status', 'install', 'autostart']);
    assert.deepStrictEqual(stubCalls(log)[1].args, ['install', '--json', '--launcher', paths.launcher]);
    assertNoKeyText([res.stdout, res.stderr]);
  }));

test('a takeover over a folder the desktop app had patched does not unpatch it at uninstall', () =>
  withCodeTab(async ({ sb, paths, log }) => {
    desktopOff(sb, paths);
    sb.env.CONTEXTSHRINK_NPX_STUB_STATUS_STATE = 'patched';
    sb.env.CONTEXTSHRINK_NPX_STUB_AUTOSTART = 'on';
    const up = await runCli(sb, []);
    assert.strictEqual(up.code, 0, up.output);
    const record = readJson(paths.takeover);
    assert.strictEqual(record.patched_by_npx, false);
    assert.strictEqual(record.autostart_by_npx, false);

    const off = await runCli(sb, ['uninstall']);
    assert.strictEqual(off.code, 0, off.output);
    assert.match(off.stdout, /Claude Desktop stays patched, as the desktop app had it before\./);
    assert.ok(fs.readFileSync(paths.config).equals(DESKTOP_OFF_BYTES));
    assert.deepStrictEqual(commands(log), ['status', 'install', 'autostart'], 'the helper ran remove or turned the shortcut off');
  }));

test('uninstall hands the Code tab back: the desktop codetab.json is restored byte for byte', () =>
  withCodeTab(async ({ sb, paths, log, claudeExe }) => {
    desktopOff(sb, paths);
    const up = await runCli(sb, []);
    assert.strictEqual(up.code, 0, up.output);

    const off = await runCli(sb, ['uninstall']);
    assert.strictEqual(off.code, 0, off.output);
    assert.match(off.stdout, /Code tab: restored 1 Claude Code folder\(s\) and handed the Code tab back to the ContextShrink desktop app\./);
    assert.ok(fs.readFileSync(paths.config).equals(DESKTOP_OFF_BYTES), 'codetab.json was not restored byte for byte');
    assert.ok(!fs.existsSync(paths.takeover), 'the takeover record was left behind');
    assert.ok(!fs.existsSync(paths.bin), 'npx-bin was left behind');
    assert.deepStrictEqual(commands(log).slice(-2), ['remove', 'autostart']);
    assert.deepStrictEqual(stubCalls(log).slice(-1)[0].args, ['autostart', 'off']);
    assert.strictEqual(fs.readFileSync(claudeExe, 'utf8'), 'real claude bytes');
    assertNoKeyText([off.stdout, off.stderr]);
  }));

test('uninstall leaves codetab.json alone when the desktop app changed it after the takeover', () =>
  withCodeTab(async ({ sb, paths, log }) => {
    desktopOff(sb, paths);
    const up = await runCli(sb, []);
    assert.strictEqual(up.code, 0, up.output);
    const changed = Buffer.from(
      JSON.stringify({ v: 1, enabled: true, base_url: 'http://127.0.0.1:18788/k/newer', health_port: 18788, env: {} }) + '\n'
    );
    fs.writeFileSync(paths.config, changed);

    const off = await runCli(sb, ['uninstall']);
    assert.strictEqual(off.code, 0, off.output);
    assert.match(off.stdout, /Code tab left alone: codetab.json changed after ContextShrink took it over/);
    assert.ok(fs.readFileSync(paths.config).equals(changed), 'the desktop app\'s file was overwritten');
    assert.ok(!fs.existsSync(paths.takeover));
    assert.ok(fs.existsSync(paths.helper), 'the helper was removed while the desktop app may still use the patch');
    assert.deepStrictEqual(commands(log), ['status', 'install', 'autostart'], 'the helper ran after the desktop app changed the file');
  }));

test('a takeover whose patch fails puts the desktop codetab.json back exactly and keeps no record', () =>
  withCodeTab(async ({ sb, paths }) => {
    desktopOff(sb, paths);
    sb.env.CONTEXTSHRINK_NPX_STUB_INSTALL_FAIL = '1';
    const res = await runCli(sb, []);
    assert.strictEqual(res.code, 1, res.output);
    assert.match(res.stderr, /the helper could not patch Claude Desktop \(exit code 5\)/);
    assert.ok(fs.readFileSync(paths.config).equals(DESKTOP_OFF_BYTES), 'the desktop codetab.json was not put back');
    assert.ok(!fs.existsSync(paths.takeover), 'a record was kept for a takeover that did not happen');
    assert.match(readJson(sb.settings).env.ANTHROPIC_BASE_URL, BASE_RE, 'the terminal part did not connect');
  }));

test('a Code tab still patched at uninstall keeps npx config, so a retry hands it back', () =>
  withCodeTab(async ({ sb, paths }) => {
    desktopOff(sb, paths);
    const up = await runCli(sb, []);
    assert.strictEqual(up.code, 0, up.output);

    sb.env.CONTEXTSHRINK_NPX_STUB_REMOVE_STATE = 'locked';
    const first = await runCli(sb, ['uninstall']);
    assert.strictEqual(first.code, 1, first.output);
    assert.match(first.stderr, /still patched/);
    const kept = readJson(paths.config);
    assert.strictEqual(kept.owner, 'npx');
    assert.strictEqual(kept.enabled, true, 'the config was left switched off while folders are still patched');
    assert.ok(fs.existsSync(paths.takeover), 'the takeover record was removed too early');

    delete sb.env.CONTEXTSHRINK_NPX_STUB_REMOVE_STATE;
    const second = await runCli(sb, ['uninstall']);
    assert.strictEqual(second.code, 0, second.output);
    assert.ok(fs.readFileSync(paths.config).equals(DESKTOP_OFF_BYTES));
    assert.ok(!fs.existsSync(paths.takeover));
  }));

test('a second connect while taken over keeps the desktop bytes on record', () =>
  withCodeTab(async ({ sb, paths }) => {
    desktopOff(sb, paths);
    const up = await runCli(sb, []);
    assert.strictEqual(up.code, 0, up.output);
    const first = readJson(paths.takeover);

    const again = await runCli(sb, [], { input: '' });
    assert.strictEqual(again.code, 0, again.output);
    const second = readJson(paths.takeover);
    assert.deepStrictEqual(second.previous, first.previous, 'npx recorded its own config as the desktop app\'s');
    assert.strictEqual(second.written, fs.readFileSync(paths.config, 'utf8'), 'the record does not match the file npx wrote');

    const off = await runCli(sb, ['uninstall']);
    assert.strictEqual(off.code, 0, off.output);
    assert.ok(fs.readFileSync(paths.config).equals(DESKTOP_OFF_BYTES));
  }));

test('--no-code-tab leaves a switched-off desktop codetab.json alone', () =>
  withCodeTab(async ({ sb, paths, log }) => {
    desktopOff(sb, paths);
    const res = await runCli(sb, ['--no-code-tab']);
    assert.strictEqual(res.code, 0, res.output);
    assert.match(res.stdout, /Code tab left alone: the ContextShrink desktop app manages it\./);
    assert.ok(fs.readFileSync(paths.config).equals(DESKTOP_OFF_BYTES));
    assert.ok(!fs.existsSync(paths.takeover));
    assert.deepStrictEqual(stubCalls(log), []);
  }));

test('status says routed (npx), desktop app, or not set up, and never the key', () =>
  withCodeTab(async ({ sb, paths }) => {
    const none = await runCli(sb, ['status']);
    assert.match(none.stdout, /Code tab: not set up\. Run npx contextshrink-claude to set it up\./);

    fs.mkdirSync(paths.dir, { recursive: true });
    fs.writeFileSync(paths.config, DESKTOP_OFF_BYTES);
    const off = await runCli(sb, ['status']);
    assert.match(off.stdout, /^Code tab: desktop app\. Its routing is off;/m);

    fs.writeFileSync(paths.config, JSON.stringify({ v: 1, enabled: true, base_url: 'http://127.0.0.1:18788/k/x' }));
    const on = await runCli(sb, ['status']);
    assert.match(on.stdout, /^Code tab: desktop app\. The ContextShrink desktop app routes it\./m);

    desktopOff(sb, paths);
    const up = await runCli(sb, []);
    assert.strictEqual(up.code, 0, up.output);
    sb.env.CONTEXTSHRINK_NPX_STUB_STATUS_STATE = 'patched';
    const routed = await runCli(sb, ['status']);
    assert.match(routed.stdout, /^Code tab: routed \(npx\)\. 1 Claude Code version\(s\) patched; startup re-patch off\./m);
    assertNoKeyText([none.stdout, off.stdout, on.stdout, routed.stdout]);
  }));
