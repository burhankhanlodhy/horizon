"use strict";
const core = require("./graph-core.cjs");
const config = require("./graph-config.cjs");
const HELP =
  'graph init|index|refresh|status|overview|disable [PROJECT]\ngraph search|trace|query PROJECT "QUERY_OR_SYMBOL"\ngraph export PROJECT [MAP.html]\ninit builds a local graph and enables project-scoped Codex MCP tools; index only builds it.';
async function run(args, ctx) {
  const [action, ...rest] = args;
  if (
    ![
      "init",
      "index",
      "refresh",
      "status",
      "overview",
      "disable",
      "search",
      "trace",
      "query",
      "export",
    ].includes(action)
  ) {
    ctx.err(HELP);
    return 2;
  }
  const needsQuery = ["search", "trace", "query"].includes(action);
  if (
    (needsQuery && rest.length !== 2) ||
    (!needsQuery && action !== "export" && rest.length > 1) ||
    (action === "export" && (rest.length < 1 || rest.length > 2))
  ) {
    ctx.err(HELP);
    return 2;
  }
  const root = rest[0] || process.cwd(),
    env = ctx.env || process.env,
    options = ctx.graphOptions || {};
  try {
    let result;
    if (action === "init") {
      result = await core.refresh(root, env, options);
      result.integration = await core.withLock(core.project(root, env), () =>
        config.install(root, env),
      );
    } else if (["index", "refresh"].includes(action))
      result = await core.refresh(root, env, options);
    else if (action === "status") result = core.status(root, env);
    else if (action === "disable")
      result = await core.withLock(core.project(root, env), () =>
        config.disable(root, env),
      );
    else if (action === "export")
      result = await require("./graph-export.cjs").exportMap(
        root,
        rest[1],
        env,
        options,
      );
    else
      result = await core.query(
        root,
        action,
        action === "trace" ? { symbol: rest[1] } : { query: rest[1] },
        env,
        options,
      );
    ctx.out(JSON.stringify(result, null, 2));
    return 0;
  } catch (e) {
    ctx.err(
      e instanceof core.GraphError
        ? e.message
        : "Could not complete the local graph operation. Check Git, project permissions, and graph status.",
    );
    return 1;
  }
}
module.exports = { run };
