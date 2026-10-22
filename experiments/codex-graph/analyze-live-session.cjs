"use strict";
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const assert = require("node:assert/strict");
const core = require("../../desktop/npx-codex/lib/graph-core.cjs");
const root = path.resolve(__dirname, "../..");
const run = path.join(root, "desktop/dist-windows/npx-test/live-graph-21-turns");
const work = path.join(os.tmpdir(), "cs-g21-" + core.hash(root).slice(0, 8));
const state = JSON.parse(fs.readFileSync(path.join(run, "state.json"), "utf8"));
const summary = JSON.parse(fs.readFileSync(path.join(run, "summary.json"), "utf8"));
const manifest = JSON.parse(fs.readFileSync(path.join(run, "source-manifest.json"), "utf8"));
const quality = {};
for (const arm of ["A", "B"]) {
  const turns = state.arms[arm].turns;
  assert.equal(turns.length, 21, "Both arms must finish the initial review and 20 follow-ups");
  assert.ok(turns.every((t) => t.completed));
  assert.equal(turns.at(-1).cliUsage.cache_write_input_tokens || 0, 0, "Explicit cache-write usage needs its separate pricing rate");
  for (const key of ["input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"]) {
    assert.equal(turns.reduce((n, t) => n + t.usage[key], 0), turns.at(-1).cumulativeUsage[key]);
    assert.ok(turns.every((t) => t.usage[key] >= 0));
  }
  assert.ok(turns.every((t) => t.usage.cached_input_tokens <= t.usage.input_tokens && t.usage.reasoning_output_tokens <= t.usage.output_tokens));
  const changed = manifest.filter((entry) => core.hash(fs.readFileSync(path.join(work, arm, "repo", entry.path))) !== entry.sha256).map((entry) => entry.path);
  assert.deepEqual(changed, [], "Read-only review changed frozen source");
  let citations = 0, invalidCitations = [], toolFailures = 0, blockedTurns = [];
  const citedFiles = new Set();
  for (const t of turns) {
    const base = path.join(run, arm, "turn-" + String(t.index).padStart(2, "0"));
    const text = fs.readFileSync(base + ".answer.md", "utf8");
    const stderr = fs.readFileSync(base + ".stderr", "utf8");
    if (/usage limit|blocked by policy|Automatic approval review failed/i.test(stderr)) blockedTurns.push(t.index);
    for (const match of text.matchAll(/\]\(([^()\n]+?)#L(\d+)(?:[^()]*)\)/g)) {
      let file = match[1].replace(/\\/g, "/");
      if (/^https?:/i.test(file)) continue;
      file = file.replace(/^.*?\/repo\//, "").replace(/^\.\//, "");
      const source = manifest.find((entry) => entry.path.replace(/\\/g, "/") === file);
      citations++;
      if (!source) { invalidCitations.push({ step: t.index, file, line: Number(match[2]), reason: "file absent from frozen corpus" }); continue; }
      citedFiles.add(file);
      const lines = fs.readFileSync(path.join(work, arm, "repo", file), "utf8").split(/\r?\n/).length;
      if (Number(match[2]) > lines) invalidCitations.push({ step: t.index, file, line: Number(match[2]), reason: "line outside source" });
    }
    toolFailures += t.toolCalls.filter((call) => call.status === "failed").length;
  }
  quality[arm] = { sourceUnchanged: changed.length === 0, markdownFileLineCitations: citations, distinctCitedFiles: citedFiles.size, invalidFileLineCitations: invalidCitations, failedToolItems: toolFailures, permissionOrQuotaBlockedTurns: blockedTurns, scope: "Citation checks verify file/line existence, not the semantic correctness or equal completeness of the answers. Nonzero shell exits may mean no search matches." };
}
assert.ok(summary.arms.A.graphToolCalls === 0, "Baseline unexpectedly used graph tools");
assert.ok(summary.arms.B.graphToolCalls > 0, "Graph arm did not actually use graph tools");
const comparisons = {};
for (const phase of ["initialReview", "next20", "combined"]) {
  const a = summary.arms.A[phase], b = summary.arms.B[phase];
  comparisons[phase] = { inputTokensAvoided: a.input_tokens - b.input_tokens, inputReductionPercent: 100 * (a.input_tokens - b.input_tokens) / a.input_tokens, outputTokensAvoided: a.output_tokens - b.output_tokens, dollarsAvoided: a.total_usd - b.total_usd, dollarReductionPercent: 100 * (a.total_usd - b.total_usd) / a.total_usd, noCacheDollarsAvoided: a.no_cache_list_price_usd - b.no_cache_list_price_usd };
}
const pilot = JSON.parse(fs.readFileSync(path.join(run, "pilot-blocked-summary.json"), "utf8"));
const report = { ...summary, comparisons, quality, method: { initialTurns: 1, followUpTurns: 20, modelRequestsCanExceedTurns: true, sourceFrozenAndSameBeforeGraphIntegration: true, fileCount: manifest.length, sourceManifestDigest: state.manifestDigest, providerUsageBasis: "Deltas of the engine's cumulative provider usage after each resumed turn, verified to sum to final cumulative totals. CLI turn.completed usage on resume is cumulative and must not be summed directly.", cacheControl: "Automatic actual cache hits; order alternated A/B per step. Cache state was not forced equal. One pair of stochastic model sessions, no confidence interval or proof of equal answer quality.", permissionMode: state.permissionMode, billingScope: "Main-model API list-price equivalent, not a ChatGPT subscription invoice. Auxiliary approval-review usage is not exposed here.", sourceScope: "Git-visible eligible source per the graph filter; ignored, sensitive, generated, binary and oversized files excluded in both arms.", sourceRoot: root, graphIndexBuildUsesModelTokens: false, pilot: { includedInComparison: false, reason: "No source could be read because the no-approval pilot blocked retrieval", listPriceEquivalentUsd: pilot.arms.A.combined.total_usd + pilot.arms.B.combined.total_usd } } };
report.method.interface = "Installed Codex CLI engine with fresh isolated profiles, not automated desktop UI chats. Both profiles use the same model, effort and source snapshot.";
report.method.temporaryAuthRemoved = ["A", "B"].every((arm) => !fs.existsSync(path.join(work, arm, "home/.codex/auth.json")));
assert.ok(report.method.temporaryAuthRemoved, "Remove temporary sign-in copies before exporting the final report");
fs.writeFileSync(path.join(__dirname, "live-results.json"), JSON.stringify(report, null, 2) + "\n");
const usd = (n) => "$" + n.toFixed(4);
const lines = ["# Live Horizon source-review comparison", "", `${report.model}, ${report.effort} reasoning; initial review plus 20 follow-ups per arm.`, "", "| Phase | A input | B input | A cached input | B cached input | A output | B output | A USD | B USD |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|"];
for (const phase of ["initialReview", "next20", "combined"]) { const a = report.arms.A[phase], b = report.arms.B[phase]; lines.push(`| ${phase} | ${a.input_tokens} | ${b.input_tokens} | ${a.cached_input_tokens} | ${b.cached_input_tokens} | ${a.output_tokens} | ${b.output_tokens} | ${usd(a.total_usd)} | ${usd(b.total_usd)} |`); }
const delta = report.comparisons.combined;
lines.push("", `Graph B used ${delta.inputTokensAvoided.toLocaleString("en-US")} fewer input tokens (${delta.inputReductionPercent.toFixed(2)}%) and cost ${usd(delta.dollarsAvoided)} less (${delta.dollarReductionPercent.toFixed(2)}%) in this single pair.`, "", "Costs use observed cache hits at $2/M input, $0.10/M cached input and $10/M output. Cached tokens are part of input; reasoning tokens are part of output. Total input includes history carried into repeated model requests. These are API-price equivalents, not subscription invoices.", "", `If all of the same input usage were priced uncached, the arithmetic would be ${usd(report.arms.A.combined.no_cache_list_price_usd)} for A and ${usd(report.arms.B.combined.no_cache_list_price_usd)} for B. That is a pricing scenario, not another measured run.`, "", `B made ${report.arms.B.graphToolCalls} graph calls during initial discovery; one class trace failed and its cost is retained. Later steps used source reads and existing context. The blocked pilot is excluded from the comparison and records ${usd(report.method.pilot.listPriceEquivalentUsd)} in additional main-model API-price-equivalent usage.`, "", `Both arms completed 21 turns. All ${manifest.length} source hashes remain unchanged; temporary sign-in copies were removed. A has ${quality.A.markdownFileLineCitations} file/line citations across ${quality.A.distinctCitedFiles} files and B has ${quality.B.markdownFileLineCitations} across ${quality.B.distinctCitedFiles}; no referenced file/line was missing. This verifies citation existence, not semantic correctness.`, "", "Equal prompts and snapshots do not prove equal review quality. One stochastic pair with a small gain does not establish reliable future session savings. The test used the installed CLI engine with fresh profiles and isolated source snapshots, rather than automated desktop UI chats. Only main-model usage is available; auxiliary approval-review usage is not exposed. See live-results.json for quality checks, cache limitations, pilot costs and no-cache estimates.", "");
fs.writeFileSync(path.join(__dirname, "LIVE-RESULTS.md"), lines.join("\n"));
console.log(JSON.stringify({ comparisons, quality, arms: report.arms, excludedPilotUsd: report.method.pilot.listPriceEquivalentUsd }, null, 2));
