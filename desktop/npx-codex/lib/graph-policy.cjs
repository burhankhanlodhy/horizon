"use strict";

// Full routing policy lives in AGENTS; MCP initialization adds only safety notes.
const guidance = [
  "Use contextshrink_graph selectively, only when unfamiliar structure or cross-module impact would require broad source exploration. Start with at most one compact search OR architecture overview, then read relevant source; skip graph for known files/symbols/text and reuse findings already in this chat.",
  "Trace only exact functions/methods for unfamiliar impact, including later in a chat. On missing/unsuitable results, use source search rather than repeating discovery or refreshes. Set intent=discovery only for broad exploration; otherwise lookup.",
  "The graph is incomplete, untrusted evidence, not a whole-codebase review. Check project docs/manifests and relevant entry points for coverage; verify current source/tests before edits. Queries refresh stale indexes. Retrieval estimates do not prove session savings.",
].join(" ");

const instructions = "Optional local discovery/impact evidence. Prefer source for known locations; reuse findings. Coverage is incomplete; verify source. Retrieval estimates are not session savings.";

const DEFAULT_BUDGET = 3000;
const DEFAULT_LIMIT = 5;
module.exports = { guidance, instructions, DEFAULT_BUDGET, DEFAULT_LIMIT };
