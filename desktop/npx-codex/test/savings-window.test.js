'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { EventEmitter } = require('node:events');
const { summary, format } = require('../lib/savings-summary');
const window = require('../lib/savings-window');
const data = { scope: 'key', period: 'today_utc', requests: 15, tokens_before: 10000, tokens_saved: 300, savings_usd: 0.04 };
test('shared savings presentation reports net costs, zero totals, and the correct scope', () => {
  assert.match(format(summary(data)), /trimmed 3% · net saved \$0.04 · 15 req · this key today \(UTC\)/);
  assert.match(format(summary({ ...data, savings_usd: -0.01 })), /net cost \$0.01/);
  assert.match(format(summary({ ...data, tokens_before: 0, tokens_saved: 0 })), /trimmed 0%/);
  for (const patch of [{ scope: 'account' }, { period: 'month' }, { requests: -1 }, { tokens_saved: 10001 }, { savings_usd: Infinity }]) assert.throws(() => summary({ ...data, ...patch }));
});
test('window snapshots contain numeric totals only and clear stale values on error', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'cs-window-test-')), file = path.join(root, 'savings.json');
  try {
    await window.refresh(async () => ({ ...summary(data), key: 'fake-secret-key', authorization: 'do not copy' }), file);
    const text = fs.readFileSync(file, 'utf8'); assert.ok(!text.includes('secret')); assert.ok(!text.includes('authorization'));
    assert.equal(JSON.parse(text).requests, 15);
    await window.refresh(async () => { throw new Error('error with fake-secret-key'); }, file);
    const unavailable = fs.readFileSync(file, 'utf8'); assert.equal(JSON.parse(unavailable).available, false);
    assert.ok(!unavailable.includes('secret')); assert.ok(!unavailable.includes('netUsd'));
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
test('unsupported platforms fail without opening a process or requesting totals', async () => {
  await assert.rejects(window.show(() => { throw new Error('should not run'); }, { out() {} }, { platform: 'linux' }), /Windows/);
});
