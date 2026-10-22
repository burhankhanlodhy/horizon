'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const chats = require('../lib/chats');
const { ID } = require('../lib/toml');
let DatabaseSync;
try { ({ DatabaseSync } = require('node:sqlite')); } catch {}
const OLD = '00000000-0000-4000-8000-000000000001';
const SECOND = '00000000-0000-4000-8000-000000000002';
const options = { all: true, apply: true };
const closed = { assertClosed() {} };
function fixture(fn) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cs-chat-test-'));
  const sessions = path.join(dir, 'sessions', '2026', '10', '20'); fs.mkdirSync(sessions, { recursive: true });
  const dbPath = path.join(dir, 'state_5.sqlite');
  const db = new DatabaseSync(dbPath);
  db.exec('CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, model_provider TEXT, archived INTEGER, title TEXT, project_id TEXT, tokens_used INTEGER, updated_at INTEGER, is_pinned INTEGER); CREATE TABLE unrelated (id TEXT, value TEXT); INSERT INTO unrelated VALUES (\'keep\', \'unchanged\');');
  db.close();
  const paths = { dir }, originals = new Map();
  function add(id, provider = 'openai', archived = 0) {
    const file = path.join(sessions, id + '.jsonl');
    const header = JSON.stringify({ timestamp: 'test', type: 'session_meta', payload: { id, model_provider: provider, history_mode: 'paginated', base_instructions: { text: 'keep instructions' }, nested: { model_provider: 'do not change' } } }) + '\r\n';
    const tail = '{"type":"response_item","payload":{"text":"history € 🙂"}}\r\n';
    fs.writeFileSync(file, header + tail); originals.set(id, Buffer.from(header + tail));
    const db = new DatabaseSync(dbPath); db.prepare('INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)').run(id, file, provider, archived, 'keep title', 'project-1', 123, 777, 1); db.close();
    return file;
  }
  const get = id => { const db = new DatabaseSync(dbPath); try { return db.prepare('SELECT * FROM threads WHERE id = ?').get(id); } finally { db.close(); } };
  try { fn({ paths, dir, dbPath, add, get, originals }); }
  finally { fs.rmSync(dir, { recursive: true, force: true }); }
}
const check = (name, fn) => test(name, { skip: !DatabaseSync && 'requires Node 22.13+' }, () => fixture(fn));
check('dry run does not write a journal, headers, or SQLite rows', f => {
  const file = f.add(OLD); const bytes = fs.readFileSync(f.dbPath);
  assert.deepEqual(chats.migrate(f.paths, { all: true, apply: false }), [OLD]);
  assert.deepEqual(fs.readFileSync(file), f.originals.get(OLD));
  assert.deepEqual(fs.readFileSync(f.dbPath), bytes);
  assert.ok(!fs.existsSync(chats.journalPath(f.paths)));
});
check('migration updates only the provider and restores exact original bytes', f => {
  const file = f.add(OLD), beforeRow = f.get(OLD);
  assert.deepEqual(chats.migrate(f.paths, options, closed), [OLD]);
  assert.equal(f.get(OLD).model_provider, ID);
  assert.deepEqual({ ...f.get(OLD), model_provider: 'openai' }, { ...beforeRow });
  const next = fs.readFileSync(file), old = f.originals.get(OLD);
  assert.deepEqual(next.subarray(next.indexOf(10)+1), old.subarray(old.indexOf(10)+1));
  assert.equal(JSON.parse(next.toString().split('\n')[0]).payload.nested.model_provider, 'do not change');
  assert.deepEqual(chats.restore(f.paths, options, closed), [OLD]);
  assert.deepEqual(fs.readFileSync(file), old); assert.equal(f.get(OLD).model_provider, 'openai');
  assert.ok(!fs.existsSync(chats.journalPath(f.paths)));
});
check('restoration preserves turns and metadata added after migration', f => {
  const file = f.add(OLD); chats.migrate(f.paths, options, closed);
  const appended = Buffer.from('{"type":"event_msg","payload":{"message":"new turn"}}\n');
  fs.appendFileSync(file, appended);
  const db = new DatabaseSync(f.dbPath); db.prepare('UPDATE threads SET title = ?, tokens_used = ?, updated_at = ? WHERE id = ?').run('new title', 999, 888, OLD); db.close();
  chats.restore(f.paths, options, closed);
  assert.deepEqual(fs.readFileSync(file), Buffer.concat([f.originals.get(OLD), appended]));
  assert.equal(f.get(OLD).title, 'new title'); assert.equal(f.get(OLD).tokens_used, 999); assert.equal(f.get(OLD).updated_at, 888);
});
check('bulk migration excludes other providers and archived chats by default', f => {
  f.add(OLD); f.add(SECOND, 'mine'); const archive = '00000000-0000-4000-8000-000000000003'; f.add(archive, 'openai', 1);
  assert.deepEqual(chats.migrate(f.paths, options, closed), [OLD]);
  assert.equal(f.get(SECOND).model_provider, 'mine'); assert.equal(f.get(archive).model_provider, 'openai');
  assert.deepEqual(chats.migrate(f.paths, { ids: [archive], apply: true }, closed), [archive]);
});
check('busy Codex refuses changes before creating any restoration state', f => {
  const file = f.add(OLD);
  assert.throws(() => chats.migrate(f.paths, options, { assertClosed() { throw new Error('busy'); } }), /busy/);
  assert.deepEqual(fs.readFileSync(file), f.originals.get(OLD)); assert.ok(!fs.existsSync(chats.journalPath(f.paths)));
});
check('mixed selection with a third-party provider refuses the entire operation', f => {
  const file = f.add(OLD); f.add(SECOND, 'third_party');
  assert.throws(() => chats.migrate(f.paths, { ids: [OLD, SECOND], apply: true }, closed), /another provider/);
  assert.deepEqual(fs.readFileSync(file), f.originals.get(OLD)); assert.equal(f.get(OLD).model_provider, 'openai');
});
check('a failure on the second header rolls the first file and database back', f => {
  const a = f.add(OLD), b = f.add(SECOND); let calls = 0;
  assert.throws(() => chats.migrate(f.paths, options, { ...closed, replaceHeader(...args) { if (++calls === 2) throw new Error('disk fixture'); return chats.replaceHeader(...args); } }), /disk fixture/);
  assert.deepEqual(fs.readFileSync(a), f.originals.get(OLD)); assert.deepEqual(fs.readFileSync(b), f.originals.get(SECOND));
  assert.equal(f.get(OLD).model_provider, 'openai'); assert.ok(!fs.existsSync(chats.journalPath(f.paths)));
});
check('restore recovers interruption between a file replacement and SQLite commit', f => {
  f.add(OLD); chats.migrate(f.paths, options, closed);
  const db = new DatabaseSync(f.dbPath); db.prepare('UPDATE threads SET model_provider = ? WHERE id = ?').run('openai', OLD); db.close();
  chats.restore(f.paths, options, closed);
  assert.equal(f.get(OLD).model_provider, 'openai'); assert.ok(!fs.existsSync(chats.journalPath(f.paths)));
});
check('restore refuses edited metadata without overwriting it or another selected chat', f => {
  const file = f.add(OLD), second = f.add(SECOND); chats.migrate(f.paths, options, closed);
  const changed = fs.readFileSync(second, 'utf8').replace('keep instructions', 'edited instructions'); fs.writeFileSync(second, changed);
  const before = fs.readFileSync(file);
  assert.throws(() => chats.restore(f.paths, options, closed), /changed or was removed/);
  assert.deepEqual(fs.readFileSync(file), before); assert.equal(fs.readFileSync(second, 'utf8'), changed);
  assert.ok(fs.existsSync(chats.journalPath(f.paths)));
});
check('path escape in database is refused before reading or modifying the target', f => {
  f.add(OLD); const db = new DatabaseSync(f.dbPath); db.prepare('UPDATE threads SET rollout_path = ? WHERE id = ?').run(path.resolve(f.dir, '..', 'outside.jsonl'), OLD); db.close();
  assert.throws(() => chats.migrate(f.paths, options, closed), /outside/); assert.ok(!fs.existsSync(chats.journalPath(f.paths)));
});
check('metadata ID mismatch refuses migration', f => {
  const file = f.add(OLD); fs.writeFileSync(file, fs.readFileSync(file, 'utf8').replace(OLD, SECOND));
  assert.throws(() => chats.migrate(f.paths, options, closed), /mismatched chat ID/);
});
check('a future database version prevents edits to a stale state_5 database', f => {
  const file = f.add(OLD); fs.writeFileSync(path.join(f.dir, 'state_6.sqlite'), 'future fixture');
  assert.throws(() => chats.migrate(f.paths, options, closed), /newer Codex database/);
  assert.deepEqual(fs.readFileSync(file), f.originals.get(OLD));
});
check('partial restore keeps other restoration entries and can finish later', f => {
  f.add(OLD); f.add(SECOND); chats.migrate(f.paths, options, closed);
  chats.restore(f.paths, { ids: [OLD], apply: true }, closed);
  assert.equal(f.get(OLD).model_provider, 'openai'); assert.equal(f.get(SECOND).model_provider, ID);
  assert.equal(chats.hasMigrations(f.paths), true); chats.restore(f.paths, options, closed); assert.equal(chats.hasMigrations(f.paths), false);
});
check('restoration follows a chat moved into the archive without undoing the archive', f => {
  const file = f.add(OLD); chats.migrate(f.paths, options, closed);
  const folder = path.join(f.dir, 'archived_sessions'); fs.mkdirSync(folder);
  const moved = path.join(folder, path.basename(file)); fs.renameSync(file, moved);
  const db = new DatabaseSync(f.dbPath); db.prepare('UPDATE threads SET rollout_path = ?, archived = 1 WHERE id = ?').run(moved, OLD); db.close();
  chats.restore(f.paths, options, closed);
  assert.deepEqual(fs.readFileSync(moved), f.originals.get(OLD)); assert.equal(f.get(OLD).archived, 1);
});
check('a failure after a header replacement still restores that header', f => {
  const file = f.add(OLD);
  assert.throws(() => chats.migrate(f.paths, options, { ...closed, replaceHeader(...args) { chats.replaceHeader(...args); throw new Error('after-write fixture'); } }), /after-write fixture/);
  assert.deepEqual(fs.readFileSync(file), f.originals.get(OLD)); assert.equal(f.get(OLD).model_provider, 'openai');
  assert.equal(chats.hasMigrations(f.paths), false);
});
test('disconnect restores migrated chats before removing the provider configuration', { skip: !DatabaseSync }, async () => {
  const { connectText } = require('../lib/toml');
  const { run } = require('../lib/cli');
  const files = require('../lib/files');
  let pending;
  // Retain the fixture until the asynchronous CLI has finished.
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'cs-disconnect-test-'));
  const dir = path.join(root, '.codex'); fs.mkdirSync(path.join(dir, 'sessions'), { recursive: true });
  const file = path.join(dir, 'sessions', OLD + '.jsonl');
  const before = JSON.stringify({ type: 'session_meta', payload: { id: OLD, model_provider: 'openai' } }) + '\n{"type":"fixture_history"}\n';
  fs.writeFileSync(file, before);
  const db = new DatabaseSync(path.join(dir, 'state_5.sqlite'));
  db.exec('CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, model_provider TEXT, archived INTEGER)');
  db.prepare('INSERT INTO threads VALUES (?, ?, ?, 0)').run(OLD, file, 'openai'); db.close();
  const original = 'model="keep-model"\n', change = connectText(original, 'https://proxy.contextshrink.com/k/abcdefghijklmnopqrstuvwx/v1', 'cs_live_fakeabcdefghijklmnopqrstuv');
  files.writePrivateJson(path.join(dir, '.contextshrink-npx-codex.json'), { version: 2, configExisted: true, ...change, device_key_id: null });
  fs.writeFileSync(path.join(dir, 'config.toml'), change.written);
  chats.migrate({ dir }, options, closed);
  const realRestore = chats.restore;
  chats.restore = (paths, opts) => realRestore(paths, opts, closed);
  try {
    let output = '';
    pending = run(['disconnect'], { env: { USERPROFILE: root, HOME: root, CODEX_HOME: dir },
      stdout: { write(s) { output += s; } }, stderr: { write(s) { output += s; } }, prompter: { ask() { throw new Error('Unexpected prompt'); } } });
    assert.equal(await pending, 0, output); assert.match(output, /Restored original providers for 1/);
    assert.equal(fs.readFileSync(file, 'utf8'), before); assert.equal(fs.readFileSync(path.join(dir, 'config.toml'), 'utf8'), original);
  } finally { chats.restore = realRestore; fs.rmSync(root, { recursive: true, force: true }); }
});
