'use strict';

// File access for settings.json and the installer's own state files.
// Writes go to a temp file in the same folder and are renamed into place, so a
// crash never leaves a half-written settings.json.

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

class FileError extends Error {}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function hasOwn(obj, key) {
  return Object.prototype.hasOwnProperty.call(obj, key);
}

/**
 * Reads settings.json. A missing or blank file is an empty object. Invalid JSON
 * or a non-object root is an error: the caller refuses and changes nothing.
 * Returns the parsed payload plus the layout to write it back with.
 */
function readSettings(file) {
  if (!fs.existsSync(file)) {
    return { exists: false, payload: {}, indent: '  ', trailingNewline: true, bom: false };
  }
  let raw;
  try {
    raw = fs.readFileSync(file, 'utf8');
  } catch (err) {
    throw new FileError(`Cannot read ${file} (${err.code || 'read error'}). Nothing was changed.`);
  }
  const bom = raw.charCodeAt(0) === 0xfeff;
  if (bom) raw = raw.slice(1);
  if (raw.trim() === '') {
    return { exists: true, payload: {}, indent: '  ', trailingNewline: true, bom };
  }
  let payload;
  try {
    payload = JSON.parse(raw);
  } catch {
    throw new FileError(
      `${file} is not valid JSON, so it was not touched. Fix or remove it, then try again.`
    );
  }
  if (!isPlainObject(payload)) {
    throw new FileError(`${file} must hold a JSON object, so it was not touched.`);
  }
  // Keep the file's own indentation: the first indented key sits at depth one.
  const indented = raw.match(/^([ \t]+)"/m);
  return {
    exists: true,
    payload,
    indent: indented ? indented[1] : '  ',
    trailingNewline: raw.endsWith('\n'),
    bom,
  };
}

function writeAtomic(file, text, mode) {
  const dir = path.dirname(file);
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const tmp = path.join(
    dir,
    `.${path.basename(file)}.${process.pid}.${crypto.randomBytes(4).toString('hex')}.tmp`
  );
  try {
    fs.writeFileSync(tmp, text, { mode, flag: 'wx' });
    fs.renameSync(tmp, file);
  } catch (err) {
    try {
      fs.rmSync(tmp, { force: true });
    } catch {
      // The temp file is ours; a failed cleanup is not worth hiding the real error.
    }
    throw new FileError(`Could not write ${file} (${err.code || 'write error'}). Nothing was changed.`);
  }
}

function currentMode(file, fallback) {
  try {
    return fs.statSync(file).mode & 0o777;
  } catch {
    return fallback;
  }
}

function writeSettings(file, settings) {
  const body = JSON.stringify(settings.payload, null, settings.indent || '  ');
  const text = (settings.bom ? '﻿' : '') + body + (settings.trailingNewline ? '\n' : '');
  writeAtomic(file, text, currentMode(file, 0o644));
}

/** Reads a small JSON file this installer owns. Missing gives null; broken throws. */
function readOwnJson(file, label) {
  if (!fs.existsSync(file)) return null;
  let parsed;
  try {
    parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    throw new FileError(
      `${label} (${file}) is not valid JSON. Remove it and try again. Nothing was changed.`
    );
  }
  if (!isPlainObject(parsed)) {
    throw new FileError(`${label} (${file}) is not in the expected format. Nothing was changed.`);
  }
  return parsed;
}

/** Writes a file that holds secrets or ids: owner-only where the OS allows it. */
function writePrivateJson(file, value) {
  writeAtomic(file, JSON.stringify(value, null, 2) + '\n', 0o600);
}

function removeFile(file) {
  fs.rmSync(file, { force: true });
}

module.exports = {
  FileError,
  isPlainObject,
  hasOwn,
  readSettings,
  writeSettings,
  readOwnJson,
  writePrivateJson,
  removeFile,
};
