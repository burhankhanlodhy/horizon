"use strict";
// Paired autonomous fresh chats, not scripted retrieval. Same frozen corpus as
// the prior 21-turn experiment; the graph is built once and reused across chats.
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const assert = require("node:assert/strict");
const { spawn, spawnSync } = require("node:child_process");
const core = require("../../desktop/npx-codex/lib/graph-core.cjs");
const integration = require("../../desktop/npx-codex/lib/graph-config.cjs");
const ROOT = path.resolve(__dirname, "../..");
const PRIOR = path.join(ROOT, "desktop/dist-windows/npx-test/live-graph-21-turns");
const RUN = path.join(ROOT, "desktop/dist-windows/npx-test/fresh-graph-chats");
const WORK = path.join(os.tmpdir(), "cs-fresh-" + core.hash(ROOT).slice(0, 8));
const PRIOR_WORK = path.join(os.tmpdir(), "cs-g21-" + core.hash(ROOT).slice(0, 8));
const CODEX = process.env.CODEX_EXE || "codex"; // path to codex.exe (set CODEX_EXE)
const ENGINE = path.join(ROOT, "desktop/dist-windows/npx-test/graphify-review/cbm-bin/codebase-memory-mcp.exe");
const AUTH = process.env.CODEX_AUTH || require("node:path").join(require("node:os").homedir(), ".codex", "auth.json");
const MODEL = "gpt-6.1-sol", EFFORT = "high";
const PRICES = { input: 2, cachedInput: 0.1, output: 10, units: "USD per million tokens", source: "https://developers.openai.com/api/docs/pricing", checked: "2026-10-21" };
const TASKS = [
  { id: "architecture", prompt: "Review the codebase in this folder. Explain architecture, key entry points, data flow and main risks, with source citations. Treat this as an initial architecture review, not an exhaustive audit of every file. Keep your answer within 400 words." },
  { id: "request-flow", prompt: "Trace a public OpenAI Responses inference request from its HTTP entry point through authentication, routing, input transformation, provider forwarding and usage reporting. Identify the key functions and one failure-handling boundary. Use source citations and stay within 300 words." },
  { id: "savings-accounting", prompt: "Explain how this codebase calculates and reports compression savings and graph estimated savings. Trace the main components and explain how duplicate graph receipts and inference counts are handled. Distinguish observed usage from estimates. Use source citations and stay within 300 words." },
  { id: "focused-control", prompt: "In desktop/npx-codex/lib/graph-policy.cjs, find the guidance controlling when graph_search intent=discovery is appropriate and when direct source search is preferred. Explain the policy and its limitation, with line citations. Keep your answer within 180 words." },
];
const CASES = [0, 1].flatMap((repetition) => TASKS.map((task) => ({ ...task, repetition })));
const COMMON = "This is a read-only review of a frozen source snapshot. Do not edit files, install dependencies, access the network, inspect credentials/parent folders, call external APIs/models, or delegate/spawn agents. Use only local source and available retrieval tools. Prefer bounded reads and avoid dumping whole directories or huge files. This is a fresh chat: no earlier conversation is supplied. Cite current relative source paths and line numbers using Markdown links ending #L<number>. State uncertainty and missing coverage honestly. Read only evidence needed to answer the question.";
function atomic(file, value) { core.atomic(file, JSON.stringify(value, null, 2) + "\n"); }
function state() { return core.json(path.join(RUN, "state.json")); }
function save(s) { atomic(path.join(RUN, "state.json"), s); }
function repo(arm) { return path.join(WORK, arm, "repo"); }
function home(arm) { return path.join(WORK, arm, "home"); }
function env(arm) {
  const allowed = ["PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "TEMP", "TMP", "LANG", "LC_ALL"];
  const clean = Object.fromEntries(Object.entries(process.env).filter(([key]) => allowed.includes(key.toUpperCase())));
  return { ...clean, HOME: home(arm), USERPROFILE: home(arm), CODEX_HOME: path.join(home(arm), ".codex"), APPDATA: path.join(home(arm), "roaming"), LOCALAPPDATA: path.join(home(arm), "local"), NO_COLOR: "1" };
}
function cleanupAuth() {
  for (const arm of ["A", "B"]) {
    const file = path.join(home(arm), ".codex", "auth.json");
    if (fs.existsSync(file)) fs.unlinkSync(file);
  }
}
function archiveFailed() {
  const s = state();
  s.failedAttempts ||= [];
  for (const arm of ["A", "B"]) for (let index = 0; index < s.arms[arm].length; index++) {
    const record = s.arms[arm][index];
    if (!record || record.completed) continue;
    const attempt = s.failedAttempts.filter((r) => r.arm === arm && r.index === index).length + 1;
    const base = path.join(RUN, arm, `chat-${String(index).padStart(2, "0")}`);
    const archived = base + `.failed-attempt-${attempt}`;
    for (const ext of [".jsonl", ".stderr", ".answer.md"]) if (fs.existsSync(base + ext)) fs.renameSync(base + ext, archived + ext);
    s.failedAttempts.push({ arm, ...record, archivedEvidence: path.relative(RUN, archived) });
    s.arms[arm][index] = null;
  }
  save(s);
  console.log(JSON.stringify({ archivedFailedAttempts: s.failedAttempts.length, retainedKnownUsd: s.failedAttempts.reduce((n, r) => n + (r.cost?.total_usd || 0), 0) }));
}
function enableAuth() {
  for (const arm of ["A", "B"]) fs.copyFileSync(AUTH, path.join(home(arm), ".codex", "auth.json"));
}
async function prepare() {
  if (state()) throw Error("Run already exists; resume rather than overwrite evidence.");
  const manifest = core.json(path.join(PRIOR, "source-manifest.json"));
  const previous = core.json(path.join(PRIOR, "state.json"));
  assert.ok(manifest?.length && previous);
  fs.mkdirSync(RUN, { recursive: true });
  for (const arm of ["A", "B"]) {
    fs.mkdirSync(path.join(RUN, arm), { recursive: true });
    for (const file of manifest) {
      const source = path.join(PRIOR_WORK, "A/repo", file.path);
      assert.equal(core.hash(fs.readFileSync(source)), file.sha256, "Prior frozen source changed");
      const dest = path.join(repo(arm), file.path);
      fs.mkdirSync(path.dirname(dest), { recursive: true });
      fs.copyFileSync(source, dest);
    }
    assert.equal(spawnSync("git", ["init", "-q", repo(arm)], { windowsHide: true }).status, 0);
    const h = path.join(home(arm), ".codex");
    fs.mkdirSync(h, { recursive: true, mode: 0o700 });
    fs.writeFileSync(path.join(h, "config.toml"), `model = ${JSON.stringify(MODEL)}\nmodel_provider = "openai"\nmodel_reasoning_effort = ${JSON.stringify(EFFORT)}\nweb_search = "disabled"\n[features]\nmulti_agent = false\n[projects.${JSON.stringify(repo(arm))}]\ntrust_level = "trusted"\n`);
  }
  const project = core.project(repo("B"), env("B"));
  const binary = core.engineLocation(project);
  fs.mkdirSync(path.dirname(binary), { recursive: true });
  if (!fs.existsSync(binary)) fs.copyFileSync(ENGINE, binary);
  atomic(binary + ".json", { version: "v0.8.1", sha256: core.hash(fs.readFileSync(ENGINE)) });
  integration.install(repo("B"), env("B"));
  const start = Date.now();
  const graph = await core.refresh(repo("B"), env("B"), { binary });
  const s = { startedAt: new Date().toISOString(), model: MODEL, reasoningEffort: EFFORT, prices: PRICES, sourceFiles: manifest.length, sourceBytes: manifest.reduce((n, f) => n + f.bytes, 0), sourceFingerprint: previous.sourceFingerprint, manifestDigest: core.hash(JSON.stringify(manifest)), graphVersion: require("../../desktop/npx-codex/package.json").version, graph: { nodes: graph.nodes, edges: graph.edges, indexedFiles: graph.indexedFiles, buildSeconds: (Date.now() - start) / 1000, modelTokens: 0 }, cases: CASES, commonInstructions: COMMON, permissionMode: "--approve-for-me", arms: { A: [], B: [] } };
  atomic(path.join(RUN, "source-manifest.json"), manifest);
  save(s);
  console.log(JSON.stringify({ prepared: true, sourceFiles: s.sourceFiles, graph: s.graph, pairs: CASES.length }));
}
function findFiles(dir) {
  if (!fs.existsSync(dir)) return [];
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) => e.isDirectory() ? findFiles(path.join(dir, e.name)) : e.name.endsWith(".jsonl") ? [path.join(dir, e.name)] : []);
}
function rolloutUsage(arm, id) {
  let latest = null;
  for (const file of findFiles(path.join(home(arm), ".codex/sessions")).filter((f) => f.includes(id))) {
    for (const line of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
      let e; try { e = JSON.parse(line); } catch { continue; }
      if (e.type === "event_msg" && e.payload?.type === "token_count" && e.payload.info?.total_token_usage) latest = e.payload.info.total_token_usage;
    }
  }
  return latest;
}
function costs(u) {
  const uncached = u.input_tokens - u.cached_input_tokens;
  return { uncached_input_tokens: uncached, input_usd: (uncached * PRICES.input + u.cached_input_tokens * PRICES.cachedInput) / 1e6, output_usd: u.output_tokens * PRICES.output / 1e6, total_usd: (uncached * PRICES.input + u.cached_input_tokens * PRICES.cachedInput + u.output_tokens * PRICES.output) / 1e6, no_cache_price_scenario_usd: (u.input_tokens * PRICES.input + u.output_tokens * PRICES.output) / 1e6 };
}
async function turn(arm, index) {
  const s = state();
  if (s.arms[arm][index]?.completed) return;
  if (s.arms[arm][index]) throw Error("Failed attempt preserved. Inspect before explicitly scheduling another attempt.");
  const base = path.join(RUN, arm, `chat-${String(index).padStart(2, "0")}`);
  const out = fs.createWriteStream(base + ".jsonl"), err = fs.createWriteStream(base + ".stderr");
  const events = []; let buffer = "", timedOut = false;
  const start = Date.now();
  // Never resume: every case gets an independent conversation and full setup cost.
  const child = spawn(CODEX, ["-C", repo(arm), "--approve-for-me", "exec", "--json", "-"], { cwd: repo(arm), env: env(arm), windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
  const timer = setTimeout(() => { timedOut = true; spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { windowsHide: true }); }, 600000);
  child.stdout.on("data", (data) => { out.write(data); buffer += data.toString(); let end; while ((end = buffer.indexOf("\n")) >= 0) { const line = buffer.slice(0, end); buffer = buffer.slice(end + 1); try { events.push(JSON.parse(line)); } catch {} } });
  child.stderr.on("data", (data) => err.write(data));
  child.stdin.end(COMMON + "\n\n" + CASES[index].prompt);
  const exitCode = await new Promise((resolve, reject) => { child.once("error", reject); child.once("close", resolve); });
  clearTimeout(timer);
  await Promise.all([new Promise((r) => out.end(r)), new Promise((r) => err.end(r))]);
  const id = events.find((e) => e.type === "thread.started")?.thread_id;
  const complete = events.findLast((e) => e.type === "turn.completed");
  const usage = id ? rolloutUsage(arm, id) : null;
  const items = events.filter((e) => e.type === "item.completed").map((e) => e.item);
  const answer = items.filter((i) => i.type === "agent_message").map((i) => i.text).join("\n\n");
  fs.writeFileSync(base + ".answer.md", answer);
  const record = { index, task: CASES[index].id, repetition: CASES[index].repetition, threadId: id, completed: !!complete && exitCode === 0, exitCode, timedOut, seconds: (Date.now() - start) / 1000, usage: usage || complete?.usage || null, cliUsage: complete?.usage || null, toolCalls: items.filter((i) => ["command_execution", "mcp_tool_call"].includes(i.type)).map((i) => ({ type: i.type, tool: i.tool || null, server: i.server || null, exit_code: i.exit_code ?? null, status: i.status })), answerWords: answer.split(/\s+/).filter(Boolean).length, failure: events.filter((e) => ["turn.failed", "error"].includes(e.type)).map((e) => JSON.stringify(e).replace(/Bearer\s+\S+|sk-[a-zA-Z0-9_-]+/g, "[redacted]")) };
  if (record.usage) record.cost = costs(record.usage);
  s.arms[arm][index] = record;
  save(s);
  console.log(JSON.stringify({ arm, index, task: record.task, completed: record.completed, seconds: record.seconds, input: record.usage?.input_tokens, cached: record.usage?.cached_input_tokens, output: record.usage?.output_tokens, usd: record.cost?.total_usd, tools: record.toolCalls.length, graphCalls: record.toolCalls.filter((t) => t.server === "contextshrink_graph").length, failures: record.failure }));
  if (!record.completed) throw Error(`Fresh chat ${arm}/${index} failed. Stopping; no outcome discarded.`);
}
function aggregate(rows) {
  const usage = { input_tokens: 0, cached_input_tokens: 0, output_tokens: 0, reasoning_output_tokens: 0 };
  for (const r of rows) for (const key of Object.keys(usage)) usage[key] += r.usage?.[key] || 0;
  return { chats: rows.length, ...usage, ...costs(usage), seconds: rows.reduce((n, r) => n + r.seconds, 0), graphCalls: rows.flatMap((r) => r.toolCalls).filter((t) => t.server === "contextshrink_graph").length };
}
function summary() {
  const s = state();
  const result = { model: MODEL, reasoningEffort: EFFORT, prices: PRICES, sourceFiles: s.sourceFiles, graph: s.graph, arms: {}, byTask: {}, failedAttempts: s.failedAttempts || [], interruptedAttemptKnownUsd: (s.failedAttempts || []).reduce((n, r) => n + (r.cost?.total_usd || 0), 0) };
  for (const arm of ["A", "B"]) result.arms[arm] = aggregate(s.arms[arm].filter(Boolean));
  for (const task of TASKS) { result.byTask[task.id] = {}; for (const arm of ["A", "B"]) result.byTask[task.id][arm] = aggregate(s.arms[arm].filter((r) => r?.task === task.id)); }
  atomic(path.join(RUN, "summary.json"), result);
  return result;
}
async function run() {
  assert.ok(state(), "Prepare frozen source first");
  enableAuth();
  try {
    const arg = process.argv.find((a) => a.startsWith("--through="));
    const through = arg ? Number(arg.split("=")[1]) : CASES.length - 1;
    assert.ok(Number.isInteger(through) && through >= 0 && through < CASES.length);
    for (let i = 0; i <= through; i++) {
      for (const arm of i % 2 ? ["B", "A"] : ["A", "B"]) await turn(arm, i);
      summary();
    }
  } finally { cleanupAuth(); }
}
if (require.main === module) {
  const mode = process.argv[2];
  (mode === "prepare" ? prepare() : mode === "run" ? run() : mode === "archive-failed" ? Promise.resolve(archiveFailed()) : mode === "summary" ? Promise.resolve(console.log(JSON.stringify(summary(), null, 2))) : mode === "cleanup-auth" ? Promise.resolve(cleanupAuth()) : Promise.reject(Error("Use prepare, run [--through=N], archive-failed, summary or cleanup-auth"))).catch((e) => { console.error(e.message); process.exitCode = 1; });
}
module.exports = { ROOT, RUN, WORK, CASES, MODEL, EFFORT, PRICES, costs, summary, repo, home };
