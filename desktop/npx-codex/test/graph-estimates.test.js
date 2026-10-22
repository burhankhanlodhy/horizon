const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { spawnSync } = require("node:child_process");
const core = require("../lib/graph-core.cjs");
const config = require("../lib/graph-config.cjs");
const estimates = require("../lib/graph-estimates.cjs");

function fixture(t) {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), "cs-ge-"));
  const root = path.join(base, "r"),
    home = path.join(base, "h");
  fs.mkdirSync(root);
  fs.mkdirSync(home);
  assert.equal(
    spawnSync("git", ["init", "-q", root], { windowsHide: true }).status,
    0,
  );
  const env = { USERPROFILE: home, HOME: home };
  const p = core.project(root, env);
  fs.mkdirSync(p.mirror, { recursive: true });
  const source = Array.from(
    { length: 800 },
    (_, i) => `function example_${i}() { return ${i}; }`,
  ).join("\n");
  fs.writeFileSync(path.join(root, "example.js"), source);
  fs.writeFileSync(path.join(p.mirror, "example.js"), source);
  core.atomic(p.state, JSON.stringify({ ...core.snapshot(p), root }));
  t.after(() => {
    const target = path.resolve(base);
    assert.ok(target.startsWith(path.resolve(os.tmpdir()) + path.sep));
    assert.match(path.basename(target), /^cs-ge-/);
    fs.rmSync(target, { recursive: true, force: true, maxRetries: 3 });
  });
  return { root, env, p };
}

test("local tokenizer uses o200k without special-token injection", () => {
  assert.equal(estimates.count("hello world"), 2);
  assert.ok(estimates.count("<|endoftext|>") > 0);
  const { tools } = require("../lib/graph-mcp.cjs");
  const { guidance, instructions } = require("../lib/graph-policy.cjs");
  // A per-call retrieval allowance is diagnostic, not proof of session savings.
  assert.ok(estimates.count(JSON.stringify(tools) + "\n" + guidance + "\n" + instructions) <= estimates.OVERHEAD);
});

test("default responses are compact without requiring agents to tune every call", () => {
  const value = core.bounded({ data: { results: Array.from({ length: 40 }, (_, i) => ({ name: `symbol_${i}`, file_path: "src/example.py", start_line: i + 1, signature: "x".repeat(120) })) } });
  assert.ok(Buffer.byteLength(JSON.stringify(value)) <= 3000);
  assert.ok(value.truncated);
  assert.ok(value.data.results.length > 0);
  assert.equal(value.data.results[0].start_line, 1);
});

test("receipts cap alternatives, deduplicate repeated queries and never carry source", (t) => {
  const f = fixture(t),
    args = { query: "example", intent: "discovery", budget_bytes: 6000 };
  const result = {
    project: f.root,
    fresh: true,
    data: { results: [{ name: "example_1" }] },
  };
  const a = estimates.attach(f.root, args, result, f.env);
  const b = estimates.attach(f.root, args, result, f.env);
  assert.equal(a.contextshrink_graph_estimate.baseline_tokens, 4000);
  assert.deepEqual(
    a.contextshrink_graph_estimate,
    b.contextshrink_graph_estimate,
  );
  assert.equal(a.contextshrink_graph_estimate.overhead_tokens, 1024);
  assert.ok(
    !JSON.stringify(a.contextshrink_graph_estimate).includes("example_1"),
  );
  assert.equal(
    estimates.attach(f.root, { query: "xx", intent: "discovery" }, result, f.env),
    result,
  );
  const empty = { ...result, data: { results: [] } };
  assert.equal(estimates.attach(f.root, args, empty, f.env), empty);
  fs.appendFileSync(path.join(f.root, "example.js"), "\n// changed");
  assert.equal(estimates.attach(f.root, args, result, f.env), result);
});

test("lookups and file-scoped searches never receive a broad discovery receipt", (t) => {
  const f = fixture(t);
  const value = { fresh: true, data: { results: [{ name: "example_1" }] } };
  for (const args of [
    { query: "example" },
    { query: "example", intent: "lookup" },
    { query: "example", intent: "other" },
    { query: "example", intent: "discovery", file_pattern: "*.js" },
  ]) assert.equal(estimates.attach(f.root, args, value, f.env), value);
  assert.ok(estimates.attach(f.root, { query: "example", intent: "discovery" }, value, f.env).contextshrink_graph_estimate);
});

test("one oversized source line cannot inflate the bounded alternative", (t) => {
  const f = fixture(t);
  const source = "example " + "a".repeat(100000);
  fs.writeFileSync(path.join(f.root, "example.js"), source);
  fs.writeFileSync(path.join(f.p.mirror, "example.js"), source);
  core.atomic(
    f.p.state,
    JSON.stringify({ ...core.snapshot(f.p), root: f.root }),
  );
  const alternative = estimates.baseline(f.p, "example");
  assert.equal(
    alternative.tokens,
    estimates.count(("example.js:1:" + source + "\n").slice(0, 16000)),
  );
  assert.ok(alternative.tokens < 4000);
});

test("receipt metadata never displaces useful results to exceed byte budget", (t) => {
  const f = fixture(t),
    value = {
      project: f.root,
      fresh: true,
      data: { results: [{ name: "x".repeat(650) }] },
    };
  assert.equal(
    estimates.attach(
      f.root,
      { query: "example", intent: "discovery", budget_bytes: 1000 },
      value,
      f.env,
    ),
    value,
  );
});

test("owned old integration upgrades runtime and retains unrelated project content", (t) => {
  const f = fixture(t),
    installed = config.install(f.root, f.env);
  const record = path.join(f.p.dir, "integration.json"),
    state = core.json(record);
  state.runtimeVersion = "0.2.2";
  core.atomic(record, JSON.stringify(state));
  fs.appendFileSync(installed.agents, "\nKeep my own instructions.\n");
  const upgraded = config.install(f.root, f.env);
  assert.equal(upgraded.upgraded, true);
  assert.equal(core.json(record).runtimeVersion, require("../package.json").version);
  assert.ok(fs.readFileSync(installed.agents, "utf8").includes("selectively"));
  assert.ok(
    fs
      .readFileSync(installed.agents, "utf8")
      .includes("Keep my own instructions."),
  );
  assert.equal(config.install(f.root, f.env).alreadyEnabled, true);
});
