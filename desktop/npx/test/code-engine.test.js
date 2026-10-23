'use strict';

// Engine install, start and stop. Shared by both npx packages (identical copies).
// The download is real (a loopback server); the unzip, signature and shortcut are
// the fake adapter. No PowerShell runs.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const engine = require('../lib/code-engine');
const { CsError } = require('../lib/code-env');
const { startCodeApi, startFakeEngine, makeCodeSandbox, sha256, ENGINE_VERSION, ENGINE_ZIP, ADAPTER } = require('./code-support');

async function withEngine(fn) {
  const api = await startCodeApi();
  const server = await startFakeEngine();
  const sb = makeCodeSandbox(api, server);
  const lines = [];
  const ctx = { env: sb.env, out: (line = '') => lines.push(line), err: (line = '') => lines.push(line) };
  const adapter = require(ADAPTER)(sb.env);
  const manifest = {
    published: true,
    version: ENGINE_VERSION,
    url: `${api.url}/engine.zip`,
    sha256: sha256(ENGINE_ZIP),
    size: ENGINE_ZIP.length,
  };
  try {
    await fn({ api, server, sb, ctx, lines, adapter, manifest });
  } finally {
    sb.cleanup();
    await server.close();
    await api.close();
  }
}

function downloadsOf(api) {
  return api.calls.filter((c) => c.path === '/engine.zip').length;
}

test('the engine installs into the versioned folder, and a pinned version already there is reused', () =>
  withEngine(async ({ api, sb, ctx, adapter, manifest }) => {
    const exe = await engine.installEngine(ctx, sb.env, manifest, adapter);
    assert.equal(exe, sb.engineExe);
    assert.ok(fs.existsSync(sb.engineExe));
    assert.equal(downloadsOf(api), 1);
    assert.deepEqual(
      fs.readdirSync(sb.engineRoot).sort(),
      [ENGINE_VERSION],
      'a temp download or staging folder was left behind'
    );

    const again = await engine.installEngine(ctx, sb.env, manifest, adapter);
    assert.equal(again, sb.engineExe);
    assert.equal(downloadsOf(api), 1, 'the engine was downloaded again');
  }));

test('a download that does not match the manifest is refused, and nothing is installed', () =>
  withEngine(async ({ sb, ctx, adapter, manifest }) => {
    const wrong = { ...manifest, sha256: '0'.repeat(64) };
    await assert.rejects(engine.installEngine(ctx, sb.env, wrong, adapter), (error) => {
      assert.ok(error instanceof CsError);
      assert.match(error.message, /did not match the release manifest/);
      return true;
    });
    assert.ok(!fs.existsSync(sb.engineExe));
    const leftovers = fs.existsSync(sb.engineRoot) ? fs.readdirSync(sb.engineRoot) : [];
    assert.deepEqual(leftovers, [], 'a temp file was left behind');
  }));

test('an engine whose signature is not Valid is refused before it is put in place', () =>
  withEngine(async ({ sb, ctx, adapter, manifest }) => {
    const unsigned = { ...adapter, signatureStatus: async () => ({ Status: 'NotSigned' }) };
    await assert.rejects(engine.installEngine(ctx, sb.env, manifest, unsigned), /failed the signature check \(status: NotSigned\)/);
    assert.ok(!fs.existsSync(path.join(sb.engineRoot, ENGINE_VERSION)), 'an unsigned engine was put in place');
  }));

test('a failed download says so, with its HTTP status', () =>
  withEngine(async ({ sb, ctx, adapter, manifest }) => {
    const missing = { ...manifest, url: manifest.url + '-missing' };
    await assert.rejects(engine.installEngine(ctx, sb.env, missing, adapter), /Could not download the ContextShrink engine \(HTTP 404\)/);
  }));

test('the manifest loader: an empty url is unpublished; a broken manifest is refused', () => {
  const dir = fs.mkdtempSync(require('node:os').tmpdir() + path.sep + 'cs-manifest-');
  try {
    const file = path.join(dir, 'manifest.json');
    const env = { CONTEXTSHRINK_NPX_TEST: '1', CONTEXTSHRINK_NPX_ENGINE_MANIFEST: file };
    fs.writeFileSync(file, '{"version":"0.0.0","url":"","sha256":"","size":0}');
    assert.deepEqual(engine.loadManifest(env), { published: false });
    fs.writeFileSync(file, '{"version":"1.2.3","url":"http://example.test/x.zip","sha256":"' + '0'.repeat(64) + '","size":5}');
    assert.throws(() => engine.loadManifest(env), /manifest is not valid/);
    fs.writeFileSync(file, '{"version":"1.2.3","url":"https://app.contextshrink.com/releases/engine/1.2.3/x.zip","sha256":"' + '0'.repeat(64) + '","size":5}');
    assert.equal(engine.loadManifest(env).published, true);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('the engine starts when its health check answers, and stops for each installed copy', () =>
  withEngine(async ({ sb, ctx, adapter, manifest, server }) => {
    const exe = await engine.installEngine(ctx, sb.env, manifest, adapter);
    await engine.startEngine(sb.env, exe, adapter);
    assert.ok(fs.readFileSync(sb.log, 'utf8').includes('"code-engine","ensure"'));
    await engine.stopEngine(sb.env, adapter);
    assert.ok(fs.readFileSync(sb.log, 'utf8').includes('"code-engine","stop"'));
    assert.ok(server.hits > 0);
  }));

test('an engine that never answers its health check is reported after the wait', () =>
  withEngine(async ({ sb, ctx, adapter, manifest, server }) => {
    const exe = await engine.installEngine(ctx, sb.env, manifest, adapter);
    server.state.up = false;
    await assert.rejects(engine.startEngine(sb.env, exe, adapter), /The ContextShrink engine did not start\. Nothing was changed/);
  }));

test('the Startup shortcut is written for `code-engine run`, and removed on uninstall', () =>
  withEngine(async ({ sb, ctx, adapter, manifest }) => {
    const exe = await engine.installEngine(ctx, sb.env, manifest, adapter);
    await engine.installStartup(sb.env, exe, adapter);
    assert.ok(fs.existsSync(sb.startup));
    assert.ok(fs.readFileSync(sb.log, 'utf8').includes('"args":"code-engine run"'));
    engine.removeStartup(sb.env);
    assert.ok(!fs.existsSync(sb.startup));
    engine.removeEngine(sb.env);
    assert.ok(!fs.existsSync(sb.engineRoot));
  }));
