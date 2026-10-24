'use strict';

// Keep sessions lean (`lean on`, `lean off`, `lean status`): settings.json, the Code tab's
// codetab.json, the lean record, and uninstall. Every path lives in the sandbox temp folder;
// the Code tab is a fake codetab.json under the sandbox LOCALAPPDATA (Windows test platform).

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const { withSandbox, runCli, writeSettingsFile, readSettings, readJson } = require('./support');

const ON = 'Keep sessions lean: on (Claude Code compacts at 200,000 tokens)';
const OFF = "Keep sessions lean: off (Claude Code's default window)";
const RESTART = 'Start a new Claude Code session (or restart Claude Desktop) for this to apply.';

function codeTabFile(sb) {
  return path.join(sb.localAppData, 'ContextShrink', 'claude-code', 'codetab.json');
}

function leanRecordFile(sb) {
  return path.join(sb.configDir, '.contextshrink-lean.json');
}

// The forwarder config npx writes (owner npx, enabled true), as connect writes it.
function writeNpxCodeTab(sb) {
  const file = codeTabFile(sb);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const config = {
    v: 1,
    owner: 'npx',
    enabled: true,
    base_url: 'http://127.0.0.1:18793',
    health_port: 18793,
    env: { ENABLE_TOOL_SEARCH: 'true' },
  };
  fs.writeFileSync(file, JSON.stringify(config, null, 2) + '\n');
  return file;
}

// A desktop-app codetab.json (no owner, routing on). Its 4-space indent and CRLF make any rewrite visible.
const DESKTOP_CODE_TAB = Buffer.from(
  JSON.stringify({ v: 1, enabled: true, base_url: 'http://127.0.0.1:18788/k/desktopid', health_port: 18788, env: {} }, null, 4) +
    '\r\n'
);

test('lean on sets the window in settings.json; lean off removes it and the file lean created', () =>
  withSandbox(async ({ sb }) => {
    const on = await runCli(sb, ['lean', 'on']);
    assert.strictEqual(on.code, 0, on.output);
    assert.match(on.stdout, /^Keep sessions lean: on \(Claude Code compacts at 200,000 tokens\)$/m);
    assert.ok(on.stdout.includes(RESTART), on.stdout);
    assert.match(on.stdout, /A session already longer than 200,000 tokens is summarised on its next turn\./);
    assert.strictEqual(readSettings(sb).env.CLAUDE_CODE_AUTO_COMPACT_WINDOW, '200000');
    assert.strictEqual((await runCli(sb, ['lean', 'status'])).stdout.trim(), ON);

    const off = await runCli(sb, ['lean', 'off']);
    assert.strictEqual(off.code, 0, off.output);
    assert.ok(off.stdout.includes(OFF), off.stdout);
    assert.ok(off.stdout.includes(RESTART), off.stdout);
    assert.strictEqual(fs.existsSync(sb.settings), false, 'the settings file that lean created was removed');
    assert.strictEqual(fs.existsSync(leanRecordFile(sb)), false, 'the lean record was removed');
    assert.strictEqual((await runCli(sb, ['lean', 'status'])).stdout.trim(), OFF);
  }));

test('a window the user set to another value is refused unless --force; lean off puts it back', () =>
  withSandbox(async ({ sb }) => {
    const before = { env: { FOO: 'bar', CLAUDE_CODE_AUTO_COMPACT_WINDOW: '300000' }, model: 'opus' };
    const text = JSON.stringify(before, null, 2) + '\n';
    writeSettingsFile(sb, text);

    const refused = await runCli(sb, ['lean', 'on']);
    assert.strictEqual(refused.code, 1, refused.output);
    assert.match(refused.stderr, /already set to "300000" in settings\.json/);
    assert.strictEqual(fs.readFileSync(sb.settings, 'utf8'), text, 'a refused lean on changes nothing');
    assert.strictEqual(fs.existsSync(leanRecordFile(sb)), false);

    const forced = await runCli(sb, ['lean', 'on', '--force']);
    assert.strictEqual(forced.code, 0, forced.output);
    assert.strictEqual(readSettings(sb).env.CLAUDE_CODE_AUTO_COMPACT_WINDOW, '200000');

    const off = await runCli(sb, ['lean', 'off']);
    assert.strictEqual(off.code, 0, off.output);
    assert.deepStrictEqual(readSettings(sb), before, 'lean off restores the value the user had');
  }));

