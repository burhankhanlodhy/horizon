'use strict';
// Offline migration of local provider metadata. Conversation bodies are copied
// byte for byte; SQLite updates touch only threads.model_provider.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');
const { isDeepStrictEqual } = require('node:util');
const files = require('./files');
const { ConfigError, ID } = require('./toml');
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const DATABASE = 'state_5.sqlite';
const journalPath = paths => path.join(paths.dir, '.contextshrink-npx-codex-chats.json');
function storagePath(value) {
  if (typeof value !== 'string' || value.includes('\0')) throw new ConfigError('Invalid stored rollout path. Nothing was changed.');
  // Codex canonicalizes Windows rollout paths to the extended-length spelling
  // after resuming. Normalize its drive/UNC prefixes before containment checks.
  if (process.platform === 'win32') {
    if (/^\\\\\?\\[a-z]:\\/i.test(value)) return value.slice(4);
    if (/^\\\\\?\\UNC\\/i.test(value)) return '\\\\' + value.slice(8);
  }
  return value;
}
function relativeRollout(paths, file) { return path.relative(storagePath(paths.dir), storagePath(file)); }
function sqlite() {
  try { return require('node:sqlite').DatabaseSync; }
  catch { throw new ConfigError('Existing-chat commands require Node.js 22.13 or newer. Connect and savings still work on Node 18+.'); }
}
function inside(root, file) {
  const rel = path.relative(storagePath(root), storagePath(file));
  return rel !== '' && !rel.startsWith('..' + path.sep) && rel !== '..' && !path.isAbsolute(rel);
}
function relativeTarget(paths, relative) {
  if (typeof relative !== 'string' || relative.includes('\0') || path.isAbsolute(relative)) throw new ConfigError('Unsafe chat path; nothing was changed.');
  const absolute = path.resolve(paths.dir, relative);
  const parts = path.relative(paths.dir, absolute).split(path.sep);
  if (!['sessions', 'archived_sessions'].includes(parts[0]) || !inside(paths.dir, absolute)) throw new ConfigError('Chat is outside the local session folders; nothing was changed.');
  return absolute;
}
function checkedPath(paths, relative) {
  const root = fs.realpathSync(paths.dir);
  const absolute = relativeTarget(paths, relative);
  const real = fs.realpathSync(absolute);
  if (!inside(root, real) || fs.lstatSync(absolute).isSymbolicLink()) throw new ConfigError('Linked chat files are not migrated.');
  return absolute;
}
function openDb(paths, write) {
  const DatabaseSync = sqlite();
  const dbFile = path.join(paths.dir, DATABASE);
  if (fs.existsSync(paths.dir) && fs.readdirSync(paths.dir).some(name => /^state_(\d+)\.sqlite$/.test(name) && Number(name.match(/^state_(\d+)/)[1]) > 5)) {
    throw new ConfigError('A newer Codex database version is present. This preview will not edit it or a stale older database.');
  }
  if (!fs.existsSync(dbFile)) throw new ConfigError('No supported Codex state_5.sqlite database. Open Codex and create a local chat first.');
  if (fs.lstatSync(dbFile).isSymbolicLink() || !inside(fs.realpathSync(paths.dir), fs.realpathSync(dbFile))) throw new ConfigError('Linked Codex databases are not supported.');
  const db = new DatabaseSync(dbFile, { readOnly: !write });
  try {
    db.exec('PRAGMA busy_timeout = 250');
    const columns = db.prepare('PRAGMA table_info(threads)').all();
    for (const key of ['id', 'rollout_path', 'model_provider', 'archived']) {
      if (!columns.some(c => c.name === key)) throw new ConfigError('Unsupported Codex thread database schema. Nothing was changed.');
    }
    return db;
  } catch (e) { db.close(); throw e; }
}
function assertClosed() {
  let names;
  try {
    if (process.platform === 'win32') {
      const ps = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
      names = execFileSync(ps, ['-NoProfile', '-NonInteractive', '-Command', 'Get-Process -ErrorAction Stop | Select-Object -ExpandProperty ProcessName'], { encoding: 'utf8', windowsHide: true, timeout: 10000 });
    } else names = execFileSync('ps', ['-A', '-o', 'comm='], { encoding: 'utf8', timeout: 10000 });
  } catch { throw new ConfigError('Could not check whether Codex is closed. No chats were changed.'); }
  if (names.split(/\r?\n/).some(n => /^(?:codex|chatgpt)(?:\.exe| helper.*)?$/i.test(path.basename(n.trim())))) {
    throw new ConfigError('Close Codex/ChatGPT Desktop and every Codex CLI session before changing existing chats. No chats were changed.');
  }
}
function firstLine(file) {
  const fd = fs.openSync(file, 'r');
  try {
    const buf = Buffer.alloc(Math.min(fs.fstatSync(fd).size, 4 * 1024 * 1024));
    fs.readSync(fd, buf, 0, buf.length, 0);
    const end = buf.indexOf(10);
    if (end < 0) throw new ConfigError('Chat metadata is missing or too large; nothing was changed.');
    return buf.subarray(0, end + 1);
  } finally { fs.closeSync(fd); }
}
function metadata(header, id) {
  let parsed;
  try { parsed = JSON.parse(header.toString('utf8').replace(/^\uFEFF/, '')); } catch {}
  if (parsed?.type !== 'session_meta' || parsed.payload?.id !== id || typeof parsed.payload.model_provider !== 'string') {
    throw new ConfigError('Unsupported chat metadata or mismatched chat ID. Nothing was changed.');
  }
  return parsed;
}
function changedHeader(before, id) {
  const meta = metadata(before, id);
  if (meta.payload.model_provider !== 'openai') throw new ConfigError('Only chats using the built-in OpenAI provider can be migrated.');
  meta.payload.model_provider = ID;
  const newline = before.subarray(-2).equals(Buffer.from('\r\n')) ? '\r\n' : '\n';
  return Buffer.from((before[0] === 0xef ? '\uFEFF' : '') + JSON.stringify(meta) + newline);
}
function journal(paths) {
  const data = files.readOwnJson(journalPath(paths), 'Chat restoration journal');
  if (!data) return { version: 1, database: DATABASE, entries: [] };
  if (data.version !== 1 || data.database !== DATABASE || !Array.isArray(data.entries)) throw new ConfigError('Unsupported chat restoration journal. It was kept unchanged.');
  const ids = new Set();
  for (const entry of data.entries) {
    if (!UUID.test(entry.id) || ids.has(entry.id) || typeof entry.relative !== 'string' ||
        !['before', 'after'].every(k => typeof entry[k] === 'string' && entry[k].length < 6 * 1024 * 1024)) throw new ConfigError('Invalid chat restoration journal. It was kept unchanged.');
    ids.add(entry.id);
    const before = Buffer.from(entry.before, 'base64'), after = Buffer.from(entry.after, 'base64');
    const original = metadata(before, entry.id), next = metadata(after, entry.id);
    if (original.payload.model_provider !== 'openai' || next.payload.model_provider !== ID) throw new ConfigError('Unexpected saved chat provider. Journal was kept unchanged.');
    next.payload.model_provider = 'openai';
    if (!isDeepStrictEqual(original, next)) throw new ConfigError('Unexpected changes in saved chat metadata. Journal was kept unchanged.');
    relativeTarget(paths, entry.relative);
  }
  return data;
}
function saveJournal(paths, data) {
  if (data.entries.length) {
    files.writePrivateJson(journalPath(paths), data);
    const fd = fs.openSync(journalPath(paths), 'r+');
    try { fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
  }
  else files.removeFile(journalPath(paths));
}
function hasMigrations(paths) { return journal(paths).entries.length > 0; }
function rows(db) { return db.prepare('SELECT id, rollout_path, model_provider, archived FROM threads ORDER BY id').all(); }
function list(paths) {
  const db = openDb(paths, false);
  try { return rows(db).map(r => ({ id: r.id, provider: r.model_provider, archived: Boolean(r.archived) })); }
  finally { db.close(); }
}
function replaceHeader(file, expected, replacement) {
  // Atomic replace, streaming the untouched body, including any turns appended
  // after migration. Never replace the whole database or restore an old body.
  if (!firstLine(file).equals(expected)) throw new ConfigError('Chat metadata changed during the operation. Recovery journal was kept.');
  const stat = fs.statSync(file);
  const temporary = file + '.contextshrink-' + crypto.randomBytes(8).toString('hex') + '.tmp';
  let input, output;
  try {
    input = fs.openSync(file, 'r'); output = fs.openSync(temporary, 'wx', stat.mode & 0o777);
    fs.writeFileSync(output, replacement);
    const chunk = Buffer.alloc(64 * 1024);
    let offset = expected.length, count;
    while ((count = fs.readSync(input, chunk, 0, chunk.length, offset)) > 0) { fs.writeFileSync(output, chunk.subarray(0, count)); offset += count; }
    fs.fsyncSync(output); fs.closeSync(input); input = undefined; fs.closeSync(output); output = undefined;
    const current = fs.statSync(file);
    if (current.size !== stat.size || current.mtimeMs !== stat.mtimeMs || !firstLine(file).equals(expected)) throw new ConfigError('Chat changed while it was being copied. Close Codex and retry.');
    fs.renameSync(temporary, file);
    fs.utimesSync(file, stat.atime, stat.mtime);
  } finally {
    if (input !== undefined) fs.closeSync(input);
    if (output !== undefined) fs.closeSync(output);
    fs.rmSync(temporary, { force: true });
  }
}
function selectIds(allRows, { ids, all, includeArchived }, restoring) {
  if (!all && (!ids?.length || ids.some(id => !UUID.test(id)))) throw new ConfigError('Select chat IDs, or use --all. Copy an ID from chats list.');
  if (all && ids?.length) throw new ConfigError('Choose IDs or --all, not both.');
  const selection = allRows.filter(r => all ? (restoring || (r.model_provider === 'openai' && (includeArchived || !r.archived))) : ids.includes(r.id));
  if (!all && selection.length !== new Set(ids).size) throw new ConfigError('One or more selected chats were not found. Nothing was changed.');
  return selection;
}
function migrate(paths, options, hooks = {}) {
  if (options.apply) (hooks.assertClosed || assertClosed)();
  const saved = journal(paths);
  const db = openDb(paths, Boolean(options.apply));
  let transaction = false, prepared = [], journalWritten = false;
  try {
    if (options.apply) { db.exec('BEGIN EXCLUSIVE'); transaction = true; }
    const selection = selectIds(rows(db), options, false);
    for (const row of selection) {
      if (!UUID.test(row.id)) throw new ConfigError('Unsupported local chat ID. Nothing was changed.');
      if (saved.entries.some(e => e.id === row.id)) throw new ConfigError('A selected chat already has a restoration entry. Restore it before migrating again.');
      if (row.model_provider !== 'openai') throw new ConfigError('A selected chat uses another provider; nothing was changed.');
      const relative = relativeRollout(paths, row.rollout_path);
      const file = checkedPath(paths, relative), before = firstLine(file), after = changedHeader(before, row.id);
      prepared.push({ id: row.id, relative, before: before.toString('base64'), after: after.toString('base64') });
    }
    if (!options.apply) return prepared.map(e => e.id);
    if (!prepared.length) { db.exec('ROLLBACK'); transaction = false; return []; }
    // Write-ahead journal survives interruption between the file and DB update.
    saveJournal(paths, { ...saved, entries: [...saved.entries, ...prepared] });
    journalWritten = true;
    for (const entry of prepared) {
      (hooks.replaceHeader || replaceHeader)(checkedPath(paths, entry.relative), Buffer.from(entry.before, 'base64'), Buffer.from(entry.after, 'base64'));
      const result = db.prepare('UPDATE threads SET model_provider = ? WHERE id = ? AND model_provider = ?').run(ID, entry.id, 'openai');
      if (result.changes !== 1) throw new ConfigError('Chat provider changed during migration. Recovery journal was kept.');
    }
    db.exec('COMMIT'); transaction = false;
    return prepared.map(e => e.id);
  } catch (e) {
    if (transaction) { try { db.exec('ROLLBACK'); } catch {} }
    // Best-effort rollback of our own exact headers only. The journal remains if
    // interrupted or recovery fails, so chats restore --all can finish later.
    let recovered = true;
    if (journalWritten) for (const entry of [...prepared].reverse()) {
      try {
        if (db.prepare('SELECT model_provider FROM threads WHERE id = ?').get(entry.id)?.model_provider !== 'openai') { recovered = false; continue; }
        const file = checkedPath(paths, entry.relative), current = firstLine(file);
        const before = Buffer.from(entry.before, 'base64'), after = Buffer.from(entry.after, 'base64');
        if (current.equals(after)) replaceHeader(file, after, before);
        else if (!current.equals(before)) recovered = false;
      } catch { recovered = false; }
    }
    if (journalWritten && recovered) saveJournal(paths, saved);
    if (!recovered) throw new ConfigError('Migration stopped with restoration data saved. Keep Codex closed and run chats restore --all --apply to recover.');
    throw e;
  } finally { db.close(); }
}
function restore(paths, options = { all: true, apply: true }, hooks = {}) {
  const saved = journal(paths);
  if (!saved.entries.length) return [];
  if (options.apply) (hooks.assertClosed || assertClosed)();
  const entries = selectIds(saved.entries, options, true);
  const db = openDb(paths, Boolean(options.apply));
  let transaction = false;
  const targets = new Map();
  try {
    if (options.apply) { db.exec('BEGIN EXCLUSIVE'); transaction = true; }
    // Validate every selected header and row before changing the first one.
    for (const e of entries) {
      const row = db.prepare('SELECT model_provider, rollout_path FROM threads WHERE id = ?').get(e.id);
      if (!row) throw new ConfigError('A migrated chat was removed. Journal was kept for review.');
      // Codex may move a migrated rollout into archived_sessions. Follow its
      // current database path only inside CODEX_HOME and verify the exact header.
      const file = checkedPath(paths, relativeRollout(paths, row.rollout_path));
      const header = firstLine(file); targets.set(e.id, file);
      if (!row || !['openai', ID].includes(row.model_provider) ||
          ![e.before, e.after].some(b => header.equals(Buffer.from(b, 'base64')))) throw new ConfigError('A migrated chat changed or was removed. No restoration was attempted; journal was kept.');
    }
    if (!options.apply) return entries.map(e => e.id);
    for (const e of entries) {
      const file = targets.get(e.id), header = firstLine(file);
      if (header.equals(Buffer.from(e.after, 'base64'))) (hooks.replaceHeader || replaceHeader)(file, header, Buffer.from(e.before, 'base64'));
      db.prepare('UPDATE threads SET model_provider = ? WHERE id = ?').run('openai', e.id);
    }
    db.exec('COMMIT'); transaction = false;
    saveJournal(paths, { ...saved, entries: saved.entries.filter(e => !entries.includes(e)) });
    return entries.map(e => e.id);
  } catch (e) {
    if (transaction) { try { db.exec('ROLLBACK'); } catch {} }
    throw e;
  } finally { db.close(); }
}
module.exports = { list, migrate, restore, hasMigrations, assertClosed, replaceHeader, journalPath };
