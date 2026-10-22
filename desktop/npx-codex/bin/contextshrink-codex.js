#!/usr/bin/env node
'use strict';
const { run } = require('../lib/cli');
run(process.argv.slice(2), { env: process.env, stdin: process.stdin, stdout: process.stdout, stderr: process.stderr })
  .then(code => { process.exitCode = code; }, () => { console.error('Unexpected connector error.'); process.exitCode = 1; });
