'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { resolvePaths } = require('./config');
const files = require('./files');
const toml = require('./toml');
const api = require('./api');
const chats = require('./chats');
const savings = require('./savings-summary');
const login = require('./code-login');
const device = require('./code-device');
const engine = require('./code-engine');
const { CsError, platformOf } = require('./code-env');
const VERSION = require('../package.json').version;
// The experimental graph tools live in the repository only; the npm package
// leaves them out (they download a third-party engine and have not shown a
// whole-chat saving), so the command exists only when its module is present.
const GRAPH_AVAILABLE = (() => {
  try { require.resolve('./graph-command.cjs'); return true; } catch { return false; }
})();
const GRAPH_HELP = GRAPH_AVAILABLE ? `  graph init PROJECT  Build a reusable local code graph and enable project Codex tools
graph status|refresh|overview|disable PROJECT
graph search|trace|query PROJECT "QUERY_OR_SYMBOL"
` : '';
const HELP = `contextshrink-codex ${VERSION}
Usage: npx contextshrink-codex [connect|status|savings|savings-window|chats|${GRAPH_AVAILABLE ? 'graph|' : ''}uninstall]
  connect       Sign this computer in and point local Codex at the ContextShrink engine
  status        Show the device, plan, engine and usage saved today (no model requests)
  savings       Show usage saved today and the trial days left
  savings-window Open a live Windows companion window (refreshes every 30s)
${GRAPH_HELP}chats list    List local chat IDs and their saved providers (Node 22.13+)
  chats migrate <ID...>|--all [--include-archived] [--apply]
  chats restore <ID...>|--all [--apply]
                Preview changes by default; --apply requires Codex closed
  uninstall     Restore the Codex configuration; release this computer when no tool is left
                (disconnect and logout also run uninstall)
Requires Node 18+ and a Codex/desktop build that supports custom model providers.
Keep your existing ChatGPT sign-in; restart the desktop app and start a new Codex chat.
`;
const COMMANDS = new Set(['connect', 'status', 'savings', 'savings-window', 'chats', 'uninstall', 'disconnect', 'logout']);
function chatArgs(flags) {
  const [action, ...args] = flags;
  if (!['list', 'migrate', 'restore'].includes(action) || (action === 'list' && args.length)) return null;
  const allowed = action === 'migrate' ? ['--all', '--apply', '--include-archived'] : ['--all', '--apply'];
  if (args.some(f => f.startsWith('-') && !allowed.includes(f))) return null;
  const ids = args.filter(f => !f.startsWith('-'));
  if (action !== 'list' && ((!ids.length && !args.includes('--all')) || (ids.length && args.includes('--all')) || ids.some(id => !/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(id)))) return null;
  return { action, ids, all: args.includes('--all'), apply: args.includes('--apply'), includeArchived: args.includes('--include-archived') };
}
function read(file) { return fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : ''; }
function stateAt(paths) {
  const state = files.readOwnJson(paths.state, 'Codex connector state');
  if (state && (![1, 2, 3].includes(state.version) || typeof state.configExisted !== 'boolean' || !['written', 'original', 'block', 'separator', 'selectorLine'].every(k => typeof state[k] === 'string') ||
      ![null, 'string'].includes(state.originalLine === null ? null : typeof state.originalLine))) {
    throw new toml.ConfigError('Unsupported Codex connector state. It was kept unchanged.');
  }
  return state;
}
function writeConfig(paths, text) { files.writePrivateText(paths.config, text); }
function checkAuthMode(paths) {
  const file = path.join(paths.dir, 'auth.json');
  if (!fs.existsSync(file)) return;
  let data;
  try { data = JSON.parse(read(file)); } catch { throw new toml.ConfigError('Codex auth.json is invalid. Sign in to Codex normally first.'); }
  if (String(data.auth_mode || '').toLowerCase() === 'apikey' || data.OPENAI_API_KEY) {
    throw new toml.ConfigError('This connector supports ChatGPT sign-in. API-key routing is not enabled in this version.');
  }
}
// The device record used for calls to the API: its own address, else the configured one.
function apiBaseOf(dev, paths) { return (dev && dev.api_url) || paths.apiUrl; }
function trialDaysOf(note) { return note && note.plan === 'trial' ? device.daysLeft(note.until) : null; }

// The per-computer record of versions before 0.3.0. Removed once this version has run.
function retireLegacyAccount(ctx, paths) {
  let legacy = null;
  try { legacy = files.readOwnJson(paths.legacyAccount, 'Codex account state'); } catch { legacy = null; }
  if (!legacy) return;
  files.removeFile(paths.legacyAccount);
  if (legacy.device_key_id) ctx.out('The account key from the earlier version is no longer used. Revoke it on the API Keys page.');
}

