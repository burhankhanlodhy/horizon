'use strict';

// The engine's background commands never show a console window: the bundle's pythonw.exe
// runs the CLI, and the hook and the Startup shortcut use it. Shared by both npx packages.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const engine = require('../lib/code-engine');

function bundle({ withPythonw = true } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'cs-windowless-'));
  fs.mkdirSync(path.join(root, 'python'), { recursive: true });
  fs.mkdirSync(path.join(root, 'app'), { recursive: true });
  fs.writeFileSync(path.join(root, 'horizon.exe'), '');
  if (withPythonw) fs.writeFileSync(path.join(root, 'python', 'pythonw.exe'), '');
  fs.writeFileSync(path.join(root, 'app', 'horizon_main.py'), '');
  return { root, exe: path.join(root, 'horizon.exe') };
}

test('the engine runs under the bundle pythonw.exe, the same CLI as horizon.exe', () => {
  const { root, exe } = bundle();
  try {
    const invocation = engine.windowlessInvocation(exe, ['code-engine', 'run']);
    assert.equal(invocation.file, path.join(root, 'python', 'pythonw.exe'));
    assert.deepEqual(invocation.args, [
      '-I',
      '-s',
      '-X',
      'utf8',
      path.join(root, 'app', 'horizon_main.py'),
      'code-engine',
      'run',
    ]);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('a bundle without pythonw.exe keeps the horizon.exe invocation', () => {
  const { root, exe } = bundle({ withPythonw: false });
  try {
    assert.deepEqual(engine.windowlessInvocation(exe, ['code-engine', 'ensure']), {
      file: exe,
      args: ['code-engine', 'ensure'],
    });
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('the SessionStart hook runs the windowless invocation as one command line', () => {
  const { root, exe } = bundle();
  try {
    const command = engine.hookCommandOf(exe);
    const pythonw = path.join(root, 'python', 'pythonw.exe');
    const script = path.join(root, 'app', 'horizon_main.py');
    assert.ok(command.startsWith(`"${pythonw}" -I -s -X utf8 `), command);
    assert.ok(command.includes(script), command);
    assert.ok(command.endsWith(' code-engine ensure'), command);
    assert.ok(!command.includes('horizon.exe'), command);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('without pythonw.exe the hook command is the one connect always wrote', () => {
  const { root, exe } = bundle({ withPythonw: false });
  try {
    assert.equal(engine.hookCommandOf(exe), `"${exe}" code-engine ensure`);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('the Startup shortcut arguments are the windowless invocation arguments', () => {
  const { root, exe } = bundle();
  try {
    const { file } = engine.windowlessInvocation(exe, ['code-engine', 'run']);
    assert.equal(file, path.join(root, 'python', 'pythonw.exe'));
    const args = engine.windowlessInvocation(exe, ['code-engine', 'run']).args;
    assert.equal(args[args.length - 2], 'code-engine');
    assert.equal(args[args.length - 1], 'run');
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
