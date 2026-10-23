'use strict';

// Browser sign-in and the device calls, against the fake API. Shared by both npx
// packages (identical copies). Nothing opens a real browser.

const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const login = require('../lib/code-login');
const { startCodeApi, SITE } = require('./code-support');

const HASH = crypto.createHash('sha256').update('test').digest('hex');

async function withApi(fn) {
  const api = await startCodeApi();
  const lines = [];
  const ctx = { env: {}, out: (line = '') => lines.push(line), err: (line = '') => lines.push(line) };
  try {
    await fn({ api, ctx, lines });
  } finally {
    await api.close();
  }
}

function signIn(api, ctx, extra = {}) {
  const opened = [];
  const promise = login.runBrowserLogin(ctx, {
    apiUrl: api.url,
    tool: 'codex',
    deviceHash: HASH,
    deviceName: 'Test PC',
    commandName: 'contextshrink-codex',
    openBrowser: async (url) => opened.push(url),
    sleep: async () => {},
    env: {},
    ...extra,
  });
  return { promise, opened };
}

test('browser sign-in shows the link and code, opens the browser, and returns the approved device', () =>
  withApi(async ({ api, ctx, lines }) => {
    api.state.pollsBeforeApproval = 2;
    const { promise, opened } = signIn(api, ctx);
    const result = await promise;
    assert.equal(result.device_key, api.issued[0]);
    assert.equal(result.api_url, api.url);
    assert.deepEqual(opened, [`${SITE}/connect?code=ABCD-BFGH`]);
    assert.ok(lines.includes('Code: ABCD-BFGH'));
    assert.equal(api.calls.filter((c) => c.path === '/code/cli/poll').length, 3, 'pending twice, then approved');
  }));

test('a browser that will not open does not stop the sign-in', () =>
  withApi(async ({ api, ctx, lines }) => {
    const { promise } = signIn(api, ctx, {
      openBrowser: async () => {
        throw new Error('no browser');
      },
    });
    const result = await promise;
    assert.ok(result.device_key.startsWith('cs_dev_'));
    assert.ok(lines.includes('Could not open the browser. Open the link above yourself.'));
  }));

const LIMIT_DEVICES = [
  { id: 'a', name: 'Laptop', platform: 'windows', last_seen_at: null },
  { id: 'b', name: 'Desktop', platform: 'windows', last_seen_at: null },
];

test('a device limit is shown once with the devices and this package\'s uninstall command, and the sign-in keeps waiting', () =>
  withApi(async ({ api, ctx, lines }) => {
    api.state.deviceLimit = LIMIT_DEVICES;
    let sleeps = 0;
    // The user disconnects a device in the browser and approves again.
    const { promise } = signIn(api, ctx, {
      sleep: async () => {
        sleeps += 1;
        if (sleeps === 2) api.state.deviceLimit = null;
      },
    });
    const result = await promise;
    assert.ok(result.device_key.startsWith('cs_dev_'));
    assert.equal(api.calls.filter((c) => c.path === '/code/cli/poll').length, 3, 'limit twice, then approved');

    const limitLines = lines.filter((line) => line.startsWith('This account already has 2 devices connected'));
    assert.equal(limitLines.length, 1, 'the device limit is printed once');
    assert.match(limitLines[0], /^This account already has 2 devices connected: Laptop, Desktop\.$/m);
    assert.match(limitLines[0], /run "npx contextshrink-codex uninstall" on that device/);
    assert.match(limitLines[0], /https:\/\/code\.contextshrink\.com\/account/);
    assert.ok(lines.includes('Or disconnect one in the browser window and approve again; this command is still waiting.'));
    assert.equal(api.issued.length, 1);
  }));

test('a sign-in that expires while at the device limit exits with code 3', () =>
  withApi(async ({ api, ctx }) => {
    api.state.deviceLimit = LIMIT_DEVICES;
    const { promise } = signIn(api, ctx, {
      sleep: async () => {
        api.state.deviceLimit = null;
        api.state.pollStatus = 'expired';
      },
    });
    await assert.rejects(promise, (error) => {
      assert.ok(error instanceof login.DeviceLimitError);
      assert.equal(error.exitCode, 3);
      assert.match(error.message, /expired while this account was still at its device limit/);
      return true;
    });
    assert.deepEqual(api.issued, [], 'a device key was issued past the limit');
  }));

test('a sign-in that expires without a device limit is an ordinary expiry, not exit code 3', () =>
  withApi(async ({ api, ctx }) => {
    api.state.pollStatus = 'expired';
    await assert.rejects(signIn(api, ctx).promise, (error) => {
      assert.ok(error instanceof login.LoginError);
      assert.equal(error.exitCode, 1);
      return true;
    });
  }));

test('an expired or denied sign-in is reported, and nothing is returned', () =>
  withApi(async ({ api, ctx }) => {
    api.state.pollStatus = 'expired';
    await assert.rejects(signIn(api, ctx).promise, /expired before this computer was approved/);
    api.state.pollStatus = 'denied';
    await assert.rejects(signIn(api, ctx).promise, /was not approved/);
  }));

test('a sign-in link that is not a plain https address is refused before the browser opens', () =>
  withApi(async ({ api, ctx }) => {
    api.state.verifyUrl = 'https://evil.example/connect?code=X&next=y';
    const { promise, opened } = signIn(api, ctx);
    await assert.rejects(promise, /unexpected sign-in response/);
    assert.deepEqual(opened, []);
  }));

test('the plan note answers 401 for an unknown device key, and never throws on it', () =>
  withApi(async ({ api }) => {
    const note = await login.fetchPlanNote(api.url, 'cs_dev_unknownunknownunknown', HASH);
    assert.equal(note.status, 401);
  }));

test('the plan note answers 402 when the free week is used', () =>
  withApi(async ({ api, ctx }) => {
    const { promise } = signIn(api, ctx);
    const result = await promise;
    api.state.plan = 'ended';
    const note = await login.fetchPlanNote(api.url, result.device_key, HASH);
    assert.equal(note.status, 402);
    assert.equal(note.body.detail.code, 'plan_required');
  }));

test('releasing a device is idempotent, and the usage summary needs a valid device key', () =>
  withApi(async ({ api, ctx }) => {
    const result = await signIn(api, ctx).promise;
    await login.releaseDevice(api.url, result.device_key);
    await login.releaseDevice(api.url, result.device_key);
    await assert.rejects(login.fetchUsageSummary(api.url, result.device_key), /not available/);
    await assert.rejects(login.releaseDevice(api.url, 'cs_dev_unknownunknownunknown'), /./);
  }));

test('device names from the server are cleaned of control characters and cut to 60 characters', () => {
  assert.equal(login.cleanName('A\u0000B\tC'), 'A B C');
  assert.equal(login.cleanName('x'.repeat(100)).length, 60);
  assert.equal(login.cleanName(''), 'unnamed device');
});