async function connect(ctx, paths) {
  const env = ctx.env;
  if (platformOf(env) !== 'win32') throw new CsError('ContextShrink Code runs on Windows today. macOS and Linux are coming.');
  checkAuthMode(paths);
  const manifest = engine.loadManifest(env);
  if (!manifest.published) throw new CsError(engine.UNPUBLISHED);
  const current = read(paths.config);
  const state = stateAt(paths);
  const original = state ? toml.restore(current, state) : current;
  // Validate the entire config before any sign-in, so a conflicting layout changes nothing.
  toml.connectText(original, paths.engineUrl + '/v1');
  const adapter = engine.resolveAdapter(env);
  const { note } = await device.ensureDevice(ctx, env, {
    tool: 'codex',
    toolName: 'Codex',
    commandName: 'contextshrink-codex',
    deps: { adapter },
  });
  const exe = await engine.installEngine(ctx, env, manifest, adapter);
  device.applyPlanNote(ctx, env, note, 'Codex');
  await engine.startEngine(env, exe, adapter);
  await engine.installStartup(env, exe, adapter);

  const change = toml.connectText(original, paths.engineUrl + '/v1');
  if (state && change.written === current) {
    ctx.out('Already configured. Restart the desktop app and start a new Codex chat.');
  } else {
    const next = { version: 3, ...change, configExisted: state ? state.configExisted : fs.existsSync(paths.config), engine_url: paths.engineUrl };
    // Record restoration data before activating the route. Roll back on write failure.
    files.writePrivateJson(paths.state, next);
    try { writeConfig(paths, change.written); }
    catch (err) { if (state) files.writePrivateJson(paths.state, state); else files.removeFile(paths.state); throw err; }
    ctx.out('Codex configured for the ContextShrink engine on this computer, using your existing ChatGPT sign-in.');
    ctx.out('Restart ChatGPT/Codex Desktop fully, then start a NEW local Codex chat.');
    ctx.out('Check usage with: npx contextshrink-codex status');
    ctx.out('Ordinary ChatGPT chats and cloud tasks are not configured by this connector.');
  }
  retireLegacyAccount(ctx, paths);
  return 0;
}

// Restores the Codex configuration. Returns false when this connector had not set it up.
function restoreConfig(ctx, paths) {
  const state = stateAt(paths);
  if (!state) {
    ctx.out('Not connected by this Codex connector.');
    return false;
  }
  const restored = toml.restore(read(paths.config), state);
  // Restore only provider metadata recorded by the opt-in migration. Preserve
  // all turns and other metadata added since migration before removing routing.
  if (chats.hasMigrations(paths)) {
    const restoredIds = chats.restore(paths);
    ctx.out(`Restored original providers for ${restoredIds.length} existing chat(s).`);
  }
  if (!state.configExisted && restored === '') files.removeFile(paths.config);
  else writeConfig(paths, restored);
  files.removeFile(paths.state);
  ctx.out('Codex configuration restored. Restart the desktop app.');
  return true;
}

// Restores Codex, then leaves the device: released only when no ContextShrink tool is left.
async function uninstall(ctx, paths) {
  restoreConfig(ctx, paths);
  retireLegacyAccount(ctx, paths);
  await device.leaveDevice(ctx, ctx.env, { tool: 'codex' });
  return 0;
}

async function inspect(ctx, paths, command) {
  const state = stateAt(paths);
  if (command === 'savings') {
    if (!state) { ctx.out('Not connected by this Codex connector.'); return 0; }
    const dev = device.readDevice(ctx.env);
    if (!dev) throw new CsError('Connect ContextShrink before checking savings.');
    const data = await login.fetchUsageSummary(apiBaseOf(dev, paths), dev.device_key);
    ctx.out(savings.format(savings.summary(data, trialDaysOf(device.readPlanNote(ctx.env)))));
    return 0;
  }
  if (!state) {
    ctx.out('Not connected by this Codex connector.');
  } else {
    try { toml.restore(read(paths.config), state); }
    catch { ctx.err('Changed: Codex no longer matches the managed routing. Files were kept.'); return 1; }
    const provider = toml.document(state.written).model_providers[toml.ID];
    ctx.out(`Configured for ${new URL(provider.base_url).host}; ChatGPT authentication preserved.`);
  }
  await device.reportDevice(ctx, ctx.env, { toolName: 'Codex' });
  ctx.out('Desktop savings: open the Windows companion with savings-window.');
  ctx.out('Existing chats: use chats list, then chats migrate <ID> --apply with Codex closed.');
  return 0;
}