test('a window the user already set to 200,000 is kept by lean off', () =>
  withSandbox(async ({ sb }) => {
    writeSettingsFile(sb, JSON.stringify({ env: { CLAUDE_CODE_AUTO_COMPACT_WINDOW: '200000', FOO: 'x' } }, null, 2) + '\n');
    assert.strictEqual((await runCli(sb, ['lean', 'on'])).code, 0);
    assert.strictEqual((await runCli(sb, ['lean', 'off'])).code, 0);
    assert.strictEqual(readSettings(sb).env.CLAUDE_CODE_AUTO_COMPACT_WINDOW, '200000');
    assert.strictEqual(readSettings(sb).env.FOO, 'x');
  }));

test('a repeat lean on or lean off changes nothing', () =>
  withSandbox(async ({ sb }) => {
    assert.strictEqual((await runCli(sb, ['lean', 'on'])).code, 0);
    const bytes = fs.readFileSync(sb.settings, 'utf8');
    const again = await runCli(sb, ['lean', 'on']);
    assert.strictEqual(again.code, 0, again.output);
    assert.match(again.stdout, /already on/);
    assert.strictEqual(fs.readFileSync(sb.settings, 'utf8'), bytes);

    assert.strictEqual((await runCli(sb, ['lean', 'off'])).code, 0);
    const offAgain = await runCli(sb, ['lean', 'off']);
    assert.strictEqual(offAgain.code, 0, offAgain.output);
    assert.match(offAgain.stdout, /Nothing to restore/);
    assert.strictEqual(fs.existsSync(sb.settings), false);
  }));

test('lean on and lean off keep npx\'s codetab.json in step, and leave the rest of it alone', () =>
  withSandbox(async ({ sb }) => {
    const file = writeNpxCodeTab(sb);
    const original = fs.readFileSync(file);

    const on = await runCli(sb, ['lean', 'on']);
    assert.strictEqual(on.code, 0, on.output);
    assert.deepStrictEqual(readJson(file), {
      v: 1,
      owner: 'npx',
      enabled: true,
      base_url: 'http://127.0.0.1:18793',
      health_port: 18793,
      env: { ENABLE_TOOL_SEARCH: 'true', CLAUDE_CODE_AUTO_COMPACT_WINDOW: '200000' },
    });

    const off = await runCli(sb, ['lean', 'off']);
    assert.strictEqual(off.code, 0, off.output);
    assert.ok(fs.readFileSync(file).equals(original), 'codetab.json is back to the bytes npx wrote');
  }));

test('lean on leaves the desktop app\'s codetab.json alone', () =>
  withSandbox(async ({ sb }) => {
    const file = codeTabFile(sb);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, DESKTOP_CODE_TAB);

    const on = await runCli(sb, ['lean', 'on']);
    assert.strictEqual(on.code, 0, on.output);
    assert.match(on.stdout, /Desktop Code tab is managed by the ContextShrink desktop app, so it was left as it is\./);
    assert.ok(fs.readFileSync(file).equals(DESKTOP_CODE_TAB), 'the desktop app\'s codetab.json was touched');
    assert.strictEqual(readSettings(sb).env.CLAUDE_CODE_AUTO_COMPACT_WINDOW, '200000');

    const off = await runCli(sb, ['lean', 'off']);
    assert.strictEqual(off.code, 0, off.output);
    assert.ok(fs.readFileSync(file).equals(DESKTOP_CODE_TAB), 'lean off touched the desktop app\'s codetab.json');
  }));

