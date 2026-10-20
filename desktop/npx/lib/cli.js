'use strict';

// Command line for `npx contextshrink-claude`: parses arguments, runs one command and maps
// the outcome to an exit code. Output never includes the account key.

const { Refusal, connect, disconnect, status, logout } = require('./connect');
const { ApiError } = require('./api');
const { FileError } = require('./files');
const { Cancelled, createPrompter } = require('./prompt');

const USAGE = `contextshrink-claude ${require('../package.json').version}

Usage:
  npx contextshrink-claude [connect] [--force]   Sign in and route Claude Code through ContextShrink
  npx contextshrink-claude login [--key]         Make a new account key for this computer (--key: paste one)
  npx contextshrink-claude disconnect            Restore the settings that connect changed
  npx contextshrink-claude status                Show whether Claude Code is connected
  npx contextshrink-claude logout                Disconnect and revoke this computer's account key

Options:
  --force    Replace another gateway or existing header (disconnect restores it)
  --key      Read an account key from stdin instead of signing in
  -h, --help Show this help
`;

const COMMANDS = new Set(['connect', 'login', 'disconnect', 'status', 'logout', 'help']);

function parseArgs(argv) {
  const args = argv.slice();
  let command = 'connect';
  if (args.length && !args[0].startsWith('-')) command = args.shift();
  const flags = new Set(args);
  const known = new Set(['--force', '--key', '-h', '--help', '--version', '-v']);
  const unknown = args.filter((a) => !known.has(a));
  return { command, flags, unknown };
}

async function run(argv, io) {
  const out = (line = '') => io.stdout.write(line + '\n');
  const err = (line = '') => io.stderr.write(line + '\n');

  const { command, flags, unknown } = parseArgs(argv);
  if (flags.has('--version') || flags.has('-v')) {
    out(require('../package.json').version);
    return 0;
  }
  if (flags.has('-h') || flags.has('--help') || command === 'help') {
    out(USAGE.trimEnd());
    return 0;
  }
  if (!COMMANDS.has(command)) {
    err(`Unknown command: ${command}\n`);
    err(USAGE.trimEnd());
    return 2;
  }
  if (unknown.length) {
    err(`Unknown option: ${unknown[0]}\n`);
    err(USAGE.trimEnd());
    return 2;
  }
  if (flags.has('--force') && !['connect', 'login'].includes(command)) {
    err('--force only applies to connect and login.');
    return 2;
  }
  if (flags.has('--key') && command !== 'login') {
    err('--key only applies to login.');
    return 2;
  }

  const ctx = {
    env: io.env,
    out,
    err,
    prompter: io.prompter || createPrompter({ stdin: io.stdin, stderr: io.stderr }),
  };

  try {
    switch (command) {
      case 'connect':
        return await connect(ctx, { force: flags.has('--force') });
      case 'login':
        return await connect(ctx, {
          force: flags.has('--force'),
          login: true,
          keyFromStdin: flags.has('--key'),
        });
      case 'disconnect':
        return await disconnect(ctx);
      case 'status':
        return await status(ctx);
      case 'logout':
        return await logout(ctx);
      default:
        return 2;
    }
  } catch (error) {
    if (error instanceof Refusal || error instanceof FileError || error instanceof ApiError) {
      err(error.message);
      return 1;
    }
    if (error instanceof Cancelled) {
      err('Cancelled. Nothing was changed.');
      return 1;
    }
    err(`Unexpected error: ${error && error.message ? error.message : 'unknown'}.`);
    return 1;
  }
}

module.exports = { run, parseArgs, USAGE };