async function liveSavings(env, paths) {
  const state = stateAt(paths);
  if (!state) throw new toml.ConfigError('Not connected by this Codex connector.');
  toml.restore(read(paths.config), state);
  const dev = device.readDevice(env);
  if (!dev) throw new toml.ConfigError('Connect ContextShrink before opening the savings window.');
  const data = await login.fetchUsageSummary(apiBaseOf(dev, paths), dev.device_key, { timeoutMs: 2000 });
  return savings.summary(data, trialDaysOf(device.readPlanNote(env)));
}
function runChats(ctx, paths, options) {
  if (options.action === 'list') {
    const rows = chats.list(paths);
    for (const row of rows) ctx.out(`${row.id}  ${row.provider.replace(/[\r\n\x00-\x1f]/g, '')}${row.archived ? '  (archived)' : ''}`);
    if (!rows.length) ctx.out('No local chats found.');
    return 0;
  }
  if (options.action === 'migrate') {
    const state = stateAt(paths);
    if (!state) throw new toml.ConfigError('Connect ContextShrink before migrating existing chats.');
    toml.restore(read(paths.config), state);
    // Older previews do not know how to restore migrated chats. Upgrade state
    // before touching a chat so those versions refuse to remove its provider.
    if (options.apply && state.version === 1) {
      chats.assertClosed();
      files.writePrivateJson(paths.state, { ...state, version: 2 });
    }
  }
  const ids = options.action === 'migrate' ? chats.migrate(paths, options) : chats.restore(paths, options);
  for (const id of ids) ctx.out(`${id}: ${options.action === 'migrate' ? 'openai -> contextshrink_npx' : 'contextshrink_npx -> openai'}`);
  ctx.out(options.apply ? `${ids.length} chat(s) updated. Reopen Codex Desktop to test.` : `Dry run: ${ids.length} chat(s). No chats changed. Close Codex, then repeat with --apply.`);
  return 0;
}
async function run(argv, io) {
  const ctx = { env: io.env, out: text => io.stdout.write(text + '\n'), err: text => io.stderr.write(text + '\n') };
  let [command = 'connect', ...flags] = argv;
  if (['--version', '-v'].includes(command)) { ctx.out(VERSION); return 0; }
  if (['help', '--help', '-h'].includes(command)) { ctx.out(HELP); return 0; }
  if (command === 'graph') {
    if (!GRAPH_AVAILABLE) { ctx.err('Graph tools are experimental and not part of this release.'); return 2; }
    return require('./graph-command.cjs').run(flags, ctx);
  }
  const selectedChats = command === 'chats' ? chatArgs(flags) : null;
  if (!COMMANDS.has(command) || (command === 'chats' ? !selectedChats : flags.length > 0)) { ctx.err(HELP); return 2; }
  let lock, lockFile;
  try {
    const paths = resolvePaths(io.env);
    if (['status', 'savings'].includes(command)) return await inspect(ctx, paths, command);
    if (command === 'savings-window') {
      if (!stateAt(paths)) throw new toml.ConfigError('Connect ContextShrink before opening the savings window.');
      return await require('./savings-window').show(() => liveSavings(io.env, paths), ctx);
    }
    if (command === 'chats' && !selectedChats.apply) return runChats(ctx, paths, selectedChats);
    fs.mkdirSync(paths.dir, { recursive: true, mode: 0o700 });
    lockFile = paths.state + '.lock';
    try { lock = fs.openSync(lockFile, 'wx', 0o600); }
    catch { throw new Error('Another Codex connector operation is running, or its lock remains. No configuration was changed.'); }
    if (command === 'chats') return runChats(ctx, paths, selectedChats);
    if (command === 'connect') return await connect(ctx, paths);
    return await uninstall(ctx, paths);
  } catch (err) {
    const known = err instanceof toml.ConfigError || err instanceof files.FileError || err instanceof api.ApiError || err instanceof CsError;
    // Never echo unexpected parser/HTTP errors that could include a secret config.
    ctx.err(known ? err.message : 'Could not complete the Codex setup. Check paths, permissions, and the connector lock.');
    return known && err.exitCode ? err.exitCode : 1;
  } finally {
    if (lock !== undefined) { fs.closeSync(lock); files.removeFile(lockFile); }
  }
}
module.exports = { run };
