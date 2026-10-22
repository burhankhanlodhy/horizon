"use strict";
const fs = require("node:fs"), path = require("node:path"), assert = require("node:assert/strict");
const bench = require("./selective-live.cjs");
const { count: tokenCount } = require("../../desktop/npx-codex/lib/graph-estimates.cjs");
const s = bench.state();
assert.ok(s, "Prepare and finish the experiment first");
bench.verifySources();
const keys = ["input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"];
function aggregate(turns) {
  const usage = Object.fromEntries(keys.map((k) => [k, turns.reduce((n, t) => n + t.usage[k], 0)]));
  return { ...usage, ...bench.cost(usage), seconds: turns.reduce((n, t) => n + t.seconds, 0), toolCalls: turns.reduce((n, t) => n + t.tools.length, 0), graphCalls: turns.reduce((n, t) => n + t.tools.filter((i) => i.server === "contextshrink_graph").length, 0) };
}
const manifest = JSON.parse(fs.readFileSync(path.join(bench.RUN, "source-manifest.json"), "utf8"));
const sources = new Map(manifest.map((f) => [f.path.replace(/\\/g, "/"), f]));
const arms = {}, quality = {};
for (const arm of ["A", "B"]) {
  const a = s.arms[arm], turns = a.turns;
  assert.equal(turns.length, 5, "All five turns must finish in both arms");
  assert.ok(turns.every((t) => t.completed && !t.timeout));
  assert.ok(!fs.existsSync(path.join(bench.home(arm), ".codex/auth.json")), "Temporary sign-in must be removed first");
  for (const t of turns) {
    assert.ok(t.cumulativeUsage, "Provider cumulative usage unavailable");
    assert.equal(t.cliUsage?.cache_write_input_tokens || 0, 0, "Explicit cache writes need separate pricing");
    assert.ok(keys.every((k) => Number.isFinite(t.usage[k]) && t.usage[k] >= 0));
    assert.ok(t.usage.cached_input_tokens <= t.usage.input_tokens);
    assert.ok(t.usage.reasoning_output_tokens <= t.usage.output_tokens);
  }
  const combined = aggregate(turns);
  const interrupted = (s.failedAttempts || []).filter((t) => t.arm === arm);
  const observedIncludingInterruptions = aggregate([...turns, ...interrupted]);
  for (const key of keys) assert.equal(observedIncludingInterruptions[key], turns.at(-1).cumulativeUsage[key], "Resumed usage double-counted or interruption omitted");
  let citations = 0, invalidCitations = [], failedTools = [], graphTools = [], answerWords = 0, onePassToolOutputTokens = 0;
  const citedFiles = new Set();
  for (const t of turns) {
    const file = path.join(bench.RUN, arm, `turn-${String(t.index).padStart(2, "0")}.answer.md`);
    const text = fs.readFileSync(file, "utf8");
    const events = fs.readFileSync(file.replace(/\.answer\.md$/, ".jsonl"), "utf8").split(/\r?\n/).flatMap((line) => { try { return [JSON.parse(line)]; } catch { return []; } });
    for (const event of events.filter((e) => e.type === "item.completed")) {
      const i = event.item;
      if (i.type === "command_execution") onePassToolOutputTokens += tokenCount(i.aggregated_output || "");
      if (i.type === "mcp_tool_call") onePassToolOutputTokens += tokenCount(JSON.stringify(i.result || {}));
    }
    answerWords += text.split(/\s+/).filter(Boolean).length;
    for (const match of text.matchAll(/\]\(([^()\n]+?)#L(\d+)(?:[^()]*)\)/g)) {
      let relative = match[1].replace(/\\/g, "/").replace(/^.*?\/repo\//, "").replace(/^\.\//, "");
      if (/^https?:/i.test(relative)) continue;
      citations++;
      const entry = sources.get(relative), line = Number(match[2]);
      if (!entry) { invalidCitations.push({ turn: t.index, file: relative, line, reason: "not in frozen source" }); continue; }
      citedFiles.add(relative);
      const count = fs.readFileSync(path.join(bench.repo(arm), relative), "utf8").split(/\r?\n/).length;
      if (line < 1 || line > count) invalidCitations.push({ turn: t.index, file: relative, line, reason: "line outside source" });
    }
    failedTools.push(...t.tools.filter((i) => i.status === "failed" || (i.exitCode != null && i.exitCode !== 0)).map((i) => ({ turn: t.index, ...i })));
    graphTools.push(...t.tools.filter((i) => i.server === "contextshrink_graph").map((i) => ({ turn: t.index, ...i })));
  }
  quality[arm] = { citations, distinctCitedFiles: citedFiles.size, invalidCitations, failedTools, graphTools, answerWords, onePassToolOutputTokens, sourceHashesUnchanged: true };
  arms[arm] = { threadId: a.threadId, initialReview: aggregate(turns.slice(0, 1)), uninterruptedFirstTwo: aggregate(turns.slice(0, 2)), followUps: aggregate(turns.slice(1)), combined, observedIncludingInterruptions, turns };
}
assert.notEqual(arms.A.threadId, arms.B.threadId, "Arms must be independent chats");
assert.equal(arms.A.combined.graphCalls, 0, "Baseline used graph tools");
const comparisons = {};
for (const phase of ["initialReview", "uninterruptedFirstTwo", "followUps", "combined", "observedIncludingInterruptions"]) {
  const a = arms.A[phase], b = arms.B[phase];
  comparisons[phase] = { inputTokensAvoided: a.input_tokens - b.input_tokens, inputReductionPercent: 100 * (1 - b.input_tokens / a.input_tokens), dollarsAvoided: a.total_usd - b.total_usd, dollarReductionPercent: 100 * (1 - b.total_usd / a.total_usd) };
}
const manualFile = path.join(__dirname, "selective-live-quality.json");
const manual = fs.existsSync(manualFile) ? JSON.parse(fs.readFileSync(manualFile, "utf8")) : null;
const core = require("../../desktop/npx-codex/lib/graph-core.cjs");
const graphStatus = core.status(bench.repo("B"), { ...process.env, HOME: bench.home("B"), USERPROFILE: bench.home("B") });
assert.equal(graphStatus.indexedAt, s.graph.indexedAt, "Graph was unexpectedly rebuilt");
const report = { model: s.model, reasoningEffort: s.reasoningEffort, version: s.graphVersion, prices: s.prices, sourceFiles: s.sourceFiles, sourceManifestDigest: s.manifestDigest, graphBuild: s.graph, prompts: s.prompts, commonInstructions: s.commonInstructions, order: s.order, arms, comparisons, quality, manual, failedAttempts: s.failedAttempts || [],
  method: { sourceHashesUnchanged: true, temporaryAuthRemoved: true, interface: "Actual installed Codex CLI engine, isolated homes, two fresh conversations with four resumed follow-ups each", usage: "Differences in cumulative rollout provider usage; each arm telescopes to its final cumulative total. Cached input is a subset of input; reasoning is a subset of output.", cache: "Automatic actual hits, not forced equal. Order alternates per turn.", coverage: "Eligible frozen source, excluding experiment results/graph validation docs to reduce outcome leakage; shared native code graph built once with no model tokens", billing: "Main-model standard API-price equivalents, not a subscription invoice or quota conversion. Auxiliary approval-review usage unavailable.", limits: "One stochastic chat per arm, selected five-turn task sequence, no replication or equal-quality proof. Citation existence alone does not validate claims.", actualGraphUse: arms.B.combined.graphCalls > 0, approval: "Outer launch initially blocked before execution; user then explicitly approved the named payload/destination and the test launched. One capacity-interrupted attempt was explicitly archived and retried once; known usage and the partial-context confound are retained." } };
report.method.graphIndexReused = true;
report.method.scope = "Graph workflow only: both arms use the same direct model provider, without production proxy compression. This does not measure total ContextShrink proxy savings.";
report.method.nativeMemory = { A: { stage1_outputs: 0, jobs: 0 }, B: { stage1_outputs: 0, jobs: 0 }, evidence: "Read-only SQLite count check after both chats completed" };
fs.writeFileSync(path.join(__dirname, "selective-live-results.json"), JSON.stringify(report, null, 2) + "\n");
const usd = (n) => "$" + n.toFixed(4), n = (v) => v.toLocaleString("en-US");
const lines = ["# 0.2.4: paired five-turn chat pilot", "", `Model ${s.model}, ${s.reasoningEffort} reasoning; ${s.sourceFiles} identical frozen source files. Two independent conversations, five turns each.`, "", "| Phase | A input | B input | A cached | B cached | A output | B output | A USD | B USD |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|"];
for (const phase of ["initialReview", "uninterruptedFirstTwo", "followUps", "combined", "observedIncludingInterruptions"]) {
  const a = arms.A[phase], b = arms.B[phase];
  lines.push(`| ${phase} | ${n(a.input_tokens)} | ${n(b.input_tokens)} | ${n(a.cached_input_tokens)} | ${n(b.cached_input_tokens)} | ${n(a.output_tokens)} | ${n(b.output_tokens)} | ${usd(a.total_usd)} | ${usd(b.total_usd)} |`);
}
lines.push("", `Input reduction (positive means less for B): ${comparisons.combined.inputReductionPercent.toFixed(2)}%. Dollar reduction: ${comparisons.combined.dollarReductionPercent.toFixed(2)}% (${usd(comparisons.combined.dollarsAvoided)}).`, "", `B made ${arms.B.combined.graphCalls} graph calls; A made none. Graph indexing used no model tokens. Both conversations completed five turns; source hashes are unchanged and temporary sign-in copies removed.`, "", "Costs include observed cache hits at $2/M uncached input, $0.10/M cached input and $10/M output. These are main-model API-price equivalents, not subscription charges or extra-turn estimates. Total input includes history repeatedly supplied to the model.", "", "## Quality and limits", "", `Citation locations: A ${quality.A.citations} citations across ${quality.A.distinctCitedFiles} files, ${quality.A.invalidCitations.length} invalid; B ${quality.B.citations} across ${quality.B.distinctCitedFiles}, ${quality.B.invalidCitations.length} invalid. Existence checks do not establish correctness. Nonzero search exits are recorded and can mean no matches.`, "", manual ? [manual.method, manual.summary, ...manual.cases.map((c) => `Turn ${c.turn} (${c.task}): A ${c.A}; B ${c.B}. ${c.notes}`), ...manual.executionCaveats].join("\n\n") : "Manual review not yet attached. Do not treat lower usage as equivalent-quality savings until answer coverage is reviewed.", "", "This is one pilot pair, with alternating arm order and automatic cache state. It cannot establish repeatable savings. It is not comparable to earlier experiments as an isolated version effect: prompts, corpus and cache state differ. Production ledgers, personal config and deployed services were unchanged. See SELECTIVE-LIVE-PROTOCOL.md and selective-live-results.json for evidence and limits.", "");
if (s.failedAttempts?.length) lines.push("## Capacity interruption", "", "One B pricing attempt failed with selected model at capacity. Its evidence and known usage are retained separately and included in observedIncludingInterruptions. combined sums the five successful turns only. The same chat resumed after the failure, so the extra partial context can affect later calls; excluding the failed attempt's direct cost does not remove that confound. The uninterruptedFirstTwo comparison is unaffected by this failure. No model was switched.", "");
const toolReduction = 100 * (1 - quality.B.onePassToolOutputTokens / quality.A.onePassToolOutputTokens);
lines.push("## Interpretation", "", `Savings did not hold in this pilot. Across successful turns, B used ${(-comparisons.combined.inputReductionPercent).toFixed(2)}% more total input and cost ${(-comparisons.combined.dollarReductionPercent).toFixed(2)}% more. Including known interruption usage, cost was ${usd(arms.B.observedIncludingInterruptions.total_usd)} versus ${usd(arms.A.observedIncludingInterruptions.total_usd)}. The uninterrupted first two turns already cost ${(-comparisons.uninterruptedFirstTwo.dollarReductionPercent).toFixed(2)}% more for B; their input was ${(-comparisons.uninterruptedFirstTwo.inputReductionPercent).toFixed(2)}% higher.`, "", `The extra uncached input in the interrupted pricing phase dominates the full-session dollar gap. It is not an isolated causal estimate of graph overhead. Locally tokenized one-pass tool outputs were ${n(quality.A.onePassToolOutputTokens)} for A and ${n(quality.B.onePassToolOutputTokens)} for B (${toolReduction.toFixed(2)}% smaller), while successful-turn tool calls were ${arms.A.combined.toolCalls} versus ${arms.B.combined.toolCalls}. Smaller retrieval/fewer tools did not translate into lower whole-chat usage. These local diagnostic counts differ from provider usage.`, "", "The policy routed selectively: one graph overview, then source-only focused work and tool-free synthesis. Both architectures covered the expected components; the limited manual review found generally useful answers, with A's operator-auth omission. This pilot does not validate a graph savings claim or a dollar credit. Keep graph use optional and diagnostics uncredited until replicated comparable sessions show a net gain.", "", "This evaluates the graph feature alone. Neither arm used production proxy compression, so it does not measure ContextShrink's separate compression savings.", "");
fs.writeFileSync(path.join(__dirname, "SELECTIVE-LIVE-RESULTS.md"), lines.join("\n"));
console.log(JSON.stringify({ comparisons, arms: Object.fromEntries(["A", "B"].map((a) => [a, { combined: arms[a].combined, graphTools: quality[a].graphTools, invalidCitations: quality[a].invalidCitations }])) }, null, 2));
