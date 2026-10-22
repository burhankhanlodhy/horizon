"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { spawnSync } = require("node:child_process");
const core = require("../lib/graph-core.cjs");
const config = require("../lib/graph-config.cjs");
const { parse } = require("smol-toml");
const { render, exportMap } = require("../lib/graph-export.cjs");
const vm = require("node:vm");
function fixture(t) {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), "cs-graph-")),
    root = path.join(base, "r"),
    home = path.join(base, "h");
  fs.mkdirSync(root);
  fs.mkdirSync(home);
  const r = spawnSync("git", ["init", "-q", root], { windowsHide: true });
  assert.equal(r.status, 0);
  t.after(() => {
    const target = path.resolve(base);
    assert.ok(target.startsWith(path.resolve(os.tmpdir()) + path.sep));
    assert.match(path.basename(target), /^cs-graph-/);
    fs.rmSync(target, { recursive: true, force: true, maxRetries: 3 });
  });
  return {
    base,
    root,
    home,
    env: { USERPROFILE: home, HOME: home },
    write(name, data) {
      const p = path.join(root, name);
      fs.mkdirSync(path.dirname(p), { recursive: true });
      fs.writeFileSync(p, data);
    },
  };
}
test("graph snapshot excludes secrets, ignored/generated files and supports uncommitted changes", (t) => {
  const f = fixture(t);
  f.write(".gitignore", "ignored.py\n");
  f.write("ignored.py", "secret=1");
  f.write(".env", "TOKEN=fake");
  f.write("secrets.json", '{"secret":"fake"}');
  f.write("node_modules/pkg/x.js", "function excluded(){}");
  f.write("src/auth.py", "def login():\n    return True\n");
  spawnSync("git", ["-C", f.root, "add", "-f", ".env"], { windowsHide: true });
  const p = core.project(f.root, f.env),
    a = core.snapshot(p);
  assert.deepEqual(
    a.files.map((x) => x.path),
    ["src/auth.py"],
  );
  assert.equal(a.head, "unborn");
  f.write("src/auth.py", 'def login():\n    return "modified"\n');
  assert.notEqual(core.snapshot(p).fingerprint, a.fingerprint);
  f.write("src/new.py", "def next(): pass\n");
  assert.equal(core.snapshot(p).files.length, 2);
});
test("graph resolves nested folders to the same real Git root", (t) => {
  const f = fixture(t);
  f.write("src/nested/example.py", "def example(): pass\n");
  const root = core.project(f.root, f.env);
  const nested = core.project(path.join(f.root, "src", "nested"), f.env);
  assert.equal(nested.root, root.root);
  assert.equal(nested.id, root.id);
});

test("graph snapshot refuses links escaping the project", (t) => {
  const f = fixture(t);
  f.write("ok.py", "def ok(): pass\n");
  const external = path.join(f.base, "outside.py");
  fs.writeFileSync(external, 'secret = "fake"');
  try {
    fs.symlinkSync(external, path.join(f.root, "linked.py"));
  } catch (e) {
    if (["EPERM", "EACCES"].includes(e.code)) {
      t.skip("File symlinks require Windows developer mode.");
      return;
    }
    throw e;
  }
  assert.deepEqual(
    core.snapshot(core.project(f.root, f.env)).files.map((x) => x.path),
    ["ok.py"],
  );
});
test("compact search removes only internal identifiers and scores while preserving ordered citations", () => {
  const data = { total: 2, has_more: true, search_mode: "bm25", results: [
    { name: "run", label: "Method", file_path: "src/a.py", start_line: 3, end_line: 8, qualified_name: "internal.project.A.run", rank: -4.5, signature: "run(self)" },
    { name: "run", label: "Method", file_path: "src/a.py", start_line: 20, end_line: 26, qualified_name: "internal.project.B.run", rank: -2.1 },
  ] };
  const before = structuredClone(data);
  const result = core.compactSearch(data);
  assert.deepEqual(data, before);
  assert.equal(result.total, data.total);
  assert.equal(result.has_more, true);
  assert.equal(result.search_mode, "bm25");
  assert.deepEqual(result.results, data.results.map(({ qualified_name, rank, ...citation }) => citation));
  assert.ok(JSON.stringify(result).length < JSON.stringify(data).length);
});

