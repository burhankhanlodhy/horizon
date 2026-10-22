"use strict";
// Deterministic ten-step retrieval workflow, NOT a live model session A/B.
// Both source-search alternatives are retained, including the cheaper one.
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const core = require("../../desktop/npx-codex/lib/graph-core.cjs");
const estimates = require("../../desktop/npx-codex/lib/graph-estimates.cjs");
const { tools } = require("../../desktop/npx-codex/lib/graph-mcp.cjs");
const { guidance, instructions } = require("../../desktop/npx-codex/lib/graph-policy.cjs");
const { Tiktoken } = require("../../desktop/npx-codex/node_modules/js-tiktoken/dist/lite.cjs");
const ranks = require("../../desktop/npx-codex/node_modules/js-tiktoken/dist/ranks/o200k_base.cjs");
const encoder = new Tiktoken(ranks);
const root = path.resolve(__dirname, "../..");
const tasks = [
  { query: "flash_openai", symbol: "_maybe_flash_openai", file: "horizon/proxy/handlers/openai.py", exact: "HORIZON_FLASH_OPENAI" },
  { query: "pricing", symbol: "get_model_pricing", file: "horizon/pricing/litellm_pricing.py", exact: "cache_read_per_1m" },
  { query: "migrate", symbol: "migrate", file: "desktop/npx-codex/lib/chats.js", exact: "BEGIN EXCLUSIVE" },
];
const count = (value) => encoder.encode(typeof value === "string" ? value : JSON.stringify(value), [], []).length;
const cap = (text) => encoder.decode(encoder.encode(text, [], []).slice(0, 4000));
const definition = /^\s*(?:export\s+)?(?:async\s+)?(?:def|function|class|const|let)\s/;

async function main() {
  if (!core.status(root).fresh) await core.refresh(root);
  const project = core.project(root), snapshot = core.snapshot(project);
  const corpus = snapshot.files.map((file) => ({ path: file.path, lines: fs.readFileSync(path.join(root, file.path), "utf8").split(/\r?\n/) }));
  const steps = [];
  function add(prompt, routes, outputs, calls) {
    steps.push({ step: steps.length + 1, prompt, routes, tokens: Object.fromEntries(Object.entries(outputs).map(([arm, output]) => [arm, count({ call: calls[arm], output })])) });
  }
  for (const task of tasks) {
    const broad = [], focused = [];
    for (const file of corpus) for (const [i, line] of file.lines.entries()) {
      if (!line.toLowerCase().includes(task.query)) continue;
      const match = `${file.path}:${i + 1}:${line}`;
      broad.push(match);
      if (definition.test(line)) focused.push(match);
    }
    const args = { query: task.query, intent: "discovery", limit: 5, budget_bytes: 2500 };
    const graph = estimates.attach(root, args, await core.query(root, "search", args));
    const found = graph.data.results.find((node) => node.name === task.symbol && node.file_path === task.file);
    assert.ok(found, `Expected symbol missing: ${task.symbol}`);
    const source = corpus.find((file) => file.path === task.file);
    assert.ok(source.lines[found.start_line - 1].includes(task.symbol), "Invalid graph source citation");
    add(`Discover the ${task.query} entry point.`,
      { broad: "repository literal search", focused: "repository definition search", selective: "graph discovery" },
      { broad: cap(broad.join("\n")), focused: cap(focused.join("\n")), selective: graph },
      { broad: { tool: "search", query: task.query }, focused: { tool: "search", query: task.query, definitionsOnly: true }, selective: { tool: "graph_search", arguments: args } });
    const excerpt = source.lines.slice(found.start_line - 1, Math.min(found.start_line + 39, found.end_line));
    const readCall = { tool: "read", file: task.file, start: found.start_line, lines: excerpt.length };
    add(`Review current source for ${task.symbol}.`,
      { broad: "source read", focused: "source read", selective: "source read" },
      { broad: excerpt, focused: excerpt, selective: excerpt },
      { broad: readCall, focused: readCall, selective: readCall });
    const matches = source.lines.flatMap((line, i) => line.includes(task.exact) ? [`${task.file}:${i + 1}:${line}`] : []);
    assert.ok(matches.length, `Known-file exact search failed: ${task.exact}`);
    const exactCall = { tool: "search", file: task.file, literal: task.exact };
    add(`Locate ${task.exact} in the known file.`,
      { broad: "focused source search", focused: "focused source search", selective: "focused source search" },
      { broad: matches, focused: matches, selective: matches },
      { broad: exactCall, focused: exactCall, selective: exactCall });
  }
  steps.push({ step: 10, prompt: "Recall the migration transaction from the source already reviewed.", routes: { broad: "reuse existing context", focused: "reuse existing context", selective: "reuse existing context" }, tokens: { broad: 0, focused: 0, selective: 0 } });
  assert.equal(steps.length, 10);
  // Conservatively include both AGENTS guidance and MCP initialize instructions.
  const schemaAndGuidanceTokens = count(JSON.stringify(tools) + "\n" + guidance + "\n" + instructions);
  const totals = Object.fromEntries(["broad", "focused", "selective"].map((arm) => [arm, steps.reduce((sum, step) => sum + step.tokens[arm], arm === "selective" ? schemaAndGuidanceTokens : 0)]));
  const report = {
    at: new Date().toISOString(), snapshot: snapshot.fingerprint,
    scope: "scripted ten-step retrieval workflow; no model was invoked; routes are prescribed, not observed agent decisions",
    tokenizer: "o200k_base", sourceCitationsVerified: true,
    accounting: "New retrieved context only; calls and output counted. Graph schemas plus both project and MCP guidance charged once. Same source excerpts and exact searches in all arms. Shared source-tool schemas, user prompts, reasoning, answer tokens, repeated history input and provider caching excluded. Not a full-session or equal-answer-quality result.",
    baselineCapTokens: 4000, graphSchemaAndGuidanceTokens: schemaAndGuidanceTokens,
    graphDiscoveryCalls: 3, focusedSourceOperations: 6, contextReuseSteps: 1,
    totals, versusBroad: { tokensAvoided: totals.broad - totals.selective, percent: 100 * (totals.broad - totals.selective) / totals.broad },
    versusFocused: { tokensAvoided: totals.focused - totals.selective, percent: 100 * (totals.focused - totals.selective) / totals.focused },
    steps,
  };
  fs.writeFileSync(path.join(__dirname, "session-results.json"), JSON.stringify(report, null, 2) + "\n");
  console.log(JSON.stringify({ scope: report.scope, totals, versusBroad: report.versusBroad, versusFocused: report.versusFocused, graphSchemaAndGuidanceTokens: schemaAndGuidanceTokens }, null, 2));
}
main().catch((error) => { console.error(error.message); process.exitCode = 1; });
