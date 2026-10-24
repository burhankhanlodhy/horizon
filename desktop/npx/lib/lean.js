'use strict';

// Keep sessions lean. Claude Code on a 1M-context model only auto-compacts near 967k
// tokens, so a long session resends a very large context on every turn. `on` sets
// CLAUDE_CODE_AUTO_COMPACT_WINDOW to 200000, so long sessions are summarised instead.
//
// Where the value goes:
//   settings.json env   the terminal's Claude Code, and any Claude Code that reads user settings
//   codetab.json env    the Claude Desktop Code tab, only while npx owns that file
// The desktop app's codetab.json is never changed; the app keeps its own lean setting.
//
// The record of what `on` changed is .contextshrink-lean.json next to settings.json. It is
// separate from .contextshrink-npx.json, so lean works without connect, and uninstall removes
// it even when connect's state is gone. Each key keeps its value from before `on` (or its
// absence), and `off` restores exactly that, only while the key still holds the value `on` wrote.

const fs = require('node:fs');
const cfg = require('./config');
const files = require('./files');
const codetab = require('./codetab');
const { CsError } = require('./code-env');

const { hasOwn, isPlainObject } = files;
const KEY = cfg.LEAN_KEY;
const WINDOW = cfg.LEAN_VALUE;
const RECORD_VERSION = 1;
const RECORD_LABEL = 'Keep sessions lean record';
const STATUS_ON = 'Keep sessions lean: on (Claude Code compacts at 200,000 tokens)';
const STATUS_OFF = "Keep sessions lean: off (Claude Code's default window)";
const RESTART = 'Start a new Claude Code session (or restart Claude Desktop) for this to apply.';
const SUMMARISED = 'A session already longer than 200,000 tokens is summarised on its next turn.';
const DESKTOP_CODE_TAB =
  "The Desktop Code tab is managed by the ContextShrink desktop app, so it was left as it is.";

function entryOf(block, key) {
  return block && hasOwn(block, key) ? { present: true, value: block[key] } : { present: false };
}

function isWindow(entry) {
  return entry.present && entry.value === WINDOW;
}

// The env block of settings.json. Undefined when the file has no env yet.
function envBlockOf(payload, settingsFile) {
  const block = payload.env;
  if (block === undefined) return undefined;
  if (!isPlainObject(block)) {
    throw new CsError(`"env" in ${settingsFile} is not an object, so it was not touched.`);
  }
  return block;
}

// A previous value is { present: true, value } or { present: false }.
function validEntry(entry) {
  if (!isPlainObject(entry) || typeof entry.present !== 'boolean') return false;
  return entry.present ? hasOwn(entry, 'value') : !hasOwn(entry, 'value');
}

// The lean record, or null when lean is off. Throws when the file is there but not usable.
function readRecord(file) {
  const record = files.readOwnJson(file, RECORD_LABEL);
  if (record === null) return null;
  const ok =
    record.v === RECORD_VERSION &&
    record.window === WINDOW &&
    typeof record.settings_existed === 'boolean' &&
    typeof record.env_existed === 'boolean' &&
    validEntry(record.previous) &&
    isPlainObject(record.code_tab) &&
    validEntry(record.code_tab.previous);
  if (!ok) throw new CsError(`${file} is not in a format this version knows. Nothing was changed.`);
  return record;
}

// The Code tab's entry, with errors shown as they are (an unreadable codetab.json is not touched).
function codeTabEntry(env) {
  try {
    return codetab.leanEntryOf(env);
  } catch (err) {
    if (err instanceof codetab.CodeTabError) throw new CsError(err.message);
    throw err;
  }
}

function setCodeTabEntry(env, entry) {
  try {
    return codetab.setLeanEnv(env, entry);
  } catch (err) {
    if (err instanceof codetab.CodeTabError) throw new CsError(err.message);
    throw err;
  }
}

/** The status line for settings.json's env block. Never throws. */
function statusLine(block) {
  const current = entryOf(block || {}, KEY);
  if (isWindow(current)) return STATUS_ON;
  if (current.present) {
    return `Keep sessions lean: off (settings.json sets CLAUDE_CODE_AUTO_COMPACT_WINDOW to ${JSON.stringify(current.value)})`;
  }
  return STATUS_OFF;
}

/** The lean window connect keeps in codetab.json (text), or null when lean is off. */
function windowOf(paths) {
  const record = readRecord(paths.leanRecord);
  return record ? record.window : null;
}

function restoreEntry(block, key, entry) {
  if (entry.present) block[key] = entry.value;
  else delete block[key];
}

function restoreRecord(file, record) {
  if (record) files.writePrivateJson(file, record);
  else files.removeFile(file);
}

/**
 * `lean on`. Refuses when a value the user set differs (or, after an earlier `on`, when the
 * value changed since), unless force. Changes nothing when lean is already on.
 */