test('lean on is refused while the desktop app connects Claude Code, and changes nothing', () =>
  withSandbox(async ({ sb }) => {
    fs.writeFileSync(sb.desktopState, '{}\n');
    const res = await runCli(sb, ['lean', 'on']);
    assert.strictEqual(res.code, 1, res.output);
    assert.match(res.stderr, /desktop app connects Claude Code/);
    assert.strictEqual(fs.existsSync(sb.settings), false);
    assert.strictEqual(fs.existsSync(leanRecordFile(sb)), false);
  }));

test('a value changed after lean on is left alone by lean off, and lean on asks before replacing it', () =>
  withSandbox(async ({ sb }) => {
    assert.strictEqual((await runCli(sb, ['lean', 'on'])).code, 0);
    const settings = readSettings(sb);
    settings.env.CLAUDE_CODE_AUTO_COMPACT_WINDOW = '300000';
    writeSettingsFile(sb, JSON.stringify(settings, null, 2) + '\n');

    const again = await runCli(sb, ['lean', 'on']);
    assert.strictEqual(again.code, 1, again.output);
    assert.match(again.stderr, /changed since it was turned on/);

    const off = await runCli(sb, ['lean', 'off']);
    assert.strictEqual(off.code, 0, off.output);
    assert.match(off.stdout, /changed in settings\.json since keep sessions lean was turned on, so it was left alone/);
    assert.strictEqual(readSettings(sb).env.CLAUDE_CODE_AUTO_COMPACT_WINDOW, '300000');
    assert.strictEqual(fs.existsSync(leanRecordFile(sb)), false);
  }));

test('uninstall removes keep sessions lean from settings.json and the Code tab', () =>
  withSandbox(async ({ sb }) => {
    writeSettingsFile(sb, JSON.stringify({ model: 'opus' }, null, 2) + '\n');
    const file = writeNpxCodeTab(sb);
    assert.strictEqual((await runCli(sb, ['lean', 'on'])).code, 0);
    assert.strictEqual(readSettings(sb).env.CLAUDE_CODE_AUTO_COMPACT_WINDOW, '200000');

    const res = await runCli(sb, ['uninstall']);
    assert.strictEqual(res.code, 0, res.output);
    assert.deepStrictEqual(readSettings(sb), { model: 'opus' }, 'settings.json is back to what it was');
    assert.strictEqual(fs.existsSync(leanRecordFile(sb)), false, 'the lean record was removed');
    assert.strictEqual(fs.existsSync(file), false, 'npx\'s codetab.json was removed with the Code tab');
  }));

test('a broken lean record is refused, and nothing is changed', () =>
  withSandbox(async ({ sb }) => {
    fs.writeFileSync(leanRecordFile(sb), '{ not json');
    const res = await runCli(sb, ['lean', 'on']);
    assert.strictEqual(res.code, 1, res.output);
    assert.match(res.stderr, /not valid JSON/);
    assert.strictEqual(fs.existsSync(sb.settings), false);
  }));

test('status shows the keep sessions lean line', () =>
  withSandbox(async ({ sb }) => {
    const off = await runCli(sb, ['status']);
    assert.match(off.stdout, /^Keep sessions lean: off \(Claude Code's default window\)$/m, off.output);
    assert.strictEqual((await runCli(sb, ['lean', 'on'])).code, 0);
    const on = await runCli(sb, ['status']);
    assert.match(on.stdout, /^Keep sessions lean: on \(Claude Code compacts at 200,000 tokens\)$/m, on.output);
  }));

test('lean needs on, off or status, and --force only applies to lean on', () =>
  withSandbox(async ({ sb }) => {
    const bare = await runCli(sb, ['lean']);
    assert.strictEqual(bare.code, 2, bare.output);
    assert.match(bare.stderr, /lean needs one of: on, off, status/);

    const unknown = await runCli(sb, ['lean', 'toggle']);
    assert.strictEqual(unknown.code, 2, unknown.output);

    const force = await runCli(sb, ['lean', 'off', '--force']);
    assert.strictEqual(force.code, 2, force.output);
    assert.match(force.stderr, /--force only applies to connect and lean on\./);

    assert.strictEqual(fs.existsSync(sb.settings), false, 'a usage error changed nothing');
  }));
