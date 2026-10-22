"use strict";
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const core = require("../../desktop/npx-codex/lib/graph-core.cjs");
const bench = require("./fresh-chat-benchmark.cjs");
const { getEncoding } = require("../../desktop/npx-codex/node_modules/js-tiktoken");
const encoding = getEncoding("o200k_base");
const s = core.json(path.join(bench.RUN, "state.json"));
assert.ok(s, "No fresh-chat experiment state");
const manifest = core.json(path.join(bench.RUN, "source-manifest.json"));
const quality = {}, evidence = {};
const threads = new Set();
const graphIndexTimes = new Set();
for (const arm of ["A", "B"]) {
  assert.equal(s.arms[arm].length, bench.CASES.length, "Complete every prespecified case before reporting");
  assert.ok(s.arms[arm].every((r) => r.completed && r.usage));
  assert.ok(!fs.existsSync(path.join(bench.home(arm), ".codex/auth.json")), "Remove temporary sign-in copies first");
  const changed = manifest.filter((entry) => core.hash(fs.readFileSync(path.join(bench.repo(arm), entry.path))) !== entry.sha256);
  assert.equal(changed.length, 0, "Frozen source was changed");
  quality[arm] = { sourceUnchanged: true, citations: 0, invalidCitations: [], citedFiles: [], permissionOrQuotaBlocked: [], failedTools: [] };
  const cited = new Set();
  evidence[arm] = [];
  for (const r of s.arms[arm]) {
    assert.ok(!threads.has(r.threadId), "A chat was resumed or repeated");
    threads.add(r.threadId);
    assert.ok(r.usage.cached_input_tokens <= r.usage.input_tokens);
    assert.ok(r.usage.reasoning_output_tokens <= r.usage.output_tokens);
    assert.equal(r.cliUsage.cache_write_input_tokens || 0, 0, "Explicit cache writes require separate pricing");
    for (const key of ["input_tokens", "cached_input_tokens", "output_tokens"]) assert.equal(r.cliUsage[key], r.usage[key], "Fresh CLI and rollout usage disagree");
    const base = path.join(bench.RUN, arm, `chat-${String(r.index).padStart(2, "0")}`);
    const answer = fs.readFileSync(base + ".answer.md", "utf8");
    const stderr = fs.readFileSync(base + ".stderr", "utf8");
    if (/usage limit|blocked by policy|Automatic approval review failed/i.test(stderr)) quality[arm].permissionOrQuotaBlocked.push(r.index);
    for (const m of answer.matchAll(/\]\(([^()\n]+?)#L(\d+)(?:[^()]*)\)/g)) {
      let file = m[1].replace(/\\/g, "/").replace(/^.*?\/repo\//, "").replace(/^\.\//, "");
      if (/^https?:/i.test(file)) continue;
      quality[arm].citations++;
      const entry = manifest.find((e) => e.path.replace(/\\/g, "/") === file);
      if (!entry) { quality[arm].invalidCitations.push({ chat: r.index, file, line: Number(m[2]), reason: "not in frozen corpus" }); continue; }
      cited.add(file);
      const count = fs.readFileSync(path.join(bench.repo(arm), entry.path), "utf8").split(/\r?\n/).length;
      if (Number(m[2]) > count) quality[arm].invalidCitations.push({ chat: r.index, file, line: Number(m[2]), reason: "line outside source" });
    }
    const events = fs.readFileSync(base + ".jsonl", "utf8").split(/\r?\n/).flatMap((line) => { try { return [JSON.parse(line)]; } catch { return []; } });
    const calls = events.filter((e) => e.type === "item.completed" && ["command_execution", "mcp_tool_call"].includes(e.item?.type)).map((e) => e.item);
    const outputs = calls.map((c) => c.type === "command_execution" ? c.aggregated_output || "" : JSON.stringify(c.result || c.error || ""));
    const graphCalls = calls.filter((c) => c.server === "contextshrink_graph");
    for (const c of graphCalls) for (const block of c.result?.content || []) {
      if (block.type !== "text") continue;
      try { const parsed = JSON.parse(block.text); if (parsed.indexedAt) graphIndexTimes.add(parsed.indexedAt); } catch {}
    }
    const failed = calls.filter((c) => c.status === "failed" || (c.type === "command_execution" && c.exit_code));
    quality[arm].failedTools.push(...failed.map((c) => ({ chat: r.index, type: c.type, tool: c.tool || null, exitCode: c.exit_code ?? null, status: c.status })));
    evidence[arm].push({ index: r.index, task: r.task, repetition: r.repetition, shellCalls: calls.filter((c) => c.type === "command_execution").length, graphCalls: graphCalls.length, graphTools: graphCalls.map((c) => ({ tool: c.tool, arguments: c.arguments, status: c.status })), toolOutputTokens: outputs.reduce((n, output) => n + encoding.encode(output).length, 0), answerWords: r.answerWords, usage: r.usage, cost: r.cost, seconds: r.seconds });
  }
  quality[arm].citedFiles = [...cited].sort();
}
assert.equal(evidence.A.reduce((n, r) => n + r.graphCalls, 0), 0);
assert.ok(evidence.B.some((r) => r.graphCalls > 0), "Graph arm did not use its integration");
assert.equal(graphIndexTimes.size, 1, "Expected all queries to reuse a single unchanged graph index");
const summary = bench.summary();
const retrieval = Object.fromEntries(["A", "B"].map((arm) => [arm, { shellCalls: evidence[arm].reduce((n, r) => n + r.shellCalls, 0), graphCalls: evidence[arm].reduce((n, r) => n + r.graphCalls, 0), toolOutputTokens: evidence[arm].reduce((n, r) => n + r.toolOutputTokens, 0) }]));
function compare(a, b) {
  return { inputTokensAvoided: a.input_tokens - b.input_tokens, inputReductionPercent: 100 * (a.input_tokens - b.input_tokens) / a.input_tokens, outputTokensAvoided: a.output_tokens - b.output_tokens, dollarsAvoided: a.total_usd - b.total_usd, dollarReductionPercent: 100 * (a.total_usd - b.total_usd) / a.total_usd, noCacheScenarioDollarsAvoided: a.no_cache_price_scenario_usd - b.no_cache_price_scenario_usd };
}
const comparisons = { combined: compare(summary.arms.A, summary.arms.B), byTask: {}, byPair: [] };
for (const task of Object.keys(summary.byTask)) comparisons.byTask[task] = compare(summary.byTask[task].A, summary.byTask[task].B);
for (let i = 0; i < bench.CASES.length; i++) comparisons.byPair.push({ index: i, task: bench.CASES[i].id, repetition: bench.CASES[i].repetition, ...compare({ ...s.arms.A[i].usage, ...s.arms.A[i].cost }, { ...s.arms.B[i].usage, ...s.arms.B[i].cost }) });
const interruptedIndices = [...new Set((s.failedAttempts || []).map((r) => r.index))];
if (interruptedIndices.length) {
  const reduced = {};
  for (const arm of ["A", "B"]) {
    reduced[arm] = { ...summary.arms[arm] };
    for (const index of interruptedIndices) for (const key of ["input_tokens", "output_tokens", "total_usd", "no_cache_price_scenario_usd"]) reduced[arm][key] -= s.arms[arm][index].usage[key] ?? s.arms[arm][index].cost[key];
  }
  comparisons.excludingQuotaInterruptedPairs = { excludedIndices: interruptedIndices, ...compare(reduced.A, reduced.B) };
}
const manualFile = path.join(__dirname, "fresh-chat-quality.json");
const manualQuality = fs.existsSync(manualFile) ? core.json(manualFile) : null;
const result = { ...summary, comparisons, quality, evidence, retrieval, manualQuality, method: { cases: s.cases, frozenSourceFiles: manifest.length, manifestDigest: s.manifestDigest, graphVersion: s.graphVersion, freshConversations: threads.size, graphBuiltOnce: true, observedGraphIndexTimes: [...graphIndexTimes], allSourceHashesUnchanged: true, temporaryAuthRemoved: true, graphBuildModelTokens: 0, cacheControl: "Actual automatic provider cache hits; no forced equal cache state. Alternating first arm by case, same first arm per task across repetitions.", billingScope: "Observed main-model standard API-price equivalents, not subscription invoices; auxiliary approval-review usage unavailable. Cached input is part of input; reasoning output is part of output.", limits: "Two repetitions per task, stochastic outcomes, no independent semantic judge or confidence guarantee. Citation checks establish file/line existence only. Tool-output token counts are single serialized outputs, not cumulative input. No production proxy, billing or ledger involved." } };
fs.writeFileSync(path.join(__dirname, "fresh-chat-results.json"), JSON.stringify(result, null, 2) + "\n");
const usd = (v) => "$" + v.toFixed(4), fmt = (v) => v.toLocaleString("en-US");
const lines = ["# Reusing the graph across fresh chats", "", "Eight paired independent chats: four tasks, two repetitions, same frozen source and gpt-6.1-sol/high. A uses ordinary source retrieval; B has unmodified 0.2.3 graph integration. Every B chat includes setup overhead; the local index is reused.", "", "| Task (two chats per arm) | A input | B input | A USD | B USD | Dollar change with graph |", "|---|---:|---:|---:|---:|---:|"];
for (const [task, arms] of Object.entries(summary.byTask)) lines.push(`| ${task} | ${fmt(arms.A.input_tokens)} | ${fmt(arms.B.input_tokens)} | ${usd(arms.A.total_usd)} | ${usd(arms.B.total_usd)} | ${(-comparisons.byTask[task].dollarReductionPercent).toFixed(2)}% |`);
lines.push(`| **Total** | **${fmt(summary.arms.A.input_tokens)}** | **${fmt(summary.arms.B.input_tokens)}** | **${usd(summary.arms.A.total_usd)}** | **${usd(summary.arms.B.total_usd)}** | **${(-comparisons.combined.dollarReductionPercent).toFixed(2)}%** |`, "", `Overall graph input reduction: ${comparisons.combined.inputReductionPercent.toFixed(2)}%. Dollar reduction: ${comparisons.combined.dollarReductionPercent.toFixed(2)}%. Positive reductions mean savings; negative values mean extra usage/cost. ${comparisons.byPair.filter((p) => p.dollarsAvoided > 0).length} of ${bench.CASES.length} pairs cost less with the graph.`, "", "Costs use observed cached input at $0.10/M, uncached input at $2/M and output at $10/M, verified against official pricing on 2026-10-21. These are main-model API-price equivalents, not subscription invoices. Auxiliary approval-review usage is not available. No-cache amounts in JSON are pricing scenarios, not additional runs.", "", `The local graph built in ${s.graph.buildSeconds.toFixed(2)} seconds and used zero model tokens. B made ${summary.arms.B.graphCalls} graph tool calls. All ${manifest.length} source hashes were preserved in both arms. All ${threads.size} conversation IDs are distinct; sign-in copies were removed.`, "", `Citation checks: A ${quality.A.citations} citations (${quality.A.invalidCitations.length} invalid file/line locations); B ${quality.B.citations} (${quality.B.invalidCitations.length} invalid). File/line existence does not establish claim correctness.`, "", manualQuality ? "Manual source-grounded coverage review is recorded in fresh-chat-quality.json. It is a limited rubric review, not an independent blind assessment or proof of semantic equivalence." : "Manual semantic/coverage review is pending; cost results alone do not establish an equally useful answer.", "", "This is an exploratory sample, with automatic provider cache state and only two repetitions per task. First-arm order alternates by case but remains the same per task across repetitions. Do not generalize a favorable task or pair into a product-wide savings promise. See FRESH-CHAT-PROTOCOL.md and fresh-chat-results.json for prompts, per-pair outcomes, outputs, failures and limitations.", "");
if (summary.failedAttempts.length) lines.push(`Interrupted attempts: ${summary.failedAttempts.length}, with ${usd(summary.interruptedAttemptKnownUsd)} of known additional main-model usage. These attempts are preserved separately rather than substituted for completed answers. Total experiment usage including interruptions is ${usd(summary.arms.A.total_usd + summary.arms.B.total_usd + summary.interruptedAttemptKnownUsd)}. A usage-limit interruption separated the two arms of the first savings-accounting pair; cache state/timing may therefore differ further for that pair.`, "");
if (comparisons.excludingQuotaInterruptedPairs) lines.push(`Post-hoc sensitivity check excluding both arms of the quota-interrupted pair: graph input reduction ${comparisons.excludingQuotaInterruptedPairs.inputReductionPercent.toFixed(2)}%, dollar reduction ${comparisons.excludingQuotaInterruptedPairs.dollarReductionPercent.toFixed(2)}%. This is a sensitivity check, not a substitute for the prespecified overall result.`, "");
lines.push(`Retrieval diagnostics: A made ${retrieval.A.shellCalls} shell calls and returned ${fmt(retrieval.A.toolOutputTokens)} one-pass tool-output tokens; B made ${retrieval.B.shellCalls} shell calls plus ${retrieval.B.graphCalls} graph calls and returned ${fmt(retrieval.B.toolOutputTokens)} tokens. These output sizes use local o200k_base tokenization and are not total provider input or separate charges. Tool calls include source searches, reads and directory listings.`, "");
lines.push("Interpretation: this implementation reuses structural relationships, not the conclusions of earlier source reviews. In this sample it did not reliably replace enough source investigation to reduce whole-chat usage. There were two failed class traces; the adapter advertises general symbols while the native trace expects an exact function/method name. The first architecture search also returned test symbols ahead of application entry points. Failures and their costs remain included. Individual lookup estimates must not be described as measured whole-chat savings.", "", "Recommended next work: clarify callable-only tracing and missing-symbol errors; improve production-source relevance; combine graph locations with compact current-source evidence so one retrieval can replace multiple reads; and investigate source-hash-validated persistent review summaries. Validate changes with fresh held-out questions and multiple repositories before claiming a repeatable savings benefit. Existing cost results do not establish savings from any of these proposed changes.", "");
fs.writeFileSync(path.join(__dirname, "FRESH-CHAT-RESULTS.md"), lines.join("\n"));
console.log(JSON.stringify({ comparisons, arms: summary.arms, graphCalls: summary.arms.B.graphCalls, citations: { A: quality.A.citations, B: quality.B.citations }, invalidCitations: { A: quality.A.invalidCitations, B: quality.B.invalidCitations } }, null, 2));
