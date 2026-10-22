'use strict';
const fs = require('node:fs');
const os = require('node:os');
const crypto = require('node:crypto');
const { resolvePaths } = require('./config');
const files = require('./files');
const toml = require('./toml');
const api = require('./api');
const chats = require('./chats');
const savings = require('./savings-summary');
const { createPrompter, Cancelled } = require('./prompt');
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
Usage: npx contextshrink-codex [connect|login|status|savings|savings-window|chats|${GRAPH_AVAILABLE ? 'graph|' : ''}disconnect|logout]
  connect       Sign in to ContextShrink and configure local Codex
  login [--key] Rotate the device key, or paste a ContextShrink key
  status        Inspect the local connection without making model requests
  savings       Show this key's server-recorded totals today (UTC)
  savings-window Open a live Windows companion window (refreshes every 30s)
${GRAPH_HELP}chats list    List local chat IDs and their saved providers (Node 22.13+)
  chats migrate <ID...>|--all [--include-archived] [--apply]
  chats restore <ID...>|--all [--apply]
                Preview changes by default; --apply requires Codex closed
  disconnect    Restore the configuration, preserving unrelated edits
  logout        Disconnect and revoke the device key
Requires Node 18+ and a Codex/desktop build that supports custom model providers.
Keep your existing ChatGPT sign-in; restart the desktop app and start a new Codex chat.
`;
const COMMANDS = new Set(['connect', 'login', 'status', 'savings', 'savings-window', 'chats', 'disconnect', 'logout']);
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
  if (state && (![1, 2].includes(state.version) || typeof state.configExisted !== 'boolean' || !['written', 'original', 'block', 'separator', 'selectorLine'].every(k => typeof state[k] === 'string') ||
      ![null, 'string'].includes(state.originalLine === null ? null : typeof state.originalLine))) {
    throw new toml.ConfigError('Unsupported Codex connector state. It was kept unchanged.');
  }
  return state;
}
function keyOf(state) {
  const key = toml.document(state.written).model_providers?.[toml.ID]?.http_headers?.['X-Horizon-Proxy-Token'];
  if (!/^cs_live_[A-Za-z0-9_-]{20,200}$/.test(key || '')) throw new toml.ConfigError('Saved ContextShrink key is invalid.');
  return key;
}
function writeConfig(paths, text) { files.writePrivateText(paths.config, text); }
function checkAuthMode(paths) {
  const file = require('node:path').join(paths.dir, 'auth.json');
  if (!fs.existsSync(file)) return;
  let data;
  try { data = JSON.parse(read(file)); } catch { throw new toml.ConfigError('Codex auth.json is invalid. Sign in to Codex normally first.'); }
  if (String(data.auth_mode || '').toLowerCase() === 'apikey' || data.OPENAI_API_KEY) {
    throw new toml.ConfigError('This connector supports ChatGPT sign-in. API-key routing is not enabled in this version.');
  }
}
async function credentials(ctx, paths) {
  const email = (await ctx.prompter.ask('ContextShrink email: ')).trim();
  const password = await ctx.prompter.ask('Password: ', { hidden: true });
  return api.signIn(paths.apiUrl, email, password);
}
async function connect(ctx, paths, { login, keyFromStdin }) {
  checkAuthMode(paths);
  const current = read(paths.config);
  const state = stateAt(paths);
  const account = files.readOwnJson(paths.account, 'Codex account state') || {};
  let original = current;
  if (state) {
    // Validate ownership even on a repeat connect; never claim a route after edits.
    original = toml.restore(current, state);
    if (!login) { ctx.out('Already configured. Restart the desktop app and start a new Codex chat.'); return 0; }
  }
  // Validate the entire config before requesting credentials or minting a key.
  toml.connectText(original, paths.proxyUrl + '/v1', 'cs_live_preflightabcdefghijklmnop');
  let token, key, id = null, persisted = false;
  try {
    if (keyFromStdin) {
      key = (await ctx.prompter.ask('ContextShrink key (cs_live_...): ', { hidden: true })).trim();
      if (!/^cs_live_[A-Za-z0-9_-]{20,200}$/.test(key)) throw new toml.ConfigError('That is not a ContextShrink account key.');
    } else {
      token = await credentials(ctx, paths);
      const created = await api.createDeviceKey(paths.apiUrl, token, `${os.hostname().slice(0, 60)} (npx Codex)`);
      key = created.key; id = created.id;
    }
    const keepalive = /^[a-f0-9]{24}$/.test(account.keepalive_id || '') ? account.keepalive_id : crypto.randomBytes(12).toString('hex');
    const change = toml.connectText(original, `${paths.proxyUrl}/k/${keepalive}/v1`, key);
    const next = { version: 2, ...change, configExisted: state ? state.configExisted : fs.existsSync(paths.config), device_key_id: id, keepalive_id: keepalive };
    // Record restoration data before activating the route. Roll back on write failure.
    files.writePrivateJson(paths.state, next);
    try { writeConfig(paths, change.written); }
    catch (err) { if (state) files.writePrivateJson(paths.state, state); else files.removeFile(paths.state); throw err; }
    persisted = true;
    files.writePrivateJson(paths.account, { version: 1, keepalive_id: keepalive, device_key_id: id });
    if (token && account.device_key_id && account.device_key_id !== id) {
      try { await api.revokeDeviceKey(paths.apiUrl, token, account.device_key_id); }
      catch { ctx.out('The earlier key remains active; revoke it on the API Keys page.'); }
    }
    ctx.out('Codex configured for proxy.contextshrink.com using your existing ChatGPT sign-in.');
    ctx.out('Restart ChatGPT/Codex Desktop fully, then start a NEW local Codex chat.');
    ctx.out('Check savings with: npx contextshrink-codex savings');
    ctx.out('Ordinary ChatGPT chats and cloud tasks are not configured by this connector.');
    return 0;
  } catch (error) {
    if (token && id && !persisted) {
      try { await api.revokeDeviceKey(paths.apiUrl, token, id); } catch { ctx.err('A new key could not be revoked. Review the API Keys page.'); }
    }
    throw error;
  } finally { if (token) await api.signOut(paths.apiUrl, token); }
}
function disconnect(ctx, paths) {
  const state = stateAt(paths);
  if (!state) { ctx.out('Not connected by this Codex connector.'); return 0; }
  const restored = toml.restore(read(paths.config), state);
  // Restore only provider metadata recorded by the opt-in migration. Preserve
  // all turns and other metadata added since migration before removing routing.
  if (chats.hasMigrations(paths)) {
    const restoredIds = chats.restore(paths);
    ctx.out(`Restored original providers for ${restoredIds.length} existing chat(s).`);
  }
  // Preserve the current key id before removing the only file that records it.
  // This also recovers a failed account-file write after an otherwise successful login.
  const account = files.readOwnJson(paths.account, 'Codex account state') || {};
  if (account.device_key_id !== state.device_key_id) {
    files.writePrivateJson(paths.account, { version: 1, keepalive_id: state.keepalive_id, device_key_id: state.device_key_id });
  }
  if (!state.configExisted && restored === '') files.removeFile(paths.config);
  else writeConfig(paths, restored);
  files.removeFile(paths.state);
  ctx.out('Codex configuration restored. Restart the desktop app.');
  ctx.out('The ContextShrink key stays active until logout or revocation on the dashboard.');
  return 0;
}
async function logout(ctx, paths) {
  disconnect(ctx, paths);
  const account = files.readOwnJson(paths.account, 'Codex account state');
  if (!account) { ctx.out('No saved device key.'); return 0; }
  if (account.device_key_id) {
    const token = await credentials(ctx, paths);
    try { await api.revokeDeviceKey(paths.apiUrl, token, account.device_key_id); }
    finally { await api.signOut(paths.apiUrl, token); }
    ctx.out('Device key revoked.');
  } else ctx.out('A pasted key is not revoked here; manage it on the API Keys page.');
  files.removeFile(paths.account);
  return 0;
}
async function inspect(ctx, paths, command) {
  const state = stateAt(paths);
  if (!state) { ctx.out('Not connected by this Codex connector.'); return 0; }
  try { toml.restore(read(paths.config), state); }
  catch { ctx.err('Changed: Codex no longer matches the managed routing. Files were kept.'); return 1; }
  const provider = toml.document(state.written).model_providers[toml.ID];
  if (command === 'status') {
    ctx.out(`Configured for ${new URL(provider.base_url).host}; ChatGPT authentication preserved.`);
    ctx.out('Savings: this key today (UTC), via the savings command or dashboard.');
    ctx.out('Desktop savings: open the Windows companion with savings-window.');
    ctx.out('Existing chats: use chats list, then chats migrate <ID> --apply with Codex closed.');
    return 0;
  }
  const data = await api.keySummary(paths.apiUrl, keyOf(state));
  ctx.out(savings.format(savings.summary(data)));
  return 0;
}
async function liveSavings(paths) {
  const state = stateAt(paths);
  if (!state) throw new toml.ConfigError('Not connected by this Codex connector.');
  toml.restore(read(paths.config), state);
  return savings.summary(await api.keySummary(paths.apiUrl, keyOf(state), { timeoutMs: 2000 }));
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
  const ctx = { env: io.env, out: text => io.stdout.write(text + '\n'), err: text => io.stderr.write(text + '\n'),
    prompter: io.prompter || createPrompter({ stdin: io.stdin, stderr: io.stderr }) };
  let [command = 'connect', ...flags] = argv;
  if (['--version', '-v'].includes(command)) { ctx.out(VERSION); return 0; }
  if (['help', '--help', '-h'].includes(command)) { ctx.out(HELP); return 0; }
  if (command === 'graph') {
    if (!GRAPH_AVAILABLE) { ctx.err('Graph tools are experimental and not part of this release.'); return 2; }
    return require('./graph-command.cjs').run(flags, ctx);
  }
  const selectedChats = command === 'chats' ? chatArgs(flags) : null;
  if (!COMMANDS.has(command) || (command === 'chats' ? !selectedChats : flags.some(f => f !== '--key') || (flags.length && command !== 'login'))) { ctx.err(HELP); return 2; }
  let lock, lockFile;
  try {
    const paths = resolvePaths(io.env);
    if (['status', 'savings'].includes(command)) return await inspect(ctx, paths, command);
    if (command === 'savings-window') {
      if (!stateAt(paths)) throw new toml.ConfigError('Connect ContextShrink before opening the savings window.');
      return await require('./savings-window').show(() => liveSavings(paths), ctx);
    }
    if (command === 'chats' && !selectedChats.apply) return runChats(ctx, paths, selectedChats);
    fs.mkdirSync(paths.dir, { recursive: true, mode: 0o700 });
    lockFile = paths.state + '.lock';
    try { lock = fs.openSync(lockFile, 'wx', 0o600); }
    catch { throw new Error('Another Codex connector operation is running, or its lock remains. No configuration was changed.'); }
    if (command === 'chats') return runChats(ctx, paths, selectedChats);
    if (command === 'disconnect') return disconnect(ctx, paths);
    if (command === 'logout') return await logout(ctx, paths);
    return await connect(ctx, paths, { login: command === 'login', keyFromStdin: flags.includes('--key') });
  } catch (err) {
    const known = err instanceof toml.ConfigError || err instanceof files.FileError || err instanceof api.ApiError || err instanceof Cancelled;
    // Never echo unexpected parser/HTTP errors that could include a secret config.
    ctx.err(known ? err.message : 'Could not complete the Codex setup. Check paths, permissions, and the connector lock.');
    return 1;
  } finally {
    if (lock !== undefined) { fs.closeSync(lock); files.removeFile(lockFile); }
  }
}
module.exports = { run };
