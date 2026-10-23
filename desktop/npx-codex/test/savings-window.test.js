'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { EventEmitter } = require('node:events');
const { summary, fromLocal, format } = require('../lib/savings-summary');
const window = require('../lib/savings-window');
const data = { today: { requests: 15, tokens_before: 10000, tokens_after: 9700, tokens_saved: 300, pct: 3 } };
test('the usage line shows the percent and the free-week days, with no dollar amounts', () => {
  assert.equal(format(summary(data)), 'ContextShrink · usage saved 3.0% today');
  assert.equal(format(summary(data, { plan: 'trial', daysLeft: 5 })), 'ContextShrink · usage saved 3.0% today · free week 5d left');
  assert.equal(format(summary(data, { plan: 'active' })), 'ContextShrink · usage saved 3.0% today · Subscribed');
  assert.equal(format(summary({ today: { requests: 0, pct: 0 } })), 'ContextShrink · usage saved 0.0% today');
  assert.equal(format(summary({ today: { requests: 40, pct: 42.7 } })), 'ContextShrink · usage saved 43% today');
  assert.doesNotMatch(format(summary(data, { plan: 'trial', daysLeft: 5 })), /\$/);
  for (const patch of [{ today: undefined }, { today: { ...data.today, pct: NaN } }, { today: { ...data.today, requests: -1 } }]) {
    assert.throws(() => summary({ ...data, ...patch }));
  }
});
test('window snapshots contain numeric totals only and clear stale values on error', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'cs-window-test-')), file = path.join(root, 'savings.json');
  try {
    await window.refresh(async () => ({ ...summary(data), key: 'fake-secret-key', authorization: 'do not copy' }), file);
    const text = fs.readFileSync(file, 'utf8'); assert.ok(!text.includes('secret')); assert.ok(!text.includes('authorization'));
    assert.equal(JSON.parse(text).requests, 15);
    await window.refresh(async () => { throw new Error('error with fake-secret-key'); }, file);
    const unavailable = fs.readFileSync(file, 'utf8'); assert.equal(JSON.parse(unavailable).available, false);
    assert.ok(!unavailable.includes('secret')); assert.ok(!unavailable.includes('trialDays'));
    await window.refresh(async () => ({ ...summary(data), pct: 500 }), file);
    assert.equal(JSON.parse(fs.readFileSync(file)).available, false);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
test('window lifecycle hides the console, passes no credentials, and removes its snapshot', async () => {
  let snapshot, executable, args, settings, calls = 0;
  const result = await window.show(async () => { calls++; return summary(data); }, { out() {} }, {
    platform: 'win32', intervalMs: 10,
    spawnProcess(bin, argv, options) {
      executable = bin; args = argv; settings = options;
      snapshot = argv[argv.indexOf('-SnapshotPath')+1];
      assert.equal(JSON.parse(fs.readFileSync(snapshot)).requests, 15);
      const child = new EventEmitter(); child.kill = () => child.emit('exit', 0);
      setTimeout(() => child.emit('exit', 0), 60); return child;
    },
  });
  assert.equal(result, 0); assert.ok(calls >= 2); assert.equal(settings.windowsHide, true); assert.equal(settings.shell, false);
  assert.match(executable, /powershell.exe$/i); assert.ok(!args.join(' ').includes('cs_live_'));
  assert.equal(args[args.indexOf('-ExecutionPolicy')+1], 'RemoteSigned');
  assert.ok(!fs.existsSync(snapshot)); assert.ok(!fs.existsSync(path.dirname(snapshot)));
});
test('today comes from the local usage file (Codex buckets only, UTC day), with no network call', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'cs-local-test-'));
  try {
    const now = new Date('2026-10-23T12:00:00Z');
    const bucket = (hour, tool, requests, before, after) => ({ hour, tool, requests, tokens_before: before, tokens_after: after });
    fs.writeFileSync(path.join(root, 'usage-pending.json'), JSON.stringify({ version: 1, buckets: [
      bucket('2026-10-23T09:00:00Z', 'codex', 30, 1000, 957),
      bucket('2026-10-23T10:00:00Z', 'codex', 10, 1000, 957),
      bucket('2026-10-22T23:00:00Z', 'codex', 99, 9000, 0),
      bucket('2026-10-23T11:00:00Z', 'claude', 99, 9000, 0),
    ] }));
    const snapshot = fromLocal(root, { plan: 'trial', daysLeft: 3 }, now);
    assert.equal(format(snapshot), 'ContextShrink · usage saved 4.3% today · free week 3d left');
    assert.equal(snapshot.requests, 40);
    assert.equal(fromLocal(path.join(root, 'missing'), null, now), null);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('unsupported platforms fail without opening a process or requesting totals', async () => {
  await assert.rejects(window.show(() => { throw new Error('should not run'); }, { out() {} }, { platform: 'linux' }), /Windows/);
});

