"use strict";
const fs = require("node:fs"), path = require("node:path"), assert = require("node:assert/strict");
const core = require("../../desktop/npx-codex/lib/graph-core.cjs");
const bench = require("./graphify-benchmark.cjs");
const encoding = require("../../desktop/npx-codex/node_modules/js-tiktoken").getEncoding("o200k_base");
const s = core.json(path.join(bench.RUN, "state.json"));
const manifest = core.json(path.join(bench.RUN, "source-manifest.json"));
assert.equal(s.graphVersion, "0.9.93");
assert.equal(s.installedSkillSha256, s.publishedSkillSha256, "The installed skill was modified");
const ids = new Set(), quality = {}, retrieval = {};
for (const arm of ["A", "B"]) {
  const rows = s.arms[arm];
  assert.equal(rows.length, bench.CASES.length, "Complete all prespecified cases first");
  assert.ok(rows.every((r) => r?.completed && r.usage));
  assert.ok(!fs.existsSync(path.join(bench.home(arm), ".codex/auth.json")), "Temporary sign-in copy still exists");
  for (const entry of manifest) assert.equal(core.hash(fs.readFileSync(path.join(bench.repo(arm), entry.path))), entry.sha256, "Frozen source changed: " + entry.path);
  quality[arm] = { citations: 0, generatedCitations: [], invalidLocations: [], blockedCompletedChats: [], toolFailures: [] };
  retrieval[arm] = [];
  for (const row of rows) {
    assert.ok(!ids.has(row.threadId), "Chat was resumed or reused"); ids.add(row.threadId);
    for (const k of ["input_tokens", "cached_input_tokens", "output_tokens"]) assert.equal(row.cliUsage[k], row.usage[k], "CLI and engine disagree");
    assert.ok(row.usage.cached_input_tokens <= row.usage.input_tokens);
    assert.ok(row.usage.reasoning_output_tokens <= row.usage.output_tokens);
    assert.equal(row.cliUsage.cache_write_input_tokens || 0, 0);
    const base = path.join(bench.RUN, arm, `chat-${String(row.index).padStart(2, "0")}`);
    const answer = fs.readFileSync(base + ".answer.md", "utf8");
    if (/usage limit|blocked by policy|Automatic approval review failed/i.test(fs.readFileSync(base + ".stderr", "utf8"))) quality[arm].blockedCompletedChats.push(row.index);
    for (const m of answer.matchAll(/\]\(([^()\n]+?)#L(\d+)(?:[^()]*)\)/g)) {
      const file = m[1].replace(/\\/g, "/").replace(/^.*?\/repo\//, "").replace(/^\.\//, "");
      if (/^https?:/i.test(file)) continue;
      quality[arm].citations++;
      const entry = manifest.find((e) => e.path.replace(/\\/g, "/") === file);
      if (!entry) {
        if (file.startsWith("graphify-out/") && fs.existsSync(path.join(bench.repo(arm), file))) quality[arm].generatedCitations.push({ index: row.index, file, line: +m[2] });
        else quality[arm].invalidLocations.push({ index: row.index, file, line: +m[2], reason: "Not original source or existing generated evidence" });
        continue;
      }
      if (+m[2] > fs.readFileSync(path.join(bench.repo(arm), entry.path), "utf8").split(/\r?\n/).length) quality[arm].invalidLocations.push({ index: row.index, file, line: +m[2], reason: "Outside source" });
    }
    const events = fs.readFileSync(base + ".jsonl", "utf8").split(/\r?\n/).flatMap((l) => { try { return [JSON.parse(l)]; } catch { return []; } });
    const calls = events.filter((e) => e.type === "item.completed" && e.item?.type === "command_execution").map((e) => e.item);
    const graphifyCalls = [];
    for (const c of calls) {
      // Heuristic invocation extraction; raw commands remain in logs for review.
      const commandText = (c.command || "").replaceAll("\\", "/");
      const matches = [...commandText.matchAll(/(?:-m\s+graphify\s+|(?:^|[\s;|&"'\/])graphify(?:\.exe)?["']?\s+)(query|reflect|save-result|path|explain|update|extract|cluster-only)\b/g)];
      for (const m of matches) graphifyCalls.push({ command: m[1], exitCode: c.exit_code, status: c.status });
      if (c.exit_code || c.status === "failed") quality[arm].toolFailures.push({ index: row.index, exitCode: c.exit_code, command: c.command, status: c.status });
    }
    retrieval[arm].push({ index: row.index, task: row.task, repetition: row.repetition, shellCalls: calls.length, graphifyCalls, toolOutputTokens: calls.reduce((n, c) => n + encoding.encode(c.aggregated_output || "").length, 0), answerWords: row.answerWords });
  }
}
const summary = bench.summary();
function compare(a, b) { return { inputTokensAvoided: a.input_tokens - b.input_tokens, inputReductionPercent: 100 * (a.input_tokens - b.input_tokens) / a.input_tokens, outputTokensAvoided: a.output_tokens - b.output_tokens, dollarsAvoided: a.total_usd - b.total_usd, dollarReductionPercent: 100 * (a.total_usd - b.total_usd) / a.total_usd, noCacheScenarioDollarsAvoided: a.no_cache_price_scenario_usd - b.no_cache_price_scenario_usd }; }
const comparisons = { combined: compare(summary.arms.A, summary.arms.B), byTask: {}, byPair: [] };
for (const [task, arms] of Object.entries(summary.byTask)) comparisons.byTask[task] = compare(arms.A, arms.B);
for (let i = 0; i < bench.CASES.length; i++) comparisons.byPair.push({ index: i, task: bench.CASES[i].id, repetition: bench.CASES[i].repetition, ...compare({ ...s.arms.A[i].usage, ...s.arms.A[i].cost }, { ...s.arms.B[i].usage, ...s.arms.B[i].cost }) });
const graphFile = path.join(bench.repo("B"), "graphify-out/graph.json");
const graphHash = core.hash(fs.readFileSync(graphFile));
const manualFile = path.join(__dirname, "graphify-quality.json");
const manualQuality = fs.existsSync(manualFile) ? core.json(manualFile) : null;
function walk(dir) { if (!fs.existsSync(dir)) return []; return fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) => e.isDirectory() ? walk(path.join(dir, e.name)) : [path.join(dir, e.name)]); }
const generated = walk(path.join(bench.repo("B"), "graphify-out"));
const memory = { generatedFileCount: generated.length, savedMemoryFiles: generated.map((f) => path.relative(bench.repo("B"), f).replace(/\\/g, "/")).filter((f) => /graphify-out\/(?:memory|reflections)\//.test(f)), invocations: retrieval.B.flatMap((r) => r.graphifyCalls).reduce((acc, c) => { acc[c.command] = (acc[c.command] || 0) + 1; return acc; }, {}), structuralGraphSha256Unchanged: graphHash === s.initialGraphSha256 };
assert.ok(memory.invocations.query > 0, "Actual Graphify was not queried");
const retrievalTotals = Object.fromEntries(["A", "B"].map((arm) => [arm, retrieval[arm].reduce((a, r) => ({ shellCalls: a.shellCalls + r.shellCalls, toolOutputTokens: a.toolOutputTokens + r.toolOutputTokens, answerWords: a.answerWords + r.answerWords }), { shellCalls: 0, toolOutputTokens: 0, answerWords: 0 })]));
const result = { ...summary, package: { name: "graphifyy", version: s.graphVersion, mode: s.graphifyMode, extras: s.graphifyExtras }, setupAttempts: s.setupAttempts, totalSetupSeconds: s.totalSetupSeconds, comparisons, quality, retrieval, memory, manualQuality, method: { sourceFiles: manifest.length, manifestDigest: s.manifestDigest, independentChats: ids.size, sourceHashesUnchanged: true, temporaryAuthRemoved: true, graphBuildModelTokens: 0, billedScope: "Observed main-model standard API-price equivalents. Subscription invoices and auxiliary approval-review model usage are not measured.", limits: "One repository, two repetitions per task, automatic cache state. First arm swaps per task in repetition two. Local code-only Graphify with deterministic clustering and no paid semantic labels or Cloud. Citation location checks are not semantic verification. Generated work memory persists only for B." } };
result.retrievalTotals = retrievalTotals;
fs.writeFileSync(path.join(__dirname, "graphify-results.json"), JSON.stringify(result, null, 2) + "\n");
const fmt = (v) => v.toLocaleString("en-US"), usd = (v) => "$" + v.toFixed(4);
const lines = ["# Actual Graphify: fresh-chat comparison", "", "Published Graphify 0.9.93, using its bundled Codex skill/native routing, against ordinary source retrieval. Same frozen repository and gpt-6.1-sol/high; four tasks repeated twice, sixteen fresh conversations. Code-only index, clustering without paid semantic labels, generated query/reflection memory allowed between B chats. This does not test Graphify Cloud or semantic document/media enrichment.", "", "| Task (two chats per arm) | Ordinary input | Graphify input | Ordinary USD | Graphify USD | Cost reduction with Graphify |", "|---|---:|---:|---:|---:|---:|"];
for (const [task, a] of Object.entries(summary.byTask)) lines.push(`| ${task} | ${fmt(a.A.input_tokens)} | ${fmt(a.B.input_tokens)} | ${usd(a.A.total_usd)} | ${usd(a.B.total_usd)} | ${comparisons.byTask[task].dollarReductionPercent.toFixed(2)}% |`);
lines.push(`| **Total** | **${fmt(summary.arms.A.input_tokens)}** | **${fmt(summary.arms.B.input_tokens)}** | **${usd(summary.arms.A.total_usd)}** | **${usd(summary.arms.B.total_usd)}** | **${comparisons.combined.dollarReductionPercent.toFixed(2)}%** |`, "", `Input reduction: ${comparisons.combined.inputReductionPercent.toFixed(2)}%. Dollar reduction: ${comparisons.combined.dollarReductionPercent.toFixed(2)}%. Positive means savings; negative means higher usage/cost. ${comparisons.byPair.filter((p) => p.dollarsAvoided > 0).length} of eight pairs cost less with Graphify.`, "", "Dollar figures use observed cache hits and standard rates of $2/M uncached input, $0.10/M cached input and $10/M output. These are main-model API-price equivalents, not subscription charges. Cached input is already included in input; reasoning is already included in output. Auxiliary automatic-approval usage is unavailable.", "", `Final graph: ${fmt(s.graph.nodes)} nodes, ${fmt(s.graph.edges)} edges. Its final build took ${s.graph.buildSeconds.toFixed(2)} seconds and zero model tokens. An earlier local build revealed the missing optional SQL parser; that parser was installed and the graph rebuilt before any model calls. Total local setup computation across both builds: ${s.totalSetupSeconds.toFixed(2)} seconds, zero model tokens. Structural graph hash unchanged across chats: ${memory.structuralGraphSha256Unchanged}.`, "", `All ${manifest.length} original source hashes remained unchanged in both arms; all ${ids.size} conversation IDs are distinct; temporary sign-in copies were removed. Original-source citation location checks: A ${quality.A.citations} citations / ${quality.A.invalidLocations.length} invalid; B ${quality.B.citations} / ${quality.B.invalidLocations.length} invalid. Graph-only/generated-state citations may need separate interpretation.`, "", `Observed Graphify command counts: ${JSON.stringify(memory.invocations)}. Generated saved-answer and reflection files are listed in graphify-results.json.`, "", manualQuality ? "Manual coverage/claim review is recorded in graphify-quality.json; consult it before interpreting lower cost as equal task completion. This is not an independent blind quality assessment." : "Manual answer coverage/claim review is pending. Cheaper graph-only answers cannot be assumed equivalent to source review.", "", "The package skill, native AGENTS routing and prompts are part of this treatment. The index excludes semantic enrichment; some implementation details may be unavailable from structural evidence. Cache state was not equalized. Two repetitions and one repository provide exploratory results, not a universal savings guarantee. See GRAPHIFY-PROTOCOL.md for the fixed design and graphify-results.json for per-pair data, failures and limitations.", "");
if (summary.failedAttempts.length) lines.push(`Interrupted attempts retained separately: ${summary.failedAttempts.length}; known additional main-model equivalent ${usd(summary.interruptedAttemptKnownUsd)}. Total including interruptions: ${usd(summary.arms.A.total_usd + summary.arms.B.total_usd + summary.interruptedAttemptKnownUsd)}.`, "");
lines.push("## Interpretation", "", "This workflow did not save whole-chat tokens or dollars on this repository. Graphify cost more in seven of eight pairs; its one cheaper request-flow answer also omitted authentication conditions covered by its baseline. Both Graphify architecture answers omitted desktop integration, and the second largely omitted the separate control-plane architecture. Other answers broadly met the limited coverage rubric. This does not establish equal answer quality.", "", `One-pass tool-output tokens fell from ${fmt(retrievalTotals.A.toolOutputTokens)} to ${fmt(retrievalTotals.B.toolOutputTokens)} (${(100 * (1 - retrievalTotals.B.toolOutputTokens / retrievalTotals.A.toolOutputTokens)).toFixed(2)}% less), while shell calls rose from ${retrievalTotals.A.shellCalls} to ${retrievalTotals.B.shellCalls}. These output counts are locally tokenized retrieval diagnostics, not provider usage. Total model input includes instructions and conversation context on successive model calls, so smaller retrieval alone did not overcome the complete workflow's overhead. This experiment cannot isolate each overhead source without an ablation.`, "", `Cached input was ${fmt(summary.arms.A.cached_input_tokens)} for A and ${fmt(summary.arms.B.cached_input_tokens)} for B; uncached input was ${fmt(summary.arms.A.uncached_input_tokens)} versus ${fmt(summary.arms.B.uncached_input_tokens)}, and output was ${fmt(summary.arms.A.output_tokens)} versus ${fmt(summary.arms.B.output_tokens)}. Observed caching is already included in the dollar comparison. Reusing a local graph does not remove subsequent query, verification, or conversation costs.`, "", "Six saved-answer files and a reflection file were retained between Graphify chats. Their existence proves persistence, not a causal savings benefit. The structural graph was unchanged. Native Codex memory databases had zero stage-one outputs and zero jobs in both arms at completion; initialization rows alone do not indicate generated-memory reuse.", "", "The summary's legacy graphCalls field counts MCP-tool events; this native Graphify skill uses shell commands. Actual Graphify use is recorded under memory.invocations and per-chat retrieval.graphifyCalls. Nonzero command exits are retained in quality.toolFailures; some are normal unmatched searches, not failed conversations. No conversation was interrupted or retried.", "", "Practical implication: retain direct source retrieval for known files/symbols, and test selective compact graph discovery or impact analysis before enabling broader graph-first routing. Estimated savings should not treat reduced retrieval output as demonstrated whole-session savings. This benchmark changed no production code or ledger.", "");
fs.writeFileSync(path.join(__dirname, "GRAPHIFY-RESULTS.md"), lines.join("\n"));
console.log(JSON.stringify({ arms: summary.arms, comparisons, memory: memory.invocations, invalidCitations: { A: quality.A.invalidLocations, B: quality.B.invalidLocations } }, null, 2));
