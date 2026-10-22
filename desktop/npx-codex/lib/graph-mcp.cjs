"use strict";
const readline = require("node:readline");
const core = require("./graph-core.cjs");
const policy = require("./graph-policy.cjs");
const project = process.argv[process.argv.indexOf("--project") + 1];
const budget = {
  type: "integer",
  minimum: 1000,
  maximum: 16000,
  description:
    "Maximum UTF-8 response bytes; default 3000. Narrow queries reduce token use.",
};
const tools = [
  {
    name: "graph_search",
    description:
      "One compact search for unfamiliar structure. Use source for known files/symbols/text; reuse findings. Missing results: use source, not repeated graph calls.",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string" },
        intent: {
          type: "string",
          enum: ["discovery", "lookup"],
          default: "lookup",
          description:
            "Use discovery only when replacing broad repository exploration. Known-symbol lookups default to lookup and receive no estimate receipt.",
        },
        limit: { type: "integer", minimum: 1, maximum: 30, default: policy.DEFAULT_LIMIT },
        file_pattern: { type: "string" },
        budget_bytes: budget,
      },
      required: ["query"],
      additionalProperties: false,
    },
  },
  {
    name: "graph_overview",
    description:
      "Get one compact architecture overview when project structure is unfamiliar. Skip if this chat already has the needed overview.",
    inputSchema: {
      type: "object",
      properties: { budget_bytes: budget },
      additionalProperties: false,
    },
  },
  {
    name: "graph_trace",
    description:
      "Trace an exact function/method for unfamiliar cross-module impact. Verify source. Missing/ambiguous trace: use source search, not repeated discovery or refreshes.",
    inputSchema: {
      type: "object",
      properties: { symbol: { type: "string" }, budget_bytes: budget },
      required: ["symbol"],
      additionalProperties: false,
    },
  },
];
// Keep calls from older chat histories working without advertising maintenance
// and advanced-query schemas in every new chat. These also remain on the CLI.
const legacyTools = ["graph_query", "graph_status", "graph_refresh"];
async function respond(request) {
  if (!request || request.jsonrpc !== "2.0") return;
  const id = request.id;
  if (id === undefined) return;
  let result;
  try {
    if (request.method === "initialize")
      result = {
        protocolVersion: [
          "2024-11-05",
          "2025-03-26",
          "2025-06-18",
          "2025-11-25",
        ].includes(request.params?.protocolVersion)
          ? request.params.protocolVersion
          : "2024-11-05",
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: "contextshrink-code-graph", version: "0.2.4" },
        instructions: policy.instructions,
      };
    else if (request.method === "ping") result = {};
    else if (request.method === "tools/list") result = { tools };
    else if (request.method === "tools/call") {
      const name = request.params?.name,
        args = request.params?.arguments || {};
      if (!tools.some((t) => t.name === name) && !legacyTools.includes(name))
        throw new core.GraphError("Unknown code graph tool.");
      let value =
        name === "graph_status"
          ? core.status(project)
          : name === "graph_refresh"
            ? await core.refresh(project)
            : await core.query(project, name.replace("graph_", ""), args);
      if (name === "graph_search") {
        try {
          value = require("./graph-estimates.cjs").attach(project, args, value);
        } catch {
          /* Estimation must never break successful graph retrieval. */
        }
      }
      result = { content: [{ type: "text", text: JSON.stringify(value) }] };
    } else {
      process.stdout.write(
        JSON.stringify({
          jsonrpc: "2.0",
          id,
          error: { code: -32601, message: "Method not found" },
        }) + "\n",
      );
      return;
    }
    process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id, result }) + "\n");
  } catch (e) {
    const message =
      e instanceof core.GraphError
        ? e.message
        : "Local code graph operation failed. Check project permissions and graph status.";
    process.stdout.write(
      JSON.stringify({
        jsonrpc: "2.0",
        id,
        result: { isError: true, content: [{ type: "text", text: message }] },
      }) + "\n",
    );
  }
}
if (require.main === module) {
  if (!process.argv.includes("--project") || !project) {
    process.stderr.write("A fixed --project is required.\n");
    process.exitCode = 1;
  } else {
    let queue = Promise.resolve();
    readline
      .createInterface({ input: process.stdin, crlfDelay: Infinity })
      .on("line", (line) => {
        if (Buffer.byteLength(line) > 65536) return;
        let request;
        try {
          request = JSON.parse(line);
        } catch {
          process.stdout.write(
            JSON.stringify({
              jsonrpc: "2.0",
              id: null,
              error: { code: -32700, message: "Parse error" },
            }) + "\n",
          );
          return;
        }
        queue = queue.then(() => respond(request));
      });
  }
}
module.exports = { tools };