function turnOn(ctx, { force = false } = {}) {
  const env = ctx.env;
  const paths = cfg.resolvePaths(env);
  if (fs.existsSync(paths.desktopState)) {
    throw new CsError(
      'The ContextShrink desktop app connects Claude Code on this computer, so keep sessions lean is set in the app. ' +
        'Use the app to change it. Nothing was changed.'
    );
  }
  const record = readRecord(paths.leanRecord);
  const settings = files.readSettings(paths.settings);
  const block = envBlockOf(settings.payload, paths.settings);
  const work = block || {};
  const current = entryOf(work, KEY);
  const code = codeTabEntry(env);
  const codeHoldsOther = code.state === 'npx' && code.current.present && code.current.value !== WINDOW;
  const codeNeedsSync = code.state === 'npx' && !(code.current.present && code.current.value === WINDOW);

  if (!force) {
    if (record) {
      if (!isWindow(current) || codeHoldsOther) {
        throw new CsError(
          'Keep sessions lean is on, but CLAUDE_CODE_AUTO_COMPACT_WINDOW was changed since it was turned on. ' +
            'Run "npx contextshrink-claude lean off" to put back what was there, or add --force to turn it on again. ' +
            'Nothing was changed.'
        );
      }
    } else if (current.present && !isWindow(current)) {
      throw new CsError(
        `CLAUDE_CODE_AUTO_COMPACT_WINDOW is already set to ${JSON.stringify(current.value)} in settings.json. ` +
          'Remove it, or add --force to replace it (lean off puts the old value back). Nothing was changed.'
      );
    } else if (codeHoldsOther) {
      throw new CsError(
        `CLAUDE_CODE_AUTO_COMPACT_WINDOW is already set to ${JSON.stringify(code.current.value)} in the Code tab's codetab.json. ` +
          'Remove it, or add --force to replace it. Nothing was changed.'
      );
    }
  }

  if (record && isWindow(current) && !codeNeedsSync) {
    ctx.out(STATUS_ON);
    ctx.out('Nothing to change: keep sessions lean was already on.');
    return 0;
  }

  // The first `on` records what was there before; a later one keeps that record.
  const next = record
    ? { ...record }
    : {
        v: RECORD_VERSION,
        window: WINDOW,
        turned_on_at: new Date().toISOString(),
        settings_existed: settings.exists,
        env_existed: block !== undefined,
        previous: current,
        code_tab: { previous: code.current },
      };

  // The record goes first, so a failed settings write can put the old record back.
  files.writePrivateJson(paths.leanRecord, next);
  if (!isWindow(current)) {
    work[KEY] = WINDOW;
    settings.payload.env = work;
    try {
      files.writeSettings(paths.settings, settings);
    } catch (err) {
      restoreRecord(paths.leanRecord, record);
      throw err;
    }
  }
  if (codeNeedsSync) setCodeTabEntry(env, { present: true, value: WINDOW });

  ctx.out(STATUS_ON);
  if (code.state === 'desktop') ctx.out(DESKTOP_CODE_TAB);
  ctx.out(RESTART);
  ctx.out(SUMMARISED);
  return 0;
}

/**
 * `lean off`. Puts back what `on` replaced, in settings.json and in npx's codetab.json. A value
 * changed since `on` is left alone and said so. Returns 0.
 */
function turnOff(ctx) {
  const env = ctx.env;
  const paths = cfg.resolvePaths(env);
  const record = readRecord(paths.leanRecord);
  const settings = files.readSettings(paths.settings);
  const block = envBlockOf(settings.payload, paths.settings);
  if (!record) {
    if (isWindow(entryOf(block || {}, KEY))) {
      ctx.out('Keep sessions lean was not turned on by contextshrink, so the setting in settings.json was left alone.');
    } else {
      ctx.out(`${STATUS_OFF}. Nothing to restore.`);
    }
    return 0;
  }

  // Read before anything is written, so an unreadable codetab.json stops the run with nothing changed.
  const code = codeTabEntry(env);
  const notes = [];
  const work = block || {};
  if (isWindow(entryOf(work, KEY))) {
    restoreEntry(work, KEY, record.previous);
    if (!record.env_existed && Object.keys(work).length === 0) delete settings.payload.env;
    else settings.payload.env = work;
    if (!record.settings_existed && Object.keys(settings.payload).length === 0) files.removeFile(paths.settings);
    else files.writeSettings(paths.settings, settings);
  } else {
    notes.push(`${KEY} was changed in settings.json since keep sessions lean was turned on, so it was left alone.`);
  }

  if (code.state === 'npx') {
    if (code.current.present && code.current.value === WINDOW) {
      setCodeTabEntry(env, record.code_tab.previous);
    } else if (code.current.present) {
      notes.push(`${KEY} was changed in the Code tab's codetab.json since keep sessions lean was turned on, so it was left alone.`);
    }
  }

  files.removeFile(paths.leanRecord);
  ctx.out(statusLine(work));
  ctx.out(RESTART);
  for (const note of notes) ctx.out(note);
  return 0;
}

/** Puts back what lean changed, for uninstall. Returns 0, or 1 when a step failed (nothing thrown). */
function undoLean(ctx) {
  const paths = cfg.resolvePaths(ctx.env);
  if (!fs.existsSync(paths.leanRecord)) return 0;
  try {
    return turnOff(ctx);
  } catch (err) {
    ctx.err(err.message);
    return 1;
  }
}

/** `lean status`: the one status line. */
function showStatus(ctx) {
  const paths = cfg.resolvePaths(ctx.env);
  const settings = files.readSettings(paths.settings);
  ctx.out(statusLine(envBlockOf(settings.payload, paths.settings)));
  return 0;
}

module.exports = { turnOn, turnOff, undoLean, showStatus, statusLine, windowOf, STATUS_ON, STATUS_OFF };
