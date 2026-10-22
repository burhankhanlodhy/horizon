"use strict";
const fs = require("node:fs");
const path = require("node:path");
const { parse } = require("smol-toml");
const core = require("./graph-core.cjs");
const policy = require("./graph-policy.cjs");
const VERSION = require("../package.json").version;
const START = "# >>> ContextShrink code graph >>>",
  END = "# <<< ContextShrink code graph <<<";
const ASTART = "<!-- ContextShrink code graph: start -->",
  AEND = "<!-- ContextShrink code graph: end -->";
function read(file) {
  return fs.existsSync(file) ? fs.readFileSync(file, "utf8") : "";
}
function safeConfig(text) {
  try {
    return text.trim() ? parse(text) : {};
  } catch {
    throw new core.GraphError(
      "Project Codex config.toml is not valid TOML. No integration files were changed.",
    );
  }
}
function install(root, env = process.env) {
  const p = core.project(root, env),
    record = path.join(p.dir, "integration.json");
  const previous = core.json(record);
  const config = path.join(p.root, ".codex", "config.toml"),
    agents = path.join(p.root, "AGENTS.md");
  if (previous) {
    if (
      previous.root !== p.root ||
      !read(config).includes(previous.configAppend) ||
      !read(agents).includes(previous.agentsAppend)
    )
      throw new core.GraphError(
        "The owned graph integration was edited. It was left unchanged.",
      );
    if (previous.runtimeVersion === VERSION)
      return { enabled: true, alreadyEnabled: true, project: p.root };
    const oldConfig = read(config),
      oldAgents = read(agents);
    try {
      disable(root, env);
      return { ...install(root, env), upgraded: true };
    } catch (error) {
      core.atomic(config, oldConfig);
      core.atomic(agents, oldAgents);
      core.atomic(record, JSON.stringify(previous));
      throw error;
    }
  }
  const before = read(config),
    oldAgents = read(agents),
    doc = safeConfig(before);
  if (
    doc.mcp_servers?.contextshrink_graph ||
    before.includes(START) ||
    oldAgents.includes(ASTART)
  )
    throw new core.GraphError(
      "A graph MCP entry or instruction marker already exists. It was not replaced.",
    );
  const runtime = path.join(p.base, "runtime-" + VERSION);
  fs.mkdirSync(runtime, { recursive: true, mode: 0o700 });
  for (const name of [
    "graph-core.cjs",
    "graph-mcp.cjs",
    "graph-policy.cjs",
    "graph-estimates.cjs",
    "graph-tokenizer-LICENSE.txt",
    "graph-engine-manifest.json",
    "graph-engine-LICENSE.txt",
    "graph-engine-THIRD_PARTY_NOTICES.md",
  ])
    core.atomic(
      path.join(runtime, name),
      fs.readFileSync(path.join(__dirname, name)),
    );
  const tokenizerRoot = path.dirname(
    path.dirname(require.resolve("js-tiktoken/lite")),
  );
  const base64Root = path.dirname(require.resolve("base64-js"));
  for (const [name, source, members] of [
    [
      "js-tiktoken",
      tokenizerRoot,
      ["package.json", "dist/lite.cjs", "dist/ranks/o200k_base.cjs"],
    ],
    ["base64-js", base64Root, ["package.json", "index.js", "LICENSE"]],
  ])
    for (const member of members)
      core.atomic(
        path.join(runtime, "node_modules", name, member),
        fs.readFileSync(path.join(source, member)),
      );
  const block = `${START}\n[mcp_servers.contextshrink_graph]\ncommand = ${JSON.stringify(process.execPath)}\nargs = ${JSON.stringify([path.join(runtime, "graph-mcp.cjs"), "--project", p.root])}\nstartup_timeout_sec = 120\ntool_timeout_sec = 180\n${END}\n`;
  const guidance = `${ASTART}\n${policy.guidance}\n${AEND}\n`;
  const configAppend = (before && !before.endsWith("\n") ? "\n" : "") + block;
  const agentsAppend =
    (oldAgents && !oldAgents.endsWith("\n") ? "\n" : "") + guidance;
  safeConfig(before + configAppend);
  const state = {
    version: 1,
    runtimeVersion: VERSION,
    root: p.root,
    configExisted: fs.existsSync(config),
    agentsExisted: fs.existsSync(agents),
    configAppend,
    agentsAppend,
  };
  // Journal ownership before either write; retries/disable can safely recover.
  core.atomic(record, JSON.stringify(state));
  try {
    core.atomic(config, before + configAppend);
    core.atomic(agents, oldAgents + agentsAppend);
  } catch (e) {
    if (read(config) === before + configAppend) {
      if (state.configExisted) core.atomic(config, before);
      else fs.unlinkSync(config);
    }
    if (read(agents) === oldAgents + agentsAppend) {
      if (state.agentsExisted) core.atomic(agents, oldAgents);
      else fs.unlinkSync(agents);
    }
    fs.unlinkSync(record);
    throw new core.GraphError(
      "Could not install project graph integration. Original files were restored.",
    );
  }
  return {
    enabled: true,
    project: p.root,
    config,
    agents,
    restart:
      "Restart Codex and open a chat in this trusted project to load its MCP tools.",
  };
}
function disable(root, env = process.env) {
  const p = core.project(root, env),
    record = path.join(p.dir, "integration.json"),
    state = core.json(record);
  if (!state) return { enabled: false, project: p.root };
  if (state.root !== p.root)
    throw new core.GraphError(
      "The graph ownership record does not match this project.",
    );
  const config = path.join(p.root, ".codex", "config.toml"),
    agents = path.join(p.root, "AGENTS.md");
  const old = read(config),
    oldAgents = read(agents);
  for (const [text, append] of [
    [old, state.configAppend],
    [oldAgents, state.agentsAppend],
  ])
    if (!append || text.split(append).length !== 2)
      throw new core.GraphError(
        "Owned graph settings or instructions changed. Files were left unchanged.",
      );
  const next = old.replace(state.configAppend, ""),
    nextAgents = oldAgents.replace(state.agentsAppend, "");
  safeConfig(next);
  core.atomic(config, next);
  core.atomic(agents, nextAgents);
  if (!state.configExisted && !next) fs.unlinkSync(config);
  if (!state.agentsExisted && !nextAgents) fs.unlinkSync(agents);
  fs.unlinkSync(record);
  return { enabled: false, project: p.root, localIndexKept: true };
}
module.exports = { install, disable };
