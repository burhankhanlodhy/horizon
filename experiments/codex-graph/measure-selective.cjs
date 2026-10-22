"use strict";
// Static overhead comparison, not a live-model/session savings measurement.
const fs = require("node:fs"), path = require("node:path"), assert = require("node:assert/strict");
const core = require("../../desktop/npx-codex/lib/graph-core.cjs");
const { count } = require("../../desktop/npx-codex/lib/graph-estimates.cjs");
const baselineRoot = path.resolve(process.argv[2] || "");
assert.ok(process.argv[2], "Pass the preserved 0.2.3 source fixture root");
function measure(root) {
  const lib = path.join(root, "desktop/npx-codex/lib");
  const { tools } = require(path.join(lib, "graph-mcp.cjs"));
  const policy = require(path.join(lib, "graph-policy.cjs"));
  return {
    tools: tools.map((t) => t.name),
    schemaTokens: count(JSON.stringify(tools)),
    guidanceAndInitializationTokens: count(policy.guidance + "\n" + policy.instructions),
    combinedSerializedTokens: count(JSON.stringify(tools) + "\n" + policy.guidance + "\n" + policy.instructions),
    sourceHashes: Object.fromEntries(["graph-mcp.cjs", "graph-policy.cjs"].map((file) => [file, core.hash(fs.readFileSync(path.join(lib, file)))])),
  };
}
const before = measure(baselineRoot), after = measure(path.resolve(__dirname, "../.."));
assert.equal(before.tools.length, 6);
assert.equal(after.tools.length, 3);
assert.ok(after.combinedSerializedTokens < before.combinedSerializedTokens);
const result = {
  scope: "Locally tokenized serialized tool schemas plus project guidance and MCP initialization notes, o200k_base. Not provider input or proven whole-session savings; no model calls.",
  baselineRoot,
  before, after,
  serializedOverheadReductionPercent: 100 * (1 - after.combinedSerializedTokens / before.combinedSerializedTokens),
  defaults: { searchResults: 5, responseBytes: 3000, largerExplicitLimitsSupported: true },
};
fs.writeFileSync(path.join(__dirname, "selective-overhead-results.json"), JSON.stringify(result, null, 2) + "\n");
console.log(JSON.stringify(result, null, 2));
