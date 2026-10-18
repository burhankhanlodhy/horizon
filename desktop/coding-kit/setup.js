#!/usr/bin/env node
'use strict';

// Creates the comparison kit outside this repository (so no project files or
// CLAUDE.md from here reach the test sessions):
//
//   node desktop\coding-kit\setup.js            -> %USERPROFILE%\VachiCompare
//   node desktop\coding-kit\setup.js --out DIR
//
// It refuses to touch a folder that already exists, and never deletes anything.

const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const args = process.argv.slice(2);
const outIndex = args.indexOf('--out');
const out = path.resolve(outIndex >= 0 && args[outIndex + 1] ? args[outIndex + 1] : path.join(os.homedir(), 'VachiCompare'));
const runs = ['A-plain', 'B-vachi'];

if (fs.existsSync(out)) {
  console.error(`${out} already exists. Delete it yourself, or pick another folder with --out.`);
  process.exit(1);
}

const here = __dirname;
fs.mkdirSync(path.join(out, 'runs'), { recursive: true });
for (const run of runs) {
  fs.cpSync(path.join(here, 'template'), path.join(out, 'runs', run), { recursive: true });
}
fs.cpSync(path.join(here, 'grader'), path.join(out, '_grader'), { recursive: true });
for (const file of ['check.js', 'route.js', 'TASKS.md', 'GUIDE.md', 'RESULTS-SHEET.md']) {
  fs.copyFileSync(path.join(here, file), path.join(out, file));
}

// Both runs start as identical git repositories, so Claude can use git diff the
// same way in each.
const git = spawnSync('git', ['--version'], { encoding: 'utf8' });
if (git.status === 0) {
  for (const run of runs) {
    const cwd = path.join(out, 'runs', run);
    const steps = [
      ['init', '-q'],
      ['add', '-A'],
      ['-c', 'user.name=Coding Kit', '-c', 'user.email=kit@example.invalid', 'commit', '-q', '-m', 'Starting point'],
    ];
    for (const step of steps) {
      const result = spawnSync('git', step, { cwd, encoding: 'utf8' });
      if (result.status !== 0) {
        console.error(`git ${step.join(' ')} failed in ${cwd}:\n${result.stderr}`);
        process.exit(1);
      }
    }
  }
} else {
  console.log('git not found: the run folders are plain folders (same for both runs).');
}

console.log(`Kit created in ${out}`);
console.log('  runs\\A-plain   session without Vachi');
console.log('  runs\\B-vachi   session with Vachi');
console.log('Open GUIDE.md there to start.');
