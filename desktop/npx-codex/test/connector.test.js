'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const { execFile } = require('node:child_process');
const { parse } = require('smol-toml');
const { connectText, restore, ID } = require('../lib/toml');
const KEY = 'cs_live_TESTabcdefghijklmnopqrstuv123';
const BIN = path.join(__dirname, '../bin/contextshrink-codex.js');
async function fixture(fn) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'cs-codex-test-'));
  const home = path.join(root, 'home');
  const dir = path.join(home, '.codex');
  fs.mkdirSync(dir, { recursive: true });
  const calls = [], live = new Map(), issued = [];
  let seq = 0;
  const server = http.createServer((req, res) => {
    let raw = '';
    req.on('data', c => { raw += c; });
    req.on('end', () => {
      const body = raw ? JSON.parse(raw) : {};
      calls.push({ path: req.url, method: req.method, headers: req.headers, body });
      const send = (status, data) => { res.writeHead(status, { 'content-type': 'application/json' }); res.end(data ? JSON.stringify(data) : ''); };
      if (req.url === '/auth/login') return body.email === 'test@example.test' && body.password === 'test-pass'
        ? send(200, { token: 'session' }) : send(401, { detail: 'Invalid' });
      if (req.url === '/auth/logout') return send(204);
      if (req.url === '/usage/key-summary') return live.has([...live.entries()].find(([, key]) => req.headers.authorization === `Bearer ${key}`)?.[0])
        ? send(200, { scope: 'key', period: 'today_utc', requests: 4, tokens_before: 1000, tokens_saved: 250, savings_usd: 0.12 }) : send(401);
      if (req.headers.authorization !== 'Bearer session') return send(401);
      if (req.url === '/keys') {
        const id = `00000000-0000-4000-8000-${String(++seq).padStart(12, '0')}`;
        const key = KEY + seq;
        issued.push(key); live.set(id, key);
        return send(201, { id, key });
      }
      if (req.method === 'DELETE' && req.url.startsWith('/keys/')) { live.delete(req.url.slice(6)); return send(204); }
      send(404);
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const env = { PATH: process.env.PATH, SystemRoot: process.env.SystemRoot || '', HOME: home, USERPROFILE: home,
    CODEX_HOME: dir, TEMP: root, TMP: root, LOCALAPPDATA: path.join(root, 'local'), APPDATA: path.join(root, 'roaming'),
    CONTEXTSHRINK_API_URL: `http://127.0.0.1:${server.address().port}`, CONTEXTSHRINK_PROXY_URL: `http://127.0.0.1:${server.address().port}` };
  const config = path.join(dir, 'config.toml'), state = path.join(dir, '.contextshrink-npx-codex.json');
  const run = (args = [], input = 'test@example.test\ntest-pass\n') => new Promise(resolve => {
    const child = execFile(process.execPath, [BIN, ...args], { cwd: root, env, timeout: 10000, windowsHide: true }, (err, stdout, stderr) => resolve({ code: err ? err.code || 1 : 0, stdout, stderr, output: stdout + stderr }));
    child.stdin.end(input);
  });
  try { await fn({ root, home, dir, env, config, state, calls, live, issued, run }); }
  finally { await new Promise(resolve => server.close(resolve)); fs.rmSync(root, { recursive: true, force: true }); }
}

test('connect configures a ChatGPT-auth Responses provider with separate gateway header', () => fixture(async f => {
  const original = '# keep comment\nmodel = "my-existing-model"\n[features]\nfoo = true\n';
  fs.writeFileSync(f.config, original);
  const res = await f.run(); assert.equal(res.code, 0, res.output);
  const raw = fs.readFileSync(f.config, 'utf8'), config = parse(raw), provider = config.model_providers[ID];
  assert.equal(config.model_provider, ID); assert.equal(config.model, 'my-existing-model'); assert.equal(config.features.foo, true);
  assert.ok(raw.includes(original));
  assert.equal(provider.requires_openai_auth, true); assert.equal(provider.supports_websockets, true);
  assert.equal(provider.wire_api, 'responses'); assert.match(provider.base_url, /\/k\/[a-f0-9]{24}\/v1$/);
  assert.equal(provider.http_headers['X-Horizon-Proxy-Token'], f.issued[0]);
  assert.equal(provider.http_headers['X-Horizon-Model-Catalog'], 'codex');
  assert.ok(!provider.env_key); assert.ok(!provider.auth);
  const created = f.calls.find(c => c.path === '/keys'); assert.deepEqual(created.body.scopes, ['proxy:responses']);
  assert.ok(!res.output.includes(f.issued[0]));
  const status = await f.run(['status']); assert.equal(status.code, 0, status.output);
  assert.match(status.stdout, /Configured for 127.0.0.1/); assert.ok(!status.output.includes(f.issued[0]));
  const off = await f.run(['disconnect']); assert.equal(off.code, 0, off.output);
  assert.equal(fs.readFileSync(f.config, 'utf8'), original); assert.ok(!fs.existsSync(f.state));
}));

test('second connect needs no credentials and is byte-for-byte idempotent', () => fixture(async f => {
  assert.equal((await f.run()).code, 0);
  const before = fs.readFileSync(f.config, 'utf8'), calls = f.calls.length;
  assert.equal((await f.run([], '')).code, 0);
  assert.equal(fs.readFileSync(f.config, 'utf8'), before); assert.equal(f.calls.length, calls);
}));

for (const original of ['', 'model_provider = "openai"', '# settings\r\nmodel_provider = "openai" # mine\r\n[features]\r\nfoo = true\r\n']) {
  test('round-trip restores original formatting: ' + JSON.stringify(original.slice(0, 35)), () => fixture(async f => {
    fs.writeFileSync(f.config, original);
    assert.equal((await f.run()).code, 0);
    const off = await f.run(['disconnect']); assert.equal(off.code, 0, off.output);
    assert.equal(fs.readFileSync(f.config, 'utf8'), original);
  }));
}
test('a new config is removed on disconnect without touching auth or other files', () => fixture(async f => {
  fs.writeFileSync(path.join(f.dir, 'auth.json'), '{"auth_mode":"chatgpt","tokens":{"fake":"unchanged"}}');
  assert.equal((await f.run()).code, 0); assert.equal((await f.run(['disconnect'])).code, 0);
  assert.ok(!fs.existsSync(f.config));
  assert.equal(fs.readFileSync(path.join(f.dir, 'auth.json'), 'utf8'), '{"auth_mode":"chatgpt","tokens":{"fake":"unchanged"}}');
}));

for (const original of ['bad = [', 'model_provider = "horizon"\n', 'openai_base_url = "http://other.example"\n', '[profiles.work]\nmodel_provider="mine"\n', '[model_providers.contextshrink_npx]\nname="mine"\n']) {
  test('refuses conflicting/invalid routing before sign-in: ' + original.split('\n')[0], () => fixture(async f => {
    fs.writeFileSync(f.config, original);
    const r = await f.run(); assert.equal(r.code, 1); assert.equal(f.calls.length, 0);
    assert.equal(fs.readFileSync(f.config, 'utf8'), original); assert.ok(!fs.existsSync(f.state));
  }));
}
test('API-key Codex auth is refused without reading or changing its key', () => fixture(async f => {
  const auth = '{"auth_mode":"apikey","OPENAI_API_KEY":"fake-private-api-key"}';
  fs.writeFileSync(path.join(f.dir, 'auth.json'), auth);
  const r = await f.run(); assert.equal(r.code, 1); assert.equal(f.calls.length, 0);
  assert.ok(!r.output.includes('fake-private-api-key')); assert.equal(fs.readFileSync(path.join(f.dir, 'auth.json'), 'utf8'), auth);
}));
test('wrong password leaves config and key list unchanged', () => fixture(async f => {
  const r = await f.run([], 'test@example.test\nwrong\n'); assert.equal(r.code, 1);
  assert.ok(!fs.existsSync(f.config)); assert.equal(f.live.size, 0);
}));
test('unrelated edits before and after the block survive disconnect', () => fixture(async f => {
  const original = 'model_provider="openai"\nmodel="keep"\n'; fs.writeFileSync(f.config, original);
  assert.equal((await f.run()).code, 0);
  fs.writeFileSync(f.config, fs.readFileSync(f.config, 'utf8').replace('model="keep"', 'model="edited"') + '\n[features]\nnew_feature=true\n');
  const off = await f.run(['disconnect']); assert.equal(off.code, 0, off.output);
  const raw = fs.readFileSync(f.config, 'utf8'); assert.ok(raw.includes('model="edited"')); assert.ok(raw.includes('new_feature=true'));
  assert.equal(parse(raw).model_provider, 'openai'); assert.ok(!parse(raw).model_providers);
}));
test('edits to managed routing block disconnect and logout before revocation', () => fixture(async f => {
  assert.equal((await f.run()).code, 0);
  const raw = fs.readFileSync(f.config, 'utf8').replace('supports_websockets = true', 'supports_websockets = false'); fs.writeFileSync(f.config, raw);
  const off = await f.run(['logout']); assert.equal(off.code, 1); assert.equal(f.live.size, 1);
  assert.equal(fs.readFileSync(f.config, 'utf8'), raw); assert.ok(fs.existsSync(f.state));
}));
test('login rotates only this connector key and restores on logout', () => fixture(async f => {
  assert.equal((await f.run()).code, 0); assert.equal((await f.run(['login'])).code, 0);
  assert.equal(f.live.size, 1); assert.equal(f.issued.length, 2);
  assert.ok(fs.readFileSync(f.config, 'utf8').includes(f.issued[1]));
  assert.equal((await f.run(['logout'])).code, 0); assert.equal(f.live.size, 0); assert.ok(!fs.existsSync(f.config));
}));
test('pasted keys require no login and are not revoked by logout', () => fixture(async f => {
  assert.equal((await f.run(['login', '--key'], KEY + '\n')).code, 0); assert.equal(f.calls.length, 0);
  assert.equal((await f.run(['logout'], '')).code, 0); assert.equal(f.calls.length, 0);
}));
test('savings uses the existing key endpoint and never prints credentials', () => fixture(async f => {
  assert.equal((await f.run()).code, 0);
  const r = await f.run(['savings']); assert.equal(r.code, 0, r.output);
  assert.match(r.stdout, /trimmed 25% · net saved \$0.12 · 4 req · this key today/);
  assert.ok(!r.output.includes(f.issued[0]));
}));
test('a connector lock prevents a second writer without deleting the other lock', () => fixture(async f => {
  fs.writeFileSync(f.state + '.lock', 'other operation');
  assert.equal((await f.run()).code, 1); assert.equal(f.calls.length, 0);
  assert.equal(fs.readFileSync(f.state + '.lock', 'utf8'), 'other operation');
}));
test('multiline TOML cannot trick the editor into silently changing another field', () => {
  const original = 'description = """\nmodel_provider = "openai"\n"""\n';
  assert.throws(() => connectText(original, 'https://example.test/v1', KEY), /safely/);
});
test('invalid arguments do not initialize or read personal config', () => fixture(async f => {
  for (const args of [['no-such-command'], ['connect', '--key'], ['disconnect', '--force'], ['chats'], ['chats', 'list', '--apply'], ['chats', 'migrate'], ['chats', 'migrate', '--all', 'bad-id']]) assert.equal((await f.run(args)).code, 2);
  assert.equal((await f.run(['--version'])).stdout.trim(), require('../package.json').version); assert.equal(f.calls.length, 0);
}));
test('version-one preview connections remain readable by the new package', () => fixture(async f => {
  assert.equal((await f.run()).code, 0);
  const state = JSON.parse(fs.readFileSync(f.state)); state.version = 1; fs.writeFileSync(f.state, JSON.stringify(state));
  assert.equal((await f.run(['status'])).code, 0);
  assert.equal((await f.run(['disconnect'])).code, 0);
}));
