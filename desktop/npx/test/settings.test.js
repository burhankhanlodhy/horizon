'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const files = require('../lib/files');
const { isContextShrinkHost, displayHost } = require('../lib/connect');

function tempFile(text) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'contextshrink-settings-'));
  const file = path.join(dir, 'settings.json');
  if (text !== undefined) fs.writeFileSync(file, text, 'utf8');
  return { file, cleanup: () => fs.rmSync(dir, { recursive: true, force: true }) };
}

test('readSettings keeps four-space indentation and the trailing newline', () => {
  const t = tempFile('{\n    "a": 1,\n    "b": {\n        "c": 2\n    }\n}\n');
  try {
    const s = files.readSettings(t.file);
    assert.strictEqual(s.indent, '    ');
    assert.strictEqual(s.trailingNewline, true);
    assert.deepStrictEqual(s.payload, { a: 1, b: { c: 2 } });
  } finally {
    t.cleanup();
  }
});

test('readSettings keeps tab indentation and a byte order mark', () => {
  const t = tempFile('﻿{\n\t"a": 1\n}');
  try {
    const s = files.readSettings(t.file);
    assert.strictEqual(s.indent, '\t');
    assert.strictEqual(s.trailingNewline, false);
    assert.strictEqual(s.bom, true);
    files.writeSettings(t.file, s);
    assert.strictEqual(fs.readFileSync(t.file, 'utf8'), '﻿{\n\t"a": 1\n}');
  } finally {
    t.cleanup();
  }
});

test('readSettings: a missing file is an empty object, a blank file too', () => {
  const missing = tempFile();
  try {
    const s = files.readSettings(missing.file);
    assert.strictEqual(s.exists, false);
    assert.deepStrictEqual(s.payload, {});
  } finally {
    missing.cleanup();
  }
  const blank = tempFile('   \n');
  try {
    const s = files.readSettings(blank.file);
    assert.strictEqual(s.exists, true);
    assert.deepStrictEqual(s.payload, {});
  } finally {
    blank.cleanup();
  }
});

test('readSettings refuses invalid JSON and non-object roots', () => {
  const broken = tempFile('{"a":');
  try {
    assert.throws(() => files.readSettings(broken.file), files.FileError);
  } finally {
    broken.cleanup();
  }
  const array = tempFile('[1,2]');
  try {
    assert.throws(() => files.readSettings(array.file), /must hold a JSON object/);
  } finally {
    array.cleanup();
  }
});

test('writeSettings replaces the file in one step and leaves no temp file behind', () => {
  const t = tempFile('{"a":1}\n');
  try {
    const s = files.readSettings(t.file);
    s.payload.b = 2;
    files.writeSettings(t.file, s);
    assert.deepStrictEqual(JSON.parse(fs.readFileSync(t.file, 'utf8')), { a: 1, b: 2 });
    const leftovers = fs.readdirSync(path.dirname(t.file)).filter((name) => name.endsWith('.tmp'));
    assert.deepStrictEqual(leftovers, []);
  } finally {
    t.cleanup();
  }
});

test('only the proxy host and contextshrink.com names count as ours', () => {
  const proxy = 'https://proxy.contextshrink.com';
  assert.strictEqual(isContextShrinkHost('https://proxy.contextshrink.com/k/abc12345', proxy), true);
  assert.strictEqual(isContextShrinkHost('https://contextshrink.com/x', proxy), true);
  assert.strictEqual(isContextShrinkHost('https://gateway.vachi.example/v1', proxy), false);
  assert.strictEqual(isContextShrinkHost('https://contextshrink.com.evil.example/', proxy), false);
  assert.strictEqual(isContextShrinkHost('http://127.0.0.1:9/k/x', proxy), false);
  assert.strictEqual(isContextShrinkHost('not a url', proxy), false);
});

test('displayHost shows the host only', () => {
  assert.strictEqual(displayHost('https://user:secret@gateway.example:8443/v1?x=1'), 'gateway.example:8443');
  assert.strictEqual(displayHost('nope'), 'a value that is not a URL');
});
