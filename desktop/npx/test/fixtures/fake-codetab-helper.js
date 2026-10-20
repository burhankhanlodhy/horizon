'use strict';

// Test stand-in for contextshrink-codetab.exe. It records each call (its arguments,
// and whether codetab.json existed at that moment) in CONTEXTSHRINK_NPX_STUB_LOG,
// and answers with the JSON shape the real helper prints. It patches nothing.
//
// CONTEXTSHRINK_NPX_STUB_REMOVE_STATE sets the state reported by `remove`
// (default "real"); CONTEXTSHRINK_NPX_STUB_AUTOSTART=off makes `status` report off.

const fs = require('node:fs');
const path = require('node:path');

const args = process.argv.slice(2);
const command = args[0];
const configFile = path.join(process.env.CONTEXTSHRINK_DATA_DIR || '', 'claude-code', 'codetab.json');
const log = process.env.CONTEXTSHRINK_NPX_STUB_LOG;

if (log) {
  fs.appendFileSync(log, JSON.stringify({ args, configExists: fs.existsSync(configFile) }) + '\n');
}

function folder(state) {
  return { path: 'fake-claude-code/0.9.0/abc', state };
}

function answer(data) {
  process.stdout.write(JSON.stringify(data) + '\n');
  process.exit(0);
}

if (command === 'install') {
  answer({ folders: [folder('patched')] });
} else if (command === 'remove') {
  answer({ folders: [folder(process.env.CONTEXTSHRINK_NPX_STUB_REMOVE_STATE || 'real')] });
} else if (command === 'status') {
  answer({ folders: [folder('patched')], autostart: process.env.CONTEXTSHRINK_NPX_STUB_AUTOSTART !== 'off' });
} else if (command === 'autostart') {
  // CONTEXTSHRINK_NPX_STUB_AUTOSTART_FAIL=on makes `autostart on` fail with exit code 4.
  process.exit(process.env.CONTEXTSHRINK_NPX_STUB_AUTOSTART_FAIL === args[1] ? 4 : 0);
} else {
  process.stderr.write(`fake helper: unknown command ${command}\n`);
  process.exit(2);
}