test("bounded graph output stays valid JSON within its UTF-8 byte budget", () => {
  const data = {
    project: "demo",
    fresh: true,
    data: {
      results: Array.from({ length: 100 }, (_, n) => ({
        name: "名".repeat(100),
        n,
      })),
    },
  };
  const r = core.bounded(data, 1000);
  assert.ok(Buffer.byteLength(JSON.stringify(r)) <= 1000);
  assert.equal(r.truncated, true);
  assert.equal(data.data.results.length, 100);
  assert.throws(() => core.bounded(data, 999), core.GraphError);
});
test("graph engine platform selection and tamper checks fail closed", async (t) => {
  const f = fixture(t),
    p = core.project(f.root, f.env),
    binary = core.engineLocation(p);
  assert.throws(() => core.platformAsset("win32", "arm64"), core.GraphError);
  assert.match(core.platformAsset("win32", "x64").sha256, /^[a-f0-9]{64}$/);
  core.atomic(binary, "not an executable");
  core.atomic(
    binary + ".json",
    JSON.stringify({ version: "v0.8.1", sha256: "bad" }),
  );
  await assert.rejects(core.installEngine(p), /verification failed/);
});
test("graph project integration preserves config and instructions, and disables cleanly", (t) => {
  const f = fixture(t);
  f.write(".codex/config.toml", 'model = "gpt-6.1-sol"\n# preserved');
  f.write("AGENTS.md", "Keep existing instructions.");
  const before = fs.readFileSync(
      path.join(f.root, ".codex/config.toml"),
      "utf8",
    ),
    a = fs.readFileSync(path.join(f.root, "AGENTS.md"), "utf8");
  const enabled = config.install(f.root, f.env);
  assert.equal(enabled.enabled, true);
  const doc = parse(fs.readFileSync(enabled.config, "utf8"));
  assert.equal(doc.model, "gpt-6.1-sol");
  assert.ok(doc.mcp_servers.contextshrink_graph.args.includes(f.root));
  assert.ok(fs.existsSync(doc.mcp_servers.contextshrink_graph.args[0]));
  assert.equal(config.install(f.root, f.env).alreadyEnabled, true);
  fs.appendFileSync(enabled.config, "\n# unrelated addition\n");
  fs.appendFileSync(enabled.agents, "\nMore user guidance.\n");
  config.disable(f.root, f.env);
  assert.equal(
    fs.readFileSync(enabled.config, "utf8"),
    before + "\n# unrelated addition\n",
  );
  assert.equal(
    fs.readFileSync(enabled.agents, "utf8"),
    a + "\nMore user guidance.\n",
  );
});
test("graph disable refuses to erase edited owned settings", (t) => {
  const f = fixture(t);
  const enabled = config.install(f.root, f.env);
  fs.appendFileSync(enabled.agents, "\nkeep\n");
  const text = fs
    .readFileSync(enabled.config, "utf8")
    .replace("startup_timeout_sec = 120", "startup_timeout_sec = 42");
  fs.writeFileSync(enabled.config, text);
  assert.throws(() => config.disable(f.root, f.env), /changed/);
  assert.equal(fs.readFileSync(enabled.config, "utf8"), text);
});
test("graph init refuses existing foreign MCP configuration", (t) => {
  const f = fixture(t);
  f.write(
    ".codex/config.toml",
    '[mcp_servers.contextshrink_graph]\ncommand="other"\n',
  );
  assert.throws(() => config.install(f.root, f.env), /already exists/);
  assert.ok(!fs.existsSync(path.join(f.root, "AGENTS.md")));
});
test("project graph lock prevents concurrent cache mutation", async (t) => {
  const f = fixture(t),
    p = core.project(f.root, f.env);
  fs.mkdirSync(p.dir, { recursive: true });
  fs.writeFileSync(p.lock, "");
  await assert.rejects(
    core.withLock(p, async () => {}),
    /Another graph/,
  );
});
test("offline graph map escapes source data and contains valid JavaScript", () => {
  const html = render({
    project: "demo",
    indexedAt: "now",
    totalNodes: 1,
    nodes: [
      {
        id: "x",
        name: "</script><script>alert(1)</script>",
        kind: "Function",
        file: "x.py",
        line: 1,
      },
    ],
    edges: [],
  });
  assert.ok(!html.includes("<script>alert(1)"));
  assert.ok(html.includes("connect-src 'none'"));
  assert.ok(!/<script[^>]+src=/.test(html));
  const scripts = [...html.matchAll(/<script(?:[^>]*)>([\s\S]*?)<\/script>/g)];
  assert.equal(scripts.length, 2);
  assert.equal(
    JSON.parse(scripts[0][1]).nodes[0].name,
    "</script><script>alert(1)</script>",
  );
  new vm.Script(scripts[1][1]);
});
test(
  "pinned native engine indexes, traces, refreshes and serves Codex MCP offline",
  { skip: !process.env.CONTEXTSHRINK_TEST_CBM_BIN },
  async (t) => {
    const f = fixture(t),
      binary = process.env.CONTEXTSHRINK_TEST_CBM_BIN,
      options = { binary };
    f.write(
      "auth.py",
      'def validate_token(token):\n    return token == "demo"\n\ndef login(token):\n    return validate_token(token)\n',
    );
    f.write(
      "app.py",
      "from auth import login\n\ndef handle_request(token):\n    return login(token)\n\nclass ExampleHandler:\n    def invoke(self, token):\n        return login(token)\n" +
        Array.from({ length: 15 }, (_, i) => `\ndef lookup_${i}():\n    return ${i}\n`).join(""),
    );
    f.write(".env", "FAKE_SECRET=never_index");
    const initial = await core.refresh(f.root, f.env, options);
    assert.ok(initial.nodes >= 8);
    assert.ok(initial.edges >= 3);
    assert.equal(initial.indexedFiles, 2);
    let q = await core.query(
      f.root,
      "search",
      { query: "validate token" },
      f.env,
      options,
    );
    assert.equal(q.fresh, true);
    assert.equal(q.data.results[0].file_path, "auth.py");
    assert.equal(q.data.results[0].start_line, 1);
    assert.equal(q.data.results[0].name, "validate_token");
    assert.equal(q.budgetBytes, 3000);
    assert.ok(q.data.results.every((node) => !("qualified_name" in node) && !("rank" in node)));
    const compact = await core.query(f.root, "search", { query: "lookup" }, f.env, options);
    assert.equal(compact.data.results.length, 5);
    const expanded = await core.query(f.root, "search", { query: "lookup", limit: 10, budget_bytes: 6000 }, f.env, options);
    assert.equal(expanded.data.results.length, 10);
    const trace = await core.query(
      f.root,
      "trace",
      { symbol: "login" },
      f.env,
      options,
    );
    assert.ok(JSON.stringify(trace.data).includes("validate_token"));
    await assert.rejects(
      core.query(f.root, "trace", { symbol: "not_a_function" }, f.env, options),
      /exact function or method/,
    );
    // Some classes are traceable in the native engine. Do not blanket-reject
    // them; an unavailable symbol should provide a source-search fallback.
    assert.ok((await core.query(f.root, "trace", { symbol: "ExampleHandler" }, f.env, options)).data);
    assert.ok(
      (await core.query(f.root, "overview", {}, f.env, options)).data
        .total_nodes >= 8,
    );
    const cypher = await core.query(
      f.root,
      "query",
      { query: "MATCH (n:Function) RETURN n.name LIMIT 10" },
      f.env,
      options,
    );
    assert.ok(JSON.stringify(cypher.data).includes("handle_request"));
    const map = await exportMap(f.root, undefined, f.env, options);
    assert.ok(map.shownNodes >= 8);
    assert.ok(map.shownEdges >= 3);
    assert.ok(fs.readFileSync(map.file, "utf8").includes("validate_token"));
    await assert.rejects(
      exportMap(f.root, map.file, f.env, options),
      /already exists/,
    );
    await assert.rejects(
      core.query(
        f.root,
        "query",
        { query: "MATCH (n) DELETE n" },
        f.env,
        options,
      ),
      /read-only/,
    );
    await assert.rejects(
      core.query(
        f.root,
        "query",
        { query: "MATCH (n) RETURN n LIMIT 1000" },
        f.env,
        options,
      ),
      /LIMIT/,
    );
    f.write(
      "auth.py",
      "def validate_session(token):\n    return bool(token)\n",
    );
    assert.equal(core.status(f.root, f.env).fresh, false);
    q = await core.query(
      f.root,
      "search",
      { query: "validate session" },
      f.env,
      options,
    );
    assert.equal(q.fresh, true);
    assert.ok(q.data.results.some((x) => x.name === "validate_session"));
    const removed = await core.query(
      f.root,
      "query",
      { query: "MATCH (n:Function) RETURN n.name LIMIT 20" },
      f.env,
      options,
    );
    assert.ok(!JSON.stringify(removed.data).includes("validate_token"));
    await assert.rejects(
      core.query(
        f.root,
        "query",
        { query: "MATCH (n) RETURN n.name LIMIT 1 + 100" },
        f.env,
        options,
      ),
      /LIMIT/,
    );
    const literalLimit = await core.query(
      f.root,
      "query",
      {
        query: "MATCH (n:Function) WHERE n.name = 'LIMIT 1000' RETURN n.name",
      },
      f.env,
      options,
    );
    assert.equal(literalLimit.data.rows.length, 0);
    fs.unlinkSync(path.join(f.root, "auth.py"));
    await core.refresh(f.root, f.env, options);
    const deleted = await core.query(
      f.root,
      "query",
      { query: "MATCH (n:Function) RETURN n.name LIMIT 20" },
      f.env,
      options,
    );
    assert.ok(!JSON.stringify(deleted.data).includes("validate_session"));
    const p = core.project(f.root, f.env),
      installed = core.engineLocation(p);
    fs.mkdirSync(path.dirname(installed), { recursive: true });
    fs.copyFileSync(binary, installed);
    core.atomic(
      installed + ".json",
      JSON.stringify({
        version: "v0.8.1",
        sha256: core.hash(fs.readFileSync(installed)),
      }),
    );
    const integration = config.install(f.root, f.env),
      mcp = parse(fs.readFileSync(integration.config, "utf8")).mcp_servers
        .contextshrink_graph;
    const requests = [
      {
        jsonrpc: "2.0",
        id: 1,
        method: "initialize",
        params: { protocolVersion: "2025-03-26" },
      },
      { jsonrpc: "2.0", method: "notifications/initialized" },
      { jsonrpc: "2.0", id: 2, method: "tools/list" },
      {
        jsonrpc: "2.0",
        id: 3,
        method: "tools/call",
        params: {
          name: "graph_search",
          arguments: { query: "handle request", budget_bytes: 1000 },
        },
      },
      { jsonrpc: "2.0", id: 4, method: "tools/call", params: { name: "graph_status" } },
      { jsonrpc: "2.0", id: 5, method: "tools/call", params: { name: "graph_query", arguments: { query: "MATCH (n:Function) RETURN n.name LIMIT 5" } } },
    ];
    const result = spawnSync(mcp.command, mcp.args, {
      env: { ...process.env, ...f.env },
      input: requests.map((x) => JSON.stringify(x)).join("\n") + "\n",
      encoding: "utf8",
      windowsHide: true,
      timeout: 30000,
      maxBuffer: 1e6,
    });
    assert.equal(result.status, 0, result.stderr);
    const responses = result.stdout
      .trim()
      .split("\n")
      .map((x) => JSON.parse(x));
    assert.equal(responses.length, 5);
    assert.equal(responses[0].result.protocolVersion, "2025-03-26");
    assert.deepEqual(responses[1].result.tools.map((t) => t.name), ["graph_search", "graph_overview", "graph_trace"]);
    const response = responses[2].result;
    assert.ok(!response.isError, JSON.stringify(response));
    assert.ok(Buffer.byteLength(response.content[0].text) <= 1000);
    assert.ok(response.content[0].text.includes("handle_request"));
    assert.ok(!responses[3].result.isError);
    assert.equal(JSON.parse(responses[3].result.content[0].text).fresh, true);
    assert.ok(!responses[4].result.isError);
    assert.ok(JSON.parse(responses[4].result.content[0].text).data.rows.length > 0);
  },
);
