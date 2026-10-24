'use strict';

// Command line for `npx contextshrink-claude`: parses arguments, runs one command and maps
// the outcome to an exit code. Output never includes the device key.

const { Refusal, connect, uninstall, status } = require('./connect');
const lean = require('./lean');
const { ApiError } = require('./api');
const { FileError } = require('./files');
const { CsError } = require('./code-env');

const USAGE = `contextshrink-claude ${require('../package.json').version}

Usage:
  npx contextshrink-claude [connect] [--force]   Sign this computer in and route Claude Code through ContextShrink
  npx contextshrink-claude uninstall             Restore Claude Code and the Code tab; release this computer when no tool is left
  npx contextshrink-claude status                Show the device, plan, engine and usage saved today
  npx contextshrink-claude lean on [--force]     Keep sessions lean: Claude Code compacts at 200,000 tokens
  npx contextshrink-claude lean off              Put back the compaction setting that lean on replaced
  npx contextshrink-claude lean status           Show whether keep sessions lean is on

Options:
  --force        Replace another gateway or an existing ANTHROPIC_CUSTOM_HEADERS (uninstall restores it);
                 with lean on, replace a CLAUDE_CODE_AUTO_COMPACT_WINDOW you set (lean off restores it)
  --no-code-tab  Do not set up Claude Desktop's Code tab (Windows; connect only)
  -h, --help     Show this help

uninstall also runs as disconnect or logout.
`;

const COMMANDS = new Set(['connect', 'uninstall', 'disconnect', 'logout', 'status', 'help', 'lean']);
const LEAN_COMMANDS = new Set(['on', 'off', 'status']);
const KNOWN_FLAGS = new Set(['--force', '--no-code-tab', '-h', '--help', '--version', '-v']);

function parseArgs(argv) {
  const args = argv.slice();
  let command = 'connect';
  if (args.length && !args[0].startsWith('-')) command = args.shift();
  // `lean` takes one word after it: on, off or status.
  let sub = null;
  if (command === 'lean' && args.length && !args[0].startsWith('-')) sub = args.shift();
  const flags = new Set(args);
  const unknown = args.filter((a) => !KNOWN_FLAGS.has(a));
  return { command, sub, flags, unknown };
}

async function run(argv, io) {
  const out = (line = '') => io.stdout.write(line + '\n');
  const err = (line = '') => io.stderr.write(line + '\n');

  const { command, sub, flags, unknown } = parseArgs(argv);
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
  if (command === 'lean' && !LEAN_COMMANDS.has(sub)) {
    err(`lean needs one of: on, off, status.`);
    err(USAGE.trimEnd());
    return 2;
  }
  const forceApplies = command === 'connect' || (command === 'lean' && sub === 'on');
  if (flags.has('--force') && !forceApplies) {
    err('--force only applies to connect and lean on.');
    return 2;
  }
  if (flags.has('--no-code-tab') && command !== 'connect') {
    err('--no-code-tab only applies to connect.');
    return 2;
  }

  const ctx = { env: io.env, out, err };

  try {
    switch (command) {
      case 'connect':
        return await connect(ctx, { force: flags.has('--force'), codeTab: !flags.has('--no-code-tab') });
      case 'uninstall':
      case 'disconnect':
      case 'logout':
        return await uninstall(ctx);
      case 'status':
        return await status(ctx);
      case 'lean':
        if (sub === 'on') return lean.turnOn(ctx, { force: flags.has('--force') });
        if (sub === 'off') return lean.turnOff(ctx);
        return lean.showStatus(ctx);
      default:
        return 2;
    }
  } catch (error) {
    if (error instanceof Refusal || error instanceof CsError || error instanceof FileError || error instanceof ApiError) {
      err(error.message);
      return error.exitCode || 1;
    }
    err(`Unexpected error: ${error && error.message ? error.message : 'unknown'}.`);
    return 1;
  }
}

module.exports = { run, parseArgs, USAGE };
