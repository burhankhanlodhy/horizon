#!/usr/bin/env node
'use strict';

// Checks the kit itself: the starting project must fail the acceptance tests
// (and its own seeded-bug test), and the reference solution must pass them all.
//
//   node desktop\coding-kit\selftest.js

const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const here = __dirname;
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'coding-kit-'));
const start = path.join(scratch, 'start');
const solved = path.join(scratch, 'solved');
fs.cpSync(path.join(here, 'template'), start, { recursive: true });
fs.cpSync(path.join(here, 'template'), solved, { recursive: true });
fs.rmSync(path.join(solved, 'test', 'inventory.test.js'));
fs.cpSync(path.join(here, 'reference'), solved, { recursive: true });

function grade(folder) {
  const result = spawnSync(process.execPath, [path.join(here, 'check.js'), folder, '--no-save'], {
    encoding: 'utf8',
  });
  const acceptance = result.stdout.match(/Acceptance tests: (\d+) of (\d+) passed/);
  const own = result.stdout.match(/own tests \(npm test\): (\d+) of (\d+) passed/);
  if (!acceptance || !own) {
    throw new Error(`could not read check.js output:\n${result.stdout}\n${result.stderr}`);
  }
  return { pass: Number(acceptance[1]), total: Number(acceptance[2]), ownPass: Number(own[1]), ownTotal: Number(own[2]) };
}

const before = grade(start);
const after = grade(solved);
fs.rmSync(scratch, { recursive: true, force: true });

const problems = [];
if (before.pass > 5) problems.push(`starting project passes ${before.pass} acceptance tests (expected at most 5)`);
if (before.ownPass !== before.ownTotal - 1) problems.push(`starting project should fail exactly one of its own tests, got ${before.ownPass}/${before.ownTotal}`);
if (after.pass !== after.total) problems.push(`reference solution passes ${after.pass}/${after.total}`);
if (after.ownPass !== after.ownTotal) problems.push(`reference tests pass ${after.ownPass}/${after.ownTotal}`);

console.log(`starting project: acceptance ${before.pass}/${before.total}, own ${before.ownPass}/${before.ownTotal}`);
console.log(`reference:        acceptance ${after.pass}/${after.total}, own ${after.ownPass}/${after.ownTotal}`);
if (problems.length) {
  console.error(problems.join('\n'));
  process.exit(1);
}
console.log('kit OK');
