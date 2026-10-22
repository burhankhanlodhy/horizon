'use strict';
// Opt-in regression against the actual desktop-bundled Codex engine. All model
// calls go to loopback with fake credentials and an isolated CODEX_HOME.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const readline = require('node:readline');
const { spawn } = require('node:child_process');
const chats = require('../lib/chats');
const { ID } = require('../lib/toml');
const executable = process.env.CONTEXTSHRINK_TEST_CODEX_BIN;
test('real Codex resumes a migrated chat through the new provider and keeps its history on undo', { skip: !executable && 'set CONTEXTSHRINK_TEST_CODEX_BIN', timeout: 45000 }, async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'cs-engine-test-'));
  const dir = path.join(root, '.codex'); fs.mkdirSync(dir);
  const clients = new Set(), requests = [];
  const server = http.createServer((req, res) => {
    if (!req.url.endsWith('/responses')) { res.setHeader('content-type', 'application/json'); return res.end('{"models":[]}'); }
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => {
      requests.push({ path: req.url, headers: req.headers, body: JSON.parse(body) });
      const message = { id: 'msg_' + requests.length, type: 'message', role: 'assistant', status: 'completed', content: [{ type: 'output_text', text: 'fixture reply', annotations: [] }] };
      const response = { id: 'resp_' + requests.length, object: 'response', status: 'completed', output: [message], usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 } };
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      for (const event of [
        { type: 'response.created', response: { ...response, status: 'in_progress', output: [] } },
        { type: 'response.output_item.added', output_index: 0, item: { ...message, status: 'in_progress', content: [] } },
        { type: 'response.output_text.delta', output_index: 0, content_index: 0, item_id: message.id, delta: 'fixture reply' },
        { type: 'response.output_item.done', output_index: 0, item: message },
        { type: 'response.completed', response },
      ]) res.write(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
      res.end();
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const env = { SystemRoot: process.env.SystemRoot || '', PATH: process.env.PATH, HOME: root, USERPROFILE: root, CODEX_HOME: dir,
    APPDATA: path.join(root, 'roaming'), LOCALAPPDATA: path.join(root, 'local'), TEMP: root, TMP: root,
    OPENAI_BASE_URL: base + '/before/v1', OPENAI_API_KEY: 'fake-engine-fixture-key' };
  fs.writeFileSync(path.join(dir, 'config.toml'), `model = "gpt-5.1-codex"\nmodel_provider = "fixture_original"\nchatgpt_base_url = "${base}"\n[features]\nhooks = false\n[model_providers.fixture_original]\nname = "Fixture original"\nbase_url = "${base}/before/v1"\nwire_api = "responses"\n[model_providers.${ID}]\nname = "ContextShrink"\nbase_url = "${base}/after/v1"\nwire_api = "responses"\nhttp_headers = { "X-Horizon-Proxy-Token" = "fake-fixture-gateway-key" }\n`);
  async function start() {
    const child = spawn(executable, ['app-server', '--listen', 'stdio://'], { env, cwd: root, windowsHide: true });
    const pending = new Map(), events = []; let seq = 0, exited = false;
    child.stderr.resume();
    child.once('exit', () => { exited = true; for (const p of pending.values()) { clearTimeout(p.timer); p.reject(new Error('engine exited')); } });
    child.once('error', () => { for (const p of pending.values()) { clearTimeout(p.timer); p.reject(new Error('engine spawn failed')); } });
    const lines = readline.createInterface({ input: child.stdout });
    lines.on('line', line => {
      let message; try { message = JSON.parse(line); } catch { return; }
      if (message.method) events.push(message);
      if (pending.has(message.id)) {
        const p = pending.get(message.id); pending.delete(message.id); clearTimeout(p.timer);
        message.error ? p.reject(new Error(JSON.stringify(message.error))) : p.resolve(message.result);
      }
    });
    const call = (method, params) => new Promise((resolve, reject) => {
      const id = ++seq, timer = setTimeout(() => { pending.delete(id); reject(new Error('RPC timed out: ' + method)); }, 10000);
      pending.set(id, { resolve, reject, timer }); child.stdin.write(JSON.stringify({ id, method, params }) + '\n');
    });
    const c = { call, events, async close() {
      if (exited) return;
      await new Promise(resolve => { child.once('exit', resolve); child.stdin.end(); setTimeout(() => { child.kill(); resolve(); }, 1500).unref(); });
      lines.close(); clients.delete(c);
    } };
    clients.add(c);
    await call('initialize', { clientInfo: { name: 'contextshrink_engine_test', version: '0.1.1' }, capabilities: { experimentalApi: true } });
    child.stdin.write(JSON.stringify({ method: 'initialized', params: {} }) + '\n');
    return c;
  }
  async function turn(c, threadId, text) {
    c.events.length = 0;
    await c.call('turn/start', { threadId, input: [{ type: 'text', text, text_elements: [] }] });
    const deadline = Date.now() + 10000;
    while (!c.events.some(e => e.method === 'turn/completed')) {
      if (Date.now() > deadline) throw new Error('Fake model turn timed out');
      await new Promise(resolve => setTimeout(resolve, 20));
    }
    assert.equal(c.events.find(e => e.method === 'turn/completed').params.turn.status, 'completed');
    await c.call('thread/unsubscribe', { threadId });
  }
  try {
    let c = await start();
    const original = await c.call('thread/start', { cwd: root, sandbox: 'read-only', approvalPolicy: 'never', historyMode: 'legacy' });
    const id = original.thread.id;
    await turn(c, id, 'original fixture prompt'); await c.close();
    // Generate a real engine history against a mock HTTP provider, then give
    // that isolated fixture the incoming built-in-provider metadata. This avoids
    // the built-in provider's WebSocket negotiation contacting a real service.
    const bytes = fs.readFileSync(original.thread.path), end = bytes.indexOf(10)+1;
    const header = JSON.parse(bytes.subarray(0,end).toString()); header.payload.model_provider = 'openai';
    fs.writeFileSync(original.thread.path, Buffer.concat([Buffer.from(JSON.stringify(header)+'\n'), bytes.subarray(end)]));
    const { DatabaseSync } = require('node:sqlite');
    const fixtureDb = new DatabaseSync(path.join(dir, 'state_5.sqlite'));
    fixtureDb.prepare('UPDATE threads SET model_provider = ? WHERE id = ?').run('openai', id); fixtureDb.close();
    const originalBytes = fs.readFileSync(original.thread.path);
    chats.migrate({ dir }, { ids: [id], apply: true }, { assertClosed() {} });
    c = await start();
    const migrated = await c.call('thread/read', { threadId: id, includeTurns: false });
    assert.equal(migrated.thread.id, id); assert.equal(migrated.thread.modelProvider, ID);
    const resumed = await c.call('thread/resume', { threadId: id, modelProvider: migrated.thread.modelProvider, excludeTurns: true });
    assert.equal(resumed.modelProvider, ID);
    await turn(c, id, 'after migration fixture prompt'); await c.close();
    assert.equal(requests[0].path, '/before/v1/responses');
    assert.equal(requests[1].path, '/after/v1/responses');
    assert.equal(requests[1].headers['x-horizon-proxy-token'], 'fake-fixture-gateway-key');
    assert.ok(JSON.stringify(requests[1].body).includes('original fixture prompt'));
    const afterTurns = fs.readFileSync(original.thread.path);
    chats.restore({ dir }, { all: true, apply: true }, { assertClosed() {} });
    const restored = fs.readFileSync(original.thread.path);
    assert.deepEqual(restored.subarray(0, originalBytes.indexOf(10)+1), originalBytes.subarray(0, originalBytes.indexOf(10)+1));
    assert.deepEqual(restored.subarray(restored.indexOf(10)+1), afterTurns.subarray(afterTurns.indexOf(10)+1));
    c = await start();
    const read = await c.call('thread/read', { threadId: id, includeTurns: false });
    assert.equal(read.thread.modelProvider, 'openai');
    const restoredSession = await c.call('thread/resume', { threadId: id, modelProvider: read.thread.modelProvider, excludeTurns: true });
    assert.equal(restoredSession.modelProvider, 'openai'); await c.close();
  } catch (error) { console.error('Engine fixture failure:', error.message); throw error; }
  finally {
    for (const c of clients) await c.close();
    await new Promise(resolve => server.close(resolve));
    // Some Windows engines retain an empty directory handle briefly at exit.
    try { fs.rmSync(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 30 }); }
    catch { /* The sandbox may retain an engine-created directory or lock file. All fixture credentials are fake. */ }
  }
});
