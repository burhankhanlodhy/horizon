#!/usr/bin/env node
'use strict';

// Entry point for `npx contextshrink`. Plain JavaScript, no dependencies, no
// background process: it runs one command and exits.

const { run } = require('../lib/cli');

run(process.argv.slice(2), {
  env: process.env,
  stdin: process.stdin,
  stdout: process.stdout,
  stderr: process.stderr,
}).then(
  (code) => {
    process.exitCode = code;
  },
  (error) => {
    process.stderr.write(`Unexpected error: ${error && error.message ? error.message : 'unknown'}\n`);
    process.exitCode = 1;
  }
);
