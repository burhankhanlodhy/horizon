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

test('a Valid signature from another publisher is refused: the engine is pinned to the ContextShrink publisher', () =>
  withEngine(async ({ sb, ctx, adapter, manifest }) => {
    const issuer = 'CN=Microsoft ID Verified CS EOC CA 01, O=Microsoft Corporation, C=US';
    for (const Subject of ['CN=Someone Else, O=Someone Else, C=US', 'CN=Muhammad Burhan Khan Lodhi Extra, O=x']) {
      const other = { ...adapter, signatureStatus: async () => ({ Status: 'Valid', Subject, Issuer: issuer }) };
      await assert.rejects(
        engine.installEngine(ctx, sb.env, manifest, other),
        /failed the publisher check \(the signer is not the expected publisher\)\. Nothing was installed\./
      );
    }
    const wrongAuthority = {
      ...adapter,
      signatureStatus: async () => ({
        Status: 'Valid',
        Subject: 'CN=Muhammad Burhan Khan Lodhi, O=Muhammad Burhan Khan Lodhi, L=Somewhere, C=GB',
        Issuer: 'CN=Some Other CA',
      }),
    };
    await assert.rejects(engine.installEngine(ctx, sb.env, manifest, wrongAuthority), /not from the expected authority/);
    assert.ok(!fs.existsSync(path.join(sb.engineRoot, ENGINE_VERSION)), 'an engine from another publisher was put in place');
  }));

test('the engine from the ContextShrink publisher installs (the fake adapter signs it as that publisher)', () =>
  withEngine(async ({ sb, ctx, adapter, manifest }) => {
    const exe = await engine.installEngine(ctx, sb.env, manifest, adapter);
    assert.ok(fs.existsSync(exe));
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

// The real adapter's PowerShell calls, captured instead of run.
function recordingPowerShell() {
  const calls = [];
  const powerShell = async (script, vars, timeoutMs) => {
    calls.push({ script, vars, timeoutMs });
    return '';
  };
  return { calls, powerShell };
}

test('the unzip uses .NET ZipFile with the long timeout, and other PowerShell calls keep the short one', async () => {
  const { calls, powerShell } = recordingPowerShell();
  const real = engine.realAdapter({ powerShell });
  await real.expandZip('C:\\tmp\\engine.zip', 'C:\\tmp\\staging');
  assert.equal(calls.length, 1);
  const [unzip] = calls;
  assert.match(unzip.script, /\[System\.IO\.Compression\.ZipFile\]::ExtractToDirectory\(\$env:CONTEXTSHRINK_ZIP, \$env:CONTEXTSHRINK_DEST\)/);
  assert.match(unzip.script, /Add-Type -AssemblyName System\.IO\.Compression\.FileSystem/);
  assert.doesNotMatch(unzip.script, /Expand-Archive/);
  assert.equal(unzip.timeoutMs, engine.UNZIP_TIMEOUT_MS);
  assert.ok(engine.UNZIP_TIMEOUT_MS >= 300000, 'the unzip timeout is too short for antivirus scanning');
  assert.deepEqual(unzip.vars, { CONTEXTSHRINK_ZIP: 'C:\\tmp\\engine.zip', CONTEXTSHRINK_DEST: 'C:\\tmp\\staging' });

  await real.writeShortcut('C:\\tmp\\x.lnk', { target: 'C:\\tmp\\horizon.exe', args: 'code-engine run', workdir: 'C:\\tmp' });
  assert.equal(calls.length, 2);
  assert.equal(calls[1].timeoutMs, undefined, 'the shortcut call should keep the default (short) timeout');
});

test('an unzip killed by its timeout says it took too long, and nothing is installed', () =>
  withEngine(async ({ sb, ctx, adapter, manifest }) => {
    const slow = {
      ...adapter,
      expandZip: async () => {
        throw Object.assign(new Error('Command failed'), { killed: true, signal: 'SIGTERM', code: null });
      },
    };
    await assert.rejects(engine.installEngine(ctx, sb.env, manifest, slow), (error) => {
      assert.ok(error instanceof CsError);
      assert.equal(
        error.message,
        'The ContextShrink engine took too long to unpack (antivirus scanning can slow this). Nothing was installed. Run this command again.'
      );
      assert.ok(!error.message.includes('Command failed') && !error.message.includes(sb.engineRoot));
      return true;
    });
    assert.ok(!fs.existsSync(path.join(sb.engineRoot, ENGINE_VERSION)));
    assert.deepEqual(fs.readdirSync(sb.engineRoot), [], 'a staging folder was left behind');
  }));

test('a PowerShell timeout from the real adapter gets the same message', () =>
  withEngine(async ({ sb, ctx, adapter, manifest }) => {
    const powerShell = async () => {
      throw Object.assign(new Error('Command failed'), { killed: true, signal: 'SIGTERM', code: null });
    };
    const real = { ...adapter, expandZip: engine.realAdapter({ powerShell }).expandZip };
    await assert.rejects(engine.installEngine(ctx, sb.env, manifest, real), /took too long to unpack/);
    assert.ok(!fs.existsSync(path.join(sb.engineRoot, ENGINE_VERSION)));
  }));

test('any other unzip failure keeps the plain unpack message', () =>
  withEngine(async ({ sb, ctx, adapter, manifest }) => {
    const broken = {
      ...adapter,
      expandZip: async () => {
        throw new Error('Exception calling ExtractToDirectory');
      },
    };
    await assert.rejects(engine.installEngine(ctx, sb.env, manifest, broken), (error) => {
      assert.ok(error instanceof CsError);
      assert.equal(error.message, 'The ContextShrink engine could not be unpacked. Nothing was installed.');
      return true;
    });
  }));
