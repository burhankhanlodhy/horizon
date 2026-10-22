"use strict";
// Live, sequential model evaluation in isolated homes and frozen source copies.
// No production proxy/ledger, personal config, or existing chat is modified.
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { spawn, spawnSync } = require("node:child_process");
const core = require("../../desktop/npx-codex/lib/graph-core.cjs");
const integration = require("../../desktop/npx-codex/lib/graph-config.cjs");
const ROOT = path.resolve(__dirname, "../..");
const RUN = path.join(ROOT, "desktop/dist-windows/npx-test/live-graph-21-turns");
// Short native paths avoid Windows engine path-length limits in nested worktrees.
const WORK = path.join(os.tmpdir(), "cs-g21-" + core.hash(ROOT).slice(0, 8));
const CODEX = process.env.CODEX_EXE || "codex"; // path to codex.exe (set CODEX_EXE)
const ENGINE = path.join(ROOT, "desktop/dist-windows/npx-test/graphify-review/cbm-bin/codebase-memory-mcp.exe");
const AUTH = process.env.CODEX_AUTH || require("node:path").join(require("node:os").homedir(), ".codex", "auth.json");
const MODEL = "gpt-6.1-sol", EFFORT = "high";
const PRICES = { input: 2, cachedInput: 0.1, output: 10, units: "USD per million tokens", source: "https://developers.openai.com/api/docs/pricing", checked: "2026-10-21" };
const prompts = [
  "Review the codebase in this folder. Explain the architecture, key entry points, data flow and main risks, with source citations. Treat this as an initial architecture review, not an exhaustive audit of every file. Keep the answer within 400 words.",
  "Trace a public inference request from the HTTP entry point to the provider. Where are routing and security decisions made? Cite the key functions.",
  "Explain the OpenAI Responses transformation pipeline and its ordering. Which stages can change the input and which preserve provider semantics?",
  "Review Flash Observations eligibility, price guards and failure handling. Identify a concrete risk worth testing, with source evidence.",
  "How do conversation keys, horizon state and replay handling prevent incorrect or duplicate Flash compression? Identify any limitation you can substantiate.",
  "Where do actual provider usage, compression token savings and estimated dollar savings come from? Explain the distinctions in this codebase.",
  "Review model pricing, aliases, unknown models and cached input rates. Show the calculation path and a relevant test.",
  "Trace account/API-key authentication between the API and proxy. What data is trusted at each boundary? Do not inspect credentials or secret files.",
  "Review analytics outbox durability and retry/deduplication behavior. What happens across process restarts?",
  "Review graph estimate ledger integration. Can its events inflate inference counts or compression percentages, and how are repeated receipts handled?",
  "Review the local code graph's indexing filters and engine installation. What source is deliberately excluded, and how is the engine verified?",
  "How does the graph handle dirty files, stale indexes, deletion and concurrent queries? Cite code and tests supporting your answer.",
  "Review graph project integration ownership and runtime upgrades. Can enabling/disabling it remove unrelated user settings?",
  "Review the Codex connector's normal connect/disconnect path, including configuration ownership and authentication separation. Cite rollback behavior.",
  "Review existing-chat migration and undo. Explain how conversation history is preserved and what happens after an interrupted migration.",
  "Review the savings companion lifecycle and numeric display. Where does it get totals, and what happens when retrieval fails?",
  "Locate the Claude Desktop helper/direct launcher flow. Review its patch/restore safety and how it interacts with the installed ContextShrink app.",
  "Review the website route for signed Claude tools and package checksum verification. Explain the trust and release dependencies.",
  "Choose five important existing tests across routing, compression, graph and connector behavior. Explain the invariants they actually verify.",
  "From the code reviewed, identify three practical performance or token-efficiency improvements. Separate measured facts from hypotheses and discuss correctness risks.",
  "Summarize the review into five prioritized findings, each with a source citation, evidence and confidence. Reuse earlier findings; reread only if evidence is missing. Do not claim a bug without supporting source evidence.",
];
const common = "This is a read-only review of a frozen source snapshot. Do not edit files, install dependencies, access the network, inspect credentials/parent folders, call external APIs/models, or delegate/spawn agents. Use only local source and available retrieval tools. Prefer bounded reads and avoid dumping whole directories or huge files. Reuse evidence already in this conversation. Cite current relative paths and line numbers. Except for the initial review, keep each answer within 220 words. State uncertainty and missing coverage honestly.";

