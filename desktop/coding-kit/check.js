#!/usr/bin/env node
'use strict';

// Grades one run folder: runs the hidden acceptance tests and the project's own
// tests, prints the result and appends it to results.jsonl next to this file.
//
//   node check.js runs\A-plain
//   node check.js runs\B-vachi
//
// It runs the code Claude wrote in that folder, with Node, on this computer.

const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const args = process.argv.slice(2);
const save = !args.includes('--no-save');
const folder = args.find((arg) => !arg.startsWith('--'));
if (!folder) {
  console.error('usage: node check.js <run folder> [--no-save]');
  process.exit(2);
}
const target = path.resolve(folder);
if (!fs.existsSync(path.join(target, 'src', 'inventory.js'))) {
  console.error(`not a run folder (no src/inventory.js): ${target}`);
  process.exit(2);
}

const graderDir = ['_grader', 'grader']
  .map((name) => path.join(__dirname, name))
  .find((dir) => fs.existsSync(path.join(dir, 'acceptance.test.js')));
if (!graderDir) {
  console.error('acceptance tests not found next to check.js');
  process.exit(2);
}

function runTap(argv, options) {
  const result = spawnSync(process.execPath, ['--test', '--test-reporter=tap', ...argv], {
    encoding: 'utf8',
    timeout: 120000,
    ...options,
  });
  const out = `${result.stdout || ''}`;
  const number = (label) => {
    const match = out.match(new RegExp(`^# ${label} (\\d+)$`, 'm'));
    return match ? Number(match[1]) : null;
  };
  const failed = [...out.matchAll(/^not ok \d+ - (.+)$/gm)]
    .map((match) => match[1].trim())
    .filter((name) => !name.endsWith('.js'));
  return {
    pass: number('pass'),
    fail: number('fail'),
    total: number('tests'),
    failed,
    timedOut: result.error ? String(result.error.code || result.error.message) : null,
  };
}

const acceptance = runTap([path.join(graderDir, 'acceptance.test.js')], {
  env: { ...process.env, KIT_TARGET: target },
});
const own = runTap([], { cwd: target });

console.log(`Run folder: ${target}`);
console.log(`Acceptance tests: ${acceptance.pass ?? '?'} of ${acceptance.total ?? '?'} passed`);
for (const name of acceptance.failed) {
  console.log(`  failed: ${name}`);
}
if (acceptance.timedOut) {
  console.log(`  stopped: ${acceptance.timedOut}`);
}
console.log(`Project's own tests (npm test): ${own.pass ?? '?'} of ${own.total ?? '?'} passed`);

if (save) {
  const line = {
    at: new Date().toISOString(),
    run: path.basename(target),
    acceptance_pass: acceptance.pass,
    acceptance_total: acceptance.total,
    acceptance_failed: acceptance.failed,
    own_tests_pass: own.pass,
    own_tests_total: own.total,
  };
  fs.appendFileSync(path.join(__dirname, 'results.jsonl'), `${JSON.stringify(line)}\n`);
  console.log('Saved to results.jsonl');
}
