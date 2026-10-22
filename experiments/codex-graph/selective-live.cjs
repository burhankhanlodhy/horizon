"use strict";
// One paired five-turn session. Keep earlier benchmark evidence unchanged.
const fs = require("node:fs"), path = require("node:path"), os = require("node:os");
const assert = require("node:assert/strict");
const { spawn, spawnSync } = require("node:child_process");
const core = require("../../desktop/npx-codex/lib/graph-core.cjs");
const integration = require("../../desktop/npx-codex/lib/graph-config.cjs");
const ROOT = path.resolve(__dirname, "../..");
const RUN = path.join(ROOT, "desktop/dist-windows/npx-test/selective-live-024");
const WORK = path.join(os.tmpdir(), "cs-s24-" + core.hash(ROOT).slice(0, 8));
const CODEX = process.env.CODEX_EXE || "codex"; // path to codex.exe (set CODEX_EXE)
const ENGINE = path.join(ROOT, "desktop/dist-windows/npx-test/graphify-review/cbm-bin/codebase-memory-mcp.exe");
const AUTH = process.env.CODEX_AUTH || require("node:path").join(require("node:os").homedir(), ".codex", "auth.json");
const MODEL = "gpt-6.1-sol", EFFORT = "high";
const PRICES = { input: 2, cachedInput: 0.1, output: 10, units: "USD per million tokens", source: "https://developers.openai.com/api/docs/pricing", checked: "2026-10-21" };
const prompts = [
  "Review the codebase in this folder. Explain its architecture, key entry points, data flow and main risks, with source citations. Treat this as an initial architecture review, not an exhaustive audit of every file. Keep the answer within 400 words.",
  "Trace a public OpenAI Responses inference request from HTTP entry through authentication, routing, transformation, forwarding and usage reporting. Cite key functions and one supported failure boundary. Reuse evidence already found where possible. Keep the answer within 250 words.",
  "Review horizon/proxy/savings_tracker.py and the relevant pricing tests. Explain how cached input affects estimated compression savings and what happens for unknown pricing. Distinguish actual usage from estimates. Use source citations and stay within 220 words.",
  "Review api/savings_ledger.py, api/billing.py and horizon/proxy/graph_savings.py. Do graph retrieval estimates affect savings-based fees or inference counts in this version? Explain historical rows and receipt replay protection with source citations. Keep the answer within 220 words.",
  "Summarize this review into three practical token-efficiency recommendations with source citations, distinguishing measured facts from hypotheses. Reuse earlier findings and read again only if evidence is missing. Stay within 180 words.",
];
const common = "This is a read-only review of a frozen source snapshot. Do not edit files, install dependencies, access the network, inspect credentials/parent folders, call external APIs/models, or delegate/spawn agents. Use only local source and available retrieval tools. Prefer bounded reads and avoid dumping directories or huge files. Reuse evidence already in this conversation. Cite relative source paths and current line numbers using Markdown links ending #L<number>. State uncertainty and missing coverage honestly.";
const json = core.json;
const atomic = (file, value) => core.atomic(file, JSON.stringify(value, null, 2) + "\n");
const state = () => json(path.join(RUN, "state.json"));
const save = (value) => atomic(path.join(RUN, "state.json"), value);
const home = (arm) => path.join(WORK, arm, "home");
const repo = (arm) => path.join(WORK, arm, "repo");
function env(arm) {
  const allowed = ["PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "TEMP", "TMP", "LANG", "LC_ALL"];
  return { ...Object.fromEntries(Object.entries(process.env).filter(([k]) => allowed.includes(k.toUpperCase()))), HOME: home(arm), USERPROFILE: home(arm), CODEX_HOME: path.join(home(arm), ".codex"), APPDATA: path.join(home(arm), "roaming"), LOCALAPPDATA: path.join(home(arm), "local"), NO_COLOR: "1" };
}
function cleanupAuth() {
  for (const arm of ["A", "B"]) {
    const file = path.join(home(arm), ".codex/auth.json");
    if (fs.existsSync(file)) fs.unlinkSync(file);
  }
}
function verifySources() {
  const manifest = json(path.join(RUN, "source-manifest.json"));
  for (const arm of ["A", "B"]) for (const file of manifest)
    assert.equal(core.hash(fs.readFileSync(path.join(repo(arm), file.path))), file.sha256, "Frozen source changed: " + file.path);
}
async function prepare() {
  assert.ok(!state(), "This experiment already exists; never overwrite it");
  assert.equal(require("../../desktop/npx-codex/package.json").version, "0.2.4");
  const prior = json(path.join(ROOT, "desktop/dist-windows/npx-test/live-graph-21-turns/source-manifest.json"));
  const paths = [...new Set([...prior.map((f) => f.path), "api/savings_ledger.py"])]
    .filter((f) => !f.startsWith("experiments/") && !["desktop/npx-codex/VALIDATION.md", "desktop/npx-codex/GRAPH.md"].includes(f));
  const manifest = paths.map((file) => ({ path: file, sha256: core.hash(fs.readFileSync(path.join(ROOT, file))), bytes: fs.statSync(path.join(ROOT, file)).size }));
  for (const arm of ["A", "B"]) {
    fs.mkdirSync(path.join(RUN, arm), { recursive: true });
    for (const file of manifest) {
      const dest = path.join(repo(arm), file.path);
      fs.mkdirSync(path.dirname(dest), { recursive: true });
      fs.copyFileSync(path.join(ROOT, file.path), dest);
      assert.equal(core.hash(fs.readFileSync(dest)), file.sha256);
    }
    assert.equal(spawnSync("git", ["init", "-q", repo(arm)], { windowsHide: true }).status, 0);
    const h = path.join(home(arm), ".codex");
    fs.mkdirSync(h, { recursive: true, mode: 0o700 });
    fs.writeFileSync(path.join(h, "config.toml"), `model = ${JSON.stringify(MODEL)}\nmodel_provider = "openai"\nmodel_reasoning_effort = ${JSON.stringify(EFFORT)}\nweb_search = "disabled"\n[features]\nmulti_agent = false\n[projects.${JSON.stringify(repo(arm))}]\ntrust_level = "trusted"\n`, { mode: 0o600 });
  }
  const p = core.project(repo("B"), env("B")), binary = core.engineLocation(p);
  fs.mkdirSync(path.dirname(binary), { recursive: true });
  fs.copyFileSync(ENGINE, binary);
  atomic(binary + ".json", { version: "v0.8.1", sha256: core.hash(fs.readFileSync(ENGINE)) });
  integration.install(repo("B"), env("B"));
  const start = Date.now();
  const graph = await core.refresh(repo("B"), env("B"), { binary });
  const initial = { startedAt: new Date().toISOString(), model: MODEL, reasoningEffort: EFFORT, prices: PRICES, sourceFiles: manifest.length, sourceBytes: manifest.reduce((n, f) => n + f.bytes, 0), manifestDigest: core.hash(JSON.stringify(manifest)), graphVersion: "0.2.4", graph: { ...graph, buildSeconds: (Date.now() - start) / 1000, modelTokens: 0 }, prompts, commonInstructions: common, order: ["AB", "BA", "AB", "BA", "AB"], arms: { A: { threadId: null, turns: [] }, B: { threadId: null, turns: [] } } };
  atomic(path.join(RUN, "source-manifest.json"), manifest);
  save(initial);
  console.log(JSON.stringify({ prepared: true, sourceFiles: manifest.length, graph: initial.graph }));
}
function walk(dir) {
  if (!fs.existsSync(dir)) return [];
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) => e.isDirectory() ? walk(path.join(dir, e.name)) : [path.join(dir, e.name)]);
}
function rolloutUsage(arm, id) {
  const files = walk(path.join(home(arm), ".codex/sessions")).filter((f) => f.endsWith(".jsonl") && path.basename(f).includes(id));
  let latest = null;
  for (const file of files) for (const line of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
    let event; try { event = JSON.parse(line); } catch { continue; }
    if (event.type === "event_msg" && event.payload?.type === "token_count" && event.payload.info?.total_token_usage) latest = event.payload.info.total_token_usage;
  }
  return latest;
}
const keys = ["input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"];
const delta = (total, before = {}) => Object.fromEntries(keys.map((k) => [k, (total?.[k] || 0) - (before?.[k] || 0)]));
function cost(u) {
  const uncached = u.input_tokens - u.cached_input_tokens;
  return { uncached_input_tokens: uncached, input_usd: (uncached * PRICES.input + u.cached_input_tokens * PRICES.cachedInput) / 1e6, output_usd: u.output_tokens * PRICES.output / 1e6, total_usd: (uncached * PRICES.input + u.cached_input_tokens * PRICES.cachedInput + u.output_tokens * PRICES.output) / 1e6 };
}
async function turn(arm, index) {
  const s = state(), a = s.arms[arm];
  if (a.turns[index]?.completed) return;
  assert.ok(!a.turns[index], "An interrupted turn is preserved; do not silently retry");
  verifySources();
  const before = a.threadId ? rolloutUsage(arm, a.threadId) : null;
  const args = ["-C", repo(arm), "--approve-for-me", "exec", "--json"];
  if (a.threadId) args.push("resume", a.threadId);
  args.push("-");
  const base = path.join(RUN, arm, `turn-${String(index).padStart(2, "0")}`);
  const out = fs.createWriteStream(base + ".jsonl"), err = fs.createWriteStream(base + ".stderr");
  const events = [], start = Date.now();
  let buffer = "", timeout = false;
  const child = spawn(CODEX, args, { cwd: repo(arm), env: env(arm), windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
  const timer = setTimeout(() => { timeout = true; spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { windowsHide: true }); }, 600000);
  child.stdout.on("data", (data) => {
    out.write(data); buffer += data.toString();
    let end; while ((end = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
      try { events.push(JSON.parse(line)); } catch { /* Raw evidence is retained. */ }
    }
  });
  child.stderr.on("data", (data) => err.write(data));
  child.stdin.end((index === 0 ? common + "\n\n" : "") + prompts[index]);
  const exitCode = await new Promise((resolve, reject) => { child.once("error", reject); child.once("close", resolve); });
  clearTimeout(timer);
  await Promise.all([new Promise((r) => out.end(r)), new Promise((r) => err.end(r))]);
  const threadId = events.find((e) => e.type === "thread.started")?.thread_id || a.threadId;
  if (a.threadId) assert.equal(threadId, a.threadId, "Follow-up changed conversations");
  const completed = events.findLast((e) => e.type === "turn.completed");
  const cumulative = threadId ? rolloutUsage(arm, threadId) : null;
  const usage = cumulative ? delta(cumulative, before || {}) : completed?.usage;
  const items = events.filter((e) => e.type === "item.completed").map((e) => e.item);
  const answer = items.filter((i) => i.type === "agent_message").map((i) => i.text).join("\n\n");
  fs.writeFileSync(base + ".answer.md", answer);
  const record = { index, completed: !!completed && exitCode === 0, exitCode, timeout, seconds: (Date.now() - start) / 1000, usage, cliUsage: completed?.usage || null, cumulativeUsage: cumulative, usageBasis: cumulative ? "delta of rollout cumulative provider usage" : "CLI turn usage", tools: items.filter((i) => ["command_execution", "mcp_tool_call"].includes(i.type)).map((i) => ({ type: i.type, tool: i.tool, server: i.server, status: i.status, exitCode: i.exit_code })), failures: events.filter((e) => e.type === "turn.failed" || e.type === "error").map((e) => e.message || e.error?.message || "See retained evidence") };
  if (usage) record.cost = cost(usage);
  a.threadId = threadId; a.turns[index] = record; save(s);
  console.log(JSON.stringify({ arm, turn: index, completed: record.completed, seconds: record.seconds, usage, usd: record.cost?.total_usd, tools: record.tools.length, graphCalls: record.tools.filter((i) => i.server === "contextshrink_graph").length, failures: record.failures }));
  assert.ok(record.completed, "Turn failed; no further requests will be made");
}
async function run() {
  const s = state(); assert.ok(s, "Prepare first");
  try {
    for (const arm of ["A", "B"]) fs.copyFileSync(AUTH, path.join(home(arm), ".codex/auth.json"));
    for (let i = 0; i < prompts.length; i++) for (const arm of s.order[i]) await turn(arm, i);
  } finally { cleanupAuth(); }
}
function archiveFailed() {
  const s = state();
  assert.ok(!(s.failedAttempts || []).length, "Only one explicit capacity retry is allowed in this pilot");
  const failures = ["A", "B"].flatMap((arm) => s.arms[arm].turns.filter((t) => !t.completed).map((t) => ({ arm, record: t })));
  assert.equal(failures.length, 1);
  const { arm, record } = failures[0];
  assert.ok(record.failures.some((m) => /at capacity/i.test(m)), "Only the observed capacity interruption can be archived here");
  assert.equal(s.arms[arm].turns.at(-1), record, "Only the final failed turn can be retried");
  const archive = path.join(RUN, "failed-attempts", arm + "-" + record.index);
  fs.mkdirSync(archive, { recursive: true });
  const base = `turn-${String(record.index).padStart(2, "0")}`;
  for (const suffix of [".jsonl", ".stderr", ".answer.md"]) fs.copyFileSync(path.join(RUN, arm, base + suffix), path.join(archive, base + suffix));
  s.failedAttempts = [{ arm, ...record, archive, note: "Capacity interruption, explicitly announced before one retry. Same conversation resumed; partial context remains and can affect later turns." }];
  s.arms[arm].turns.pop();
  save(s);
  console.log(JSON.stringify({ preserved: true, arm, turn: record.index, knownUsd: record.cost.total_usd }));
}
if (require.main === module) {
  const mode = process.argv[2];
  (mode === "prepare" ? prepare() : mode === "run" ? run() : mode === "archive-failed" ? Promise.resolve(archiveFailed()) : mode === "cleanup-auth" ? Promise.resolve(cleanupAuth()) : Promise.reject(Error("Use prepare, run, archive-failed or cleanup-auth"))).catch((e) => { console.error(e.message); process.exitCode = 1; });
}
module.exports = { ROOT, RUN, WORK, repo, home, state, prompts, cost, verifySources, rolloutUsage };