function atomic(file, value) { core.atomic(file, JSON.stringify(value, null, 2) + "\n"); }
function state() { return core.json(path.join(RUN, "state.json")); }
function save(value) { atomic(path.join(RUN, "state.json"), value); }
function home(arm) { return path.join(WORK, arm, "home"); }
function repo(arm) { return path.join(WORK, arm, "repo"); }
function env(arm) {
  const allowed = ["PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "TEMP", "TMP", "LANG", "LC_ALL"];
  const clean = Object.fromEntries(Object.entries(process.env).filter(([key]) => allowed.includes(key.toUpperCase())));
  return { ...clean, HOME: home(arm), USERPROFILE: home(arm), CODEX_HOME: path.join(home(arm), ".codex"), APPDATA: path.join(home(arm), "roaming"), LOCALAPPDATA: path.join(home(arm), "local"), NO_COLOR: "1" };
}
async function prepare() {
  if (state()) throw Error("A run already exists; resume it rather than overwriting evidence.");
  fs.mkdirSync(RUN, { recursive: true });
  for (const arm of ["A", "B"]) {
    fs.mkdirSync(path.join(RUN, arm), { recursive: true });
    const oldAuth = path.join(RUN, arm, "home/.codex/auth.json");
    if (fs.existsSync(oldAuth)) fs.unlinkSync(oldAuth);
  }
  const source = core.project(ROOT), snapshot = core.snapshot(source);
  const manifest = snapshot.files.map(({ path: file }) => ({ path: file, sha256: core.hash(fs.readFileSync(path.join(ROOT, file))), bytes: fs.statSync(path.join(ROOT, file)).size }));
  for (const arm of ["A", "B"]) {
    fs.mkdirSync(repo(arm), { recursive: true });
    for (const file of manifest) {
      const dest = path.join(repo(arm), file.path);
      fs.mkdirSync(path.dirname(dest), { recursive: true });
      fs.copyFileSync(path.join(ROOT, file.path), dest);
      if (core.hash(fs.readFileSync(dest)) !== file.sha256) throw Error("Source changed while copying; no live requests were made.");
    }
    const git = spawnSync("git", ["init", "-q", repo(arm)], { windowsHide: true });
    if (git.status !== 0) throw Error("Could not initialize isolated source snapshot.");
    const codexHome = path.join(home(arm), ".codex");
    fs.mkdirSync(codexHome, { recursive: true, mode: 0o700 });
    // The temporary authentication copy is never put in a report or source tree.
    fs.copyFileSync(AUTH, path.join(codexHome, "auth.json"));
    const config = `model = ${JSON.stringify(MODEL)}\nmodel_provider = "openai"\nmodel_reasoning_effort = ${JSON.stringify(EFFORT)}\nweb_search = "disabled"\n[features]\nmulti_agent = false\n[projects.${JSON.stringify(repo(arm))}]\ntrust_level = "trusted"\n`;
    fs.writeFileSync(path.join(codexHome, "config.toml"), config, { mode: 0o600 });
  }
  const graphProject = core.project(repo("B"), env("B"));
  const engine = core.engineLocation(graphProject);
  fs.mkdirSync(path.dirname(engine), { recursive: true });
  if (!fs.existsSync(engine)) {
    try { fs.linkSync(ENGINE, engine); } catch { fs.copyFileSync(ENGINE, engine); }
  }
  atomic(engine + ".json", { version: "v0.8.1", sha256: core.hash(fs.readFileSync(ENGINE)) });
  integration.install(repo("B"), env("B"));
  const graph = await core.refresh(repo("B"), env("B"), { binary: engine });
  const initial = { startedAt: new Date().toISOString(), model: MODEL, reasoningEffort: EFFORT, sourceRoot: ROOT, sourceHead: snapshot.head, sourceFingerprint: snapshot.fingerprint, sourceFiles: manifest.length, sourceBytes: manifest.reduce((n, f) => n + f.bytes, 0), manifestDigest: core.hash(JSON.stringify(manifest)), graphVersion: require("../../desktop/npx-codex/package.json").version, graph: { nodes: graph.nodes, edges: graph.edges, indexedFiles: graph.indexedFiles }, prices: PRICES, prompts, commonInstructions: common, arms: { A: { threadId: null, turns: [] }, B: { threadId: null, turns: [] } } };
  atomic(path.join(RUN, "source-manifest.json"), manifest);
  save(initial);
  console.log(JSON.stringify({ prepared: true, sourceFiles: manifest.length, sourceBytes: initial.sourceBytes, graph: initial.graph, model: MODEL, effort: EFFORT }));
}
function findFiles(dir, predicate) {
  if (!fs.existsSync(dir)) return [];
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => entry.isDirectory() ? findFiles(path.join(dir, entry.name), predicate) : predicate(entry.name) ? [path.join(dir, entry.name)] : []);
}
function rolloutUsage(arm, id) {
  const sessions = path.join(home(arm), ".codex", "sessions");
  const files = findFiles(sessions, (name) => name.endsWith(".jsonl") && name.includes(id));
  let latest = null;
  for (const file of files) for (const line of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
    let event; try { event = JSON.parse(line); } catch { continue; }
    if (event.type === "event_msg" && event.payload?.type === "token_count" && event.payload.info?.total_token_usage) latest = event.payload.info.total_token_usage;
  }
  return latest;
}
function usageDelta(total, before = {}) {
  return Object.fromEntries(["input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"].map((key) => [key, (total?.[key] || 0) - (before?.[key] || 0)]));
}
function costs(u) {
  const uncached = u.input_tokens - u.cached_input_tokens;
  return { uncached_input_tokens: uncached, input_usd: (uncached * PRICES.input + u.cached_input_tokens * PRICES.cachedInput) / 1e6, output_usd: u.output_tokens * PRICES.output / 1e6, total_usd: (uncached * PRICES.input + u.cached_input_tokens * PRICES.cachedInput + u.output_tokens * PRICES.output) / 1e6, no_cache_list_price_usd: (u.input_tokens * PRICES.input + u.output_tokens * PRICES.output) / 1e6 };
}
async function turn(arm, index) {
  const s = state(), a = s.arms[arm];
  if (a.turns[index]?.completed) return;
  const before = a.threadId ? rolloutUsage(arm, a.threadId) : null;
  const prompt = `${index === 0 ? common + "\n\n" : ""}${prompts[index]}`;
  // Route tool permission requests through review, rather than rejecting all
  // retrieval with approvalPolicy=never. Only temporary source copies are writable.
  const args = ["-C", repo(arm), "--approve-for-me", "exec", "--json"];
  if (a.threadId) args.push("resume", a.threadId);
  args.push("-");
  const log = path.join(RUN, arm, `turn-${String(index).padStart(2, "0")}`);
  const out = fs.createWriteStream(log + ".jsonl"), err = fs.createWriteStream(log + ".stderr");
  const events = [], started = Date.now();
  let buffer = "", timeout = false;
  const child = spawn(CODEX, args, { cwd: repo(arm), env: env(arm), windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
  const timer = setTimeout(() => { timeout = true; spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { windowsHide: true }); }, 600000);
  child.stdout.on("data", (data) => {
    out.write(data); buffer += data.toString();
    let end; while ((end = buffer.indexOf("\n")) >= 0) { const line = buffer.slice(0, end); buffer = buffer.slice(end + 1); try { events.push(JSON.parse(line)); } catch { /* raw log retained */ } }
  });
  child.stderr.on("data", (data) => err.write(data));
  child.stdin.end(prompt);
  const code = await new Promise((resolve, reject) => { child.once("error", reject); child.once("close", resolve); });
  clearTimeout(timer); await Promise.all([new Promise((r) => out.end(r)), new Promise((r) => err.end(r))]);
  const thread = events.find((e) => e.type === "thread.started")?.thread_id || a.threadId;
  const completed = events.findLast((e) => e.type === "turn.completed");
  const cumulative = thread ? rolloutUsage(arm, thread) : null;
  const measured = cumulative ? usageDelta(cumulative, before) : completed?.usage;
  const items = events.filter((e) => e.type === "item.completed").map((e) => e.item);
  const answer = items.filter((item) => item.type === "agent_message").map((item) => item.text).join("\n\n");
  fs.writeFileSync(log + ".answer.md", answer);
  const record = { index, prompt: prompts[index], completed: !!completed && code === 0, exitCode: code, timeout, seconds: (Date.now() - started) / 1000, cliUsage: completed?.usage || null, cumulativeUsage: cumulative, usage: measured, usageBasis: cumulative ? "difference of engine rollout cumulative provider usage across this turn" : "CLI turn.completed usage", toolCalls: items.filter((item) => ["command_execution", "mcp_tool_call"].includes(item.type)).map((item) => ({ type: item.type, tool: item.tool || null, server: item.server || null, exit_code: item.exit_code ?? null, status: item.status })), answerWords: answer.split(/\s+/).filter(Boolean).length, failure: events.filter((e) => e.type === "turn.failed" || e.type === "error").map((e) => JSON.stringify(e).replace(/Bearer\s+\S+|sk-[a-zA-Z0-9_-]+/g, "[redacted]")) };
  if (measured) record.cost = costs(measured);
  a.threadId = thread; a.turns[index] = record; save(s);
  console.log(JSON.stringify({ arm, step: index, completed: record.completed, seconds: record.seconds, usage: measured, cost: record.cost?.total_usd, tools: record.toolCalls.length, graphCalls: record.toolCalls.filter((x) => x.server === "contextshrink_graph").length, failure: record.failure }));
  if (!record.completed) throw Error(`Arm ${arm} step ${index} failed; see isolated logs. No further requests will be made.`);
}
function summarize() {
  const s = state();
  const result = { model: MODEL, effort: EFFORT, prices: PRICES, sourceFiles: s.sourceFiles, sourceBytes: s.sourceBytes, sourceFingerprint: s.sourceFingerprint, graphVersion: s.graphVersion, scope: "live Codex sessions: initial review plus 20 follow-ups; same prompts and frozen source, one run per arm, no production proxy", costCaveat: "API list-price equivalent from actual engine usage; ChatGPT subscription does not imply a per-token invoice. Cached tokens are a subset of input. Reasoning tokens are a subset of output, not added twice.", arms: {} };
  for (const arm of ["A", "B"]) {
    const rows = s.arms[arm].turns.filter((t) => t?.completed);
    function aggregate(turns) { const u = usageDelta({}); for (const row of turns) for (const key of Object.keys(u)) u[key] += row.usage[key] || 0; return { turns: turns.length, ...u, ...costs(u) }; }
    result.arms[arm] = { initialReview: aggregate(rows.filter((t) => t.index === 0)), next20: aggregate(rows.filter((t) => t.index > 0)), combined: aggregate(rows), graphToolCalls: rows.flatMap((r) => r.toolCalls).filter((t) => t.server === "contextshrink_graph").length, failedShellCalls: rows.flatMap((r) => r.toolCalls).filter((t) => t.type === "command_execution" && t.exit_code).length };
  }
  atomic(path.join(RUN, "summary.json"), result);
  console.log(JSON.stringify(result, null, 2));
}
function cleanupAuth() {
  for (const arm of ["A", "B"]) {
    const file = path.join(home(arm), ".codex", "auth.json");
    if (fs.existsSync(file)) fs.unlinkSync(file);
  }
}
async function run() {
  if (!state()) throw Error("Prepare the source snapshots first.");
  const throughArg = process.argv.find((arg) => arg.startsWith("--through="));
  const through = throughArg ? Number(throughArg.split("=")[1]) : 20;
  for (let index = 0; index <= through; index++) {
    // Alternate which arm goes first to reduce a consistent cache/order bias.
    for (const arm of index % 2 ? ["B", "A"] : ["A", "B"]) await turn(arm, index);
  }
  summarize();
  if (through === 20) cleanupAuth();
}
const mode = process.argv[2];
(mode === "prepare" ? prepare() : mode === "run" ? run() : mode === "summarize" ? Promise.resolve(summarize()) : mode === "cleanup-auth" ? Promise.resolve(cleanupAuth()) : Promise.reject(Error("Use prepare, run [--through=0], summarize, or cleanup-auth."))).catch((error) => { console.error(error.message); process.exitCode = 1; });
