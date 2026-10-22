"use strict";
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const crypto = require("node:crypto");
const { spawnSync } = require("node:child_process");
const manifest = require("./graph-engine-manifest.json");
const policy = require("./graph-policy.cjs");

class GraphError extends Error {}
const hash = (value) => crypto.createHash("sha256").update(value).digest("hex");
const EXCLUDED_DIR =
  /(^|\/)(?:\.git|\.claude|\.codex|\.contextshrink|\.horizon|node_modules|vendor|target|dist|build|coverage|__pycache__|\.venv|venv)(\/|$)/i;
const SENSITIVE =
  /(^|\/)(?:\.env(?:\..*)?|.*\.(?:pem|key|p12|pfx)|credentials(?:\..*)?|secrets?(?:\..*)?|.*\.min\.js|(?:package-lock|pnpm-lock|yarn\.lock|uv\.lock|Cargo\.lock))(?:$|\/)/i;
const EXTENSIONS = new Set(
  ".py .pyi .js .jsx .mjs .cjs .ts .tsx .go .rs .java .c .h .cc .cpp .hpp .cs .rb .php .swift .kt .kts .scala .sh .ps1 .lua .ex .exs .vue .svelte .sql .json .toml .yaml .yml .md .mdx .proto .graphql".split(
    " ",
  ),
);
function atomic(file, data) {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const tmp = file + "." + crypto.randomBytes(5).toString("hex") + ".tmp";
  try {
    fs.writeFileSync(tmp, data, { mode: 0o600 });
    fs.renameSync(tmp, file);
  } finally {
    if (fs.existsSync(tmp)) fs.unlinkSync(tmp);
  }
}
function json(file) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch (e) {
    if (e.code === "ENOENT") return null;
    throw new GraphError(
      "The local graph state is unreadable. It was kept unchanged.",
    );
  }
}
function git(root, args) {
  let trusted = root;
  while (
    !fs.existsSync(path.join(trusted, ".git")) &&
    path.dirname(trusted) !== trusted
  )
    trusted = path.dirname(trusted);
  const r = spawnSync(
    "git",
    ["-c", "safe.directory=" + trusted.replaceAll("\\", "/"), ...args],
    {
      cwd: root,
      encoding: "utf8",
      windowsHide: true,
      timeout: 30000,
      maxBuffer: 16 * 1024 * 1024,
    },
  );
  if (r.error || r.status !== 0)
    throw new GraphError(
      "Graph indexing needs Git and a readable Git repository.",
    );
  return r.stdout;
}
function project(root, env = process.env) {
  let real;
  try {
    real = fs.realpathSync(path.resolve(root));
  } catch {
    throw new GraphError("Project folder does not exist.");
  }
  const gitRoot = fs.realpathSync(
    git(real, ["rev-parse", "--show-toplevel"]).trim(),
  );
  if (real !== gitRoot) real = gitRoot;
  const user = env.USERPROFILE || env.HOME || os.homedir();
  const base = path.join(user, ".contextshrink", "g");
  const id = hash(
    process.platform === "win32" ? real.toLowerCase() : real,
  ).slice(0, 16);
  const dir = path.join(base, id);
  return {
    root: real,
    id,
    base,
    dir,
    mirror: path.join(dir, "r"),
    cache: path.join(dir, "c"),
    state: path.join(dir, "state.json"),
    lock: path.join(dir, "lock"),
  };
}
function platformAsset(platform = process.platform, arch = process.arch) {
  const key =
    platform === "win32" && arch === "x64"
      ? "windows-x86_64"
      : platform === "darwin"
        ? arch === "arm64"
          ? "darwin-aarch64"
          : arch === "x64"
            ? "darwin-x86_64"
            : null
        : platform === "linux"
          ? arch === "arm64"
            ? "linux-aarch64-gnu"
            : arch === "x64"
              ? "linux-x86_64-gnu"
              : null
          : null;
  if (!key || !manifest.assets[key])
    throw new GraphError(
      "No pinned graph engine is available for this platform. Windows x64 is the tested platform.",
    );
  return manifest.assets[key];
}
function engineLocation(p) {
  return path.join(
    p.base,
    "engine-" + manifest.version,
    process.platform === "win32"
      ? "codebase-memory-mcp.exe"
      : "codebase-memory-mcp",
  );
}
async function installEngine(p, options = {}) {
  if (options.binary) return options.binary;
  const binary = engineLocation(p),
    record = json(binary + ".json");
  function notices() {
    for (const name of [
      "graph-engine-LICENSE.txt",
      "graph-engine-THIRD_PARTY_NOTICES.md",
    ])
      atomic(
        path.join(path.dirname(binary), name),
        fs.readFileSync(path.join(__dirname, name)),
      );
  }
  if (fs.existsSync(binary)) {
    if (
      !record ||
      record.version !== manifest.version ||
      record.sha256 !== hash(fs.readFileSync(binary))
    )
      throw new GraphError(
        "Graph engine verification failed. The existing file was not executed.",
      );
    notices();
    return binary;
  }
  const asset = platformAsset();
  fs.mkdirSync(path.dirname(binary), { recursive: true, mode: 0o700 });
  const archive = path.join(
    path.dirname(binary),
    "download-" + crypto.randomBytes(5).toString("hex"),
  );
  const unpack = archive + "-unpack";
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 90000);
  try {
    const response = await fetch(asset.url, { signal: controller.signal });
    const final = new URL(response.url);
    if (
      !response.ok ||
      final.protocol !== "https:" ||
      !(
        final.hostname === "github.com" ||
        final.hostname.endsWith(".githubusercontent.com")
      )
    )
      throw new GraphError(
        "Could not download the pinned graph engine from GitHub.",
      );
    const chunks = [];
    let bytes = 0;
    for await (const chunk of response.body) {
      bytes += chunk.length;
      if (bytes > 100 * 1024 * 1024)
        throw new GraphError("Graph engine download exceeded the size limit.");
      chunks.push(chunk);
    }
    const data = Buffer.concat(chunks);
    if (hash(data) !== asset.sha256)
      throw new GraphError(
        "Graph engine archive checksum did not match. Nothing was installed.",
      );
    fs.writeFileSync(archive, data, { mode: 0o600 });
    fs.mkdirSync(unpack, { mode: 0o700 });
    const tar =
      process.platform === "win32"
        ? path.join(
            process.env.SystemRoot || "C:\\Windows",
            "System32",
            "tar.exe",
          )
        : "tar";
    const result = spawnSync(
      tar,
      ["-xf", archive, "-C", unpack, asset.member],
      { windowsHide: true, timeout: 30000, maxBuffer: 1024 * 1024 },
    );
    const extracted = path.join(unpack, asset.member);
    if (
      result.error ||
      result.status !== 0 ||
      !fs.existsSync(extracted) ||
      fs.lstatSync(extracted).isSymbolicLink()
    )
      throw new GraphError(
        "Could not extract the verified graph engine. A working tar command is required.",
      );
    fs.chmodSync(extracted, 0o700);
    fs.renameSync(extracted, binary);
    atomic(
      binary + ".json",
      JSON.stringify({
        version: manifest.version,
        sha256: hash(fs.readFileSync(binary)),
        archiveSha256: asset.sha256,
      }),
    );
    notices();
    return binary;
  } catch (e) {
    if (e instanceof GraphError) throw e;
    throw new GraphError(
      "Could not install the graph engine. Check network access and filesystem permissions.",
    );
  } finally {
    clearTimeout(timeout);
    if (fs.existsSync(archive)) fs.unlinkSync(archive);
    if (fs.existsSync(unpack)) {
      const resolved = path.resolve(unpack),
        parent = path.resolve(path.dirname(binary));
      if (
        !resolved.startsWith(parent + path.sep) ||
        !/^download-[a-f0-9]+-unpack$/.test(path.basename(resolved))
      )
        throw new GraphError("Refused to clean an unexpected extraction path.");
      if (fs.lstatSync(resolved).isSymbolicLink()) fs.unlinkSync(resolved);
      else fs.rmSync(resolved, { recursive: true, force: true });
    }
  }
}
function engineEnv(p) {
  const inherited = Object.fromEntries(
    Object.entries(process.env).filter(([k]) =>
      [
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "SYSTEMDRIVE",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
      ].includes(k.toUpperCase()),
    ),
  );
  return {
    ...inherited,
    HOME: p.dir,
    USERPROFILE: p.dir,
    APPDATA: p.dir,
    LOCALAPPDATA: p.dir,
    XDG_CACHE_HOME: p.cache,
    XDG_CONFIG_HOME: p.cache,
    XDG_DATA_HOME: p.cache,
    CBM_CACHE_DIR: p.cache,
    CBM_LOG_LEVEL: "error",
  };
}
function runEngine(p, binary, tool, args) {
  const r = spawnSync(binary, ["cli", tool, JSON.stringify(args)], {
    cwd: p.mirror,
    env: engineEnv(p),
    windowsHide: true,
    encoding: "utf8",
    timeout: 120000,
    maxBuffer: 12 * 1024 * 1024,
  });
  if (r.error || r.status !== 0)
    throw new GraphError(
      tool === "trace_path"
        ? "Trace could not resolve this symbol. Prefer an exact function or method; class/module tracing is not guaranteed. Use source search for missing or ambiguous symbols rather than repeated discovery or refreshes."
        : "The local graph engine could not complete " +
        tool +
        ". Use graph refresh to retry.",
    );
  let data;
  try {
    data = JSON.parse(r.stdout);
  } catch {
    throw new GraphError(
      "The local graph engine returned an unreadable response.",
    );
  }
  if (data.error || data.status === "error")
    throw new GraphError(
      tool === "trace_path"
        ? "Trace could not resolve this symbol. Prefer an exact function or method; use source search for missing or ambiguous symbols rather than repeated discovery or refreshes."
        : "The local graph engine rejected " +
        tool +
        ". Check the query or refresh the index.",
    );
  return data;
}
function snapshot(p) {
  const paths = [
    ...new Set(
      git(p.root, [
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
      ])
        .split("\0")
        .filter(Boolean),
    ),
  ].sort();
  if (paths.length > 60000)
    throw new GraphError(
      "This preview supports repositories with up to 60,000 Git-visible files.",
    );
  const files = [];
  const skipped = {
    filtered: 0,
    large: 0,
    links: 0,
    longPaths: 0,
    unreadable: 0,
  };
  for (const relative of paths) {
    const name = relative.replaceAll("\\", "/");
    if (
      name.startsWith("/") ||
      name.split("/").includes("..") ||
      EXCLUDED_DIR.test(name) ||
      SENSITIVE.test(name) ||
      !EXTENSIONS.has(path.extname(name).toLowerCase())
    ) {
      skipped.filtered++;
      continue;
    }
    const source = path.join(p.root, relative);
    try {
      const stat = fs.lstatSync(source, { bigint: true });
      if (!stat.isFile() || stat.isSymbolicLink()) {
        skipped.links++;
        continue;
      }
      const resolved = fs.realpathSync(source);
      if (
        path.relative(p.root, resolved).startsWith("..") ||
        path.isAbsolute(path.relative(p.root, resolved))
      ) {
        skipped.links++;
        continue;
      }
      if (stat.size > 2n * 1024n * 1024n) {
        skipped.large++;
        continue;
      }
      if (
        process.platform === "win32" &&
        path.join(p.mirror, relative).length >= 240
      ) {
        skipped.longPaths++;
        continue;
      }
      files.push({
        path: name,
        size: String(stat.size),
        mtime: String(stat.mtimeNs),
      });
    } catch {
      skipped.unreadable++;
    }
  }
  const commit = spawnSync(
    "git",
    [
      "-c",
      "safe.directory=" + p.root.replaceAll("\\", "/"),
      "rev-parse",
      "--verify",
      "HEAD",
    ],
    { cwd: p.root, encoding: "utf8", windowsHide: true, timeout: 10000 },
  );
  const head = commit.status === 0 ? commit.stdout.trim() : "unborn";
  return {
    head,
    files,
    skipped,
    fingerprint: hash(JSON.stringify({ head, files })),
  };
}
async function withLock(p, fn) {
  fs.mkdirSync(p.dir, { recursive: true, mode: 0o700 });
  let fd;
  try {
    fd = fs.openSync(p.lock, "wx", 0o600);
  } catch (e) {
    throw new GraphError(
      e.code === "EEXIST"
        ? "Another graph operation is running, or its lock remains. No second index was started."
        : "Could not write the local graph folder. Check user-profile permissions.",
    );
  }
  try {
    return await fn();
  } finally {
    fs.closeSync(fd);
    fs.unlinkSync(p.lock);
  }
}
function syncMirror(p, files) {
  fs.mkdirSync(p.mirror, { recursive: true, mode: 0o700 });
  fs.mkdirSync(p.cache, { recursive: true, mode: 0o700 });
  if (!fs.existsSync(path.join(p.mirror, ".git")))
    git(p.mirror, ["init", "-q"]);
  const wanted = new Set(files.map((f) => f.path));
  const previous = json(path.join(p.dir, "mirror.json"));
  const oldFiles = new Map((previous?.files || []).map((f) => [f.path, f]));
  for (const old of previous?.files || []) {
    if (!wanted.has(old.path)) {
      const target = path.resolve(p.mirror, old.path);
      if (!target.startsWith(p.mirror + path.sep))
        throw new GraphError("Invalid graph mirror state.");
      if (fs.existsSync(target)) fs.unlinkSync(target);
    }
  }
  for (const file of files) {
    const prior = oldFiles.get(file.path);
    const target = path.join(p.mirror, file.path);
    if (
      prior?.mtime === file.mtime &&
      prior?.size === file.size &&
      fs.existsSync(target)
    )
      continue;
    const source = path.join(p.root, file.path);
    const contents = fs.readFileSync(source);
    if (contents.includes(0)) {
      if (fs.existsSync(target)) fs.unlinkSync(target);
      continue;
    }
    fs.mkdirSync(path.dirname(target), { recursive: true, mode: 0o700 });
    atomic(target, contents);
  }
  atomic(path.join(p.dir, "mirror.json"), JSON.stringify({ files }));
}
async function refresh(root, env = process.env, options = {}) {
  const p = project(root, env);
  return withLock(p, async () => {
    const binary = await installEngine(p, options);
    for (let attempt = 0; attempt < 2; attempt++) {
      const before = snapshot(p);
      if (!before.files.length)
        throw new GraphError(
          "No supported source files were found. Git-ignored, generated, and sensitive files are excluded.",
        );
      syncMirror(p, before.files);
      const indexed = runEngine(p, binary, "index_repository", {
        repo_path: p.mirror,
        mode: "fast",
        persistence: false,
      });
      if (snapshot(p).fingerprint !== before.fingerprint) continue;
      const state = {
        version: 1,
        root: p.root,
        project: indexed.project,
        engineVersion: manifest.version,
        indexedAt: new Date().toISOString(),
        ...before,
        nodes: indexed.nodes,
        edges: indexed.edges,
      };
      atomic(p.state, JSON.stringify(state));
      return {
        project: p.root,
        indexedAt: state.indexedAt,
        nodes: state.nodes,
        edges: state.edges,
        indexedFiles: state.files.length,
        skipped: state.skipped,
        fresh: true,
      };
    }
    throw new GraphError(
      "Files changed while indexing. The graph was not marked fresh. Retry graph refresh.",
    );
  });
}
function status(root, env = process.env) {
  const p = project(root, env),
    state = json(p.state);
  return {
    project: p.root,
    indexed: !!state,
    fresh: !!state && state.fingerprint === snapshot(p).fingerprint,
    indexedAt: state?.indexedAt,
    nodes: state?.nodes,
    edges: state?.edges,
    indexedFiles: state?.files?.length,
    skipped: state?.skipped,
  };
}
function remap(value, p) {
  if (typeof value === "string")
    return value
      .replaceAll(p.mirror.replaceAll("\\", "/"), p.root.replaceAll("\\", "/"))
      .replaceAll(p.mirror, p.root);
  if (Array.isArray(value)) return value.map((v) => remap(v, p));
  if (value && typeof value === "object")
    return Object.fromEntries(
      Object.entries(value).map(([k, v]) => [k, remap(v, p)]),
    );
  return value;
}
function compactSearch(data) {
  if (!Array.isArray(data?.results)) return data;
  return {
    ...data,
    // Ranking is represented by result order. Keep every citation and any
    // other engine metadata, but omit noisy internal identifiers and scores.
    results: data.results.map((node) =>
      node && typeof node === "object" && !Array.isArray(node)
        ? Object.fromEntries(Object.entries(node).filter(([key]) => key !== "qualified_name" && key !== "rank"))
        : node,
    ),
  };
}
function bounded(data, budget = policy.DEFAULT_BUDGET) {
  if (!Number.isInteger(budget) || budget < 1000 || budget > 16000)
    throw new GraphError("budget_bytes must be between 1000 and 16000.");
  const result = {
    ...structuredClone(data),
    budgetBytes: budget,
    truncated: !!data.truncated,
  };
  while (Buffer.byteLength(JSON.stringify(result)) > budget) {
    let biggest = null;
    function visit(v, parent, key) {
      if (Array.isArray(v) && v.length) {
        const size = Buffer.byteLength(JSON.stringify(v));
        if (!biggest || size > biggest.size) biggest = { v, parent, key, size };
      }
      if (v && typeof v === "object")
        for (const [k, x] of Object.entries(v)) visit(x, v, k);
    }
    visit(result, null, null);
    if (biggest) {
      biggest.v.pop();
      result.truncated = true;
      continue;
    }
    return {
      project: result.project,
      fresh: result.fresh,
      truncated: true,
      budgetBytes: budget,
      hint: "Result exceeds the byte budget. Narrow the query or request a targeted source snippet.",
    };
  }
  return result;
}
async function query(root, kind, args = {}, env = process.env, options = {}) {
  const p = project(root, env);
  if (!json(p.state))
    throw new GraphError(
      "No graph exists for this codebase. Run graph init first.",
    );
  if (!status(root, env).fresh) await refresh(root, env, options);
  return withLock(p, async () => {
    const state = json(p.state);
    if (state.fingerprint !== snapshot(p).fingerprint)
      throw new GraphError(
        "Code changed before the graph query. Retry to refresh it.",
      );
    const binary = await installEngine(p, options),
      a = { project: state.project };
    let tool;
    if (kind === "search") {
      if (
        typeof args.query !== "string" ||
        !args.query.trim() ||
        args.query.length > 500
      )
        throw new GraphError(
          "Search needs a nonempty query under 500 characters.",
        );
      tool = "search_graph";
      a.query = args.query;
      a.limit = Number.isInteger(args.limit)
        ? Math.max(1, Math.min(30, args.limit))
        : policy.DEFAULT_LIMIT;
      if (args.file_pattern) a.file_pattern = args.file_pattern;
    } else if (kind === "overview") {
      tool = "get_architecture";
      a.aspects = ["overview", "packages", "hotspots"];
    } else if (kind === "trace") {
      tool = "trace_path";
      if (
        typeof args.symbol !== "string" ||
        !args.symbol ||
        args.symbol.length > 500
      )
        throw new GraphError("Trace needs an exact symbol name.");
      a.function_name = args.symbol;
      a.direction = "both";
      a.depth = 2;
    } else if (kind === "query") {
      tool = "query_graph";
      const q = args.query;
      if (
        typeof q !== "string" ||
        q.length > 3000 ||
        !/^\s*MATCH\b/i.test(q) ||
        /\b(?:CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|CALL|LOAD|INSERT|UPDATE)\b/i.test(
          q,
        ) ||
        /;/.test(q)
      )
        throw new GraphError(
          "Only bounded, read-only MATCH queries are allowed.",
        );
      const syntax = q.replace(
        /'(?:\\.|''|[^'\\])*'|"(?:\\.|""|[^"\\])*"|`[^`]*`/g,
        "''",
      );
      const limits = [...syntax.matchAll(/\bLIMIT\b/gi)];
      const trailing = syntax.match(/\bLIMIT\s+(\d+)\s*$/i);
      if (
        limits.length &&
        (limits.length !== 1 || !trailing || +trailing[1] > 50)
      )
        throw new GraphError(
          "Graph query LIMIT must be a final integer of at most 50.",
        );
      a.query = limits.length ? q : q + " LIMIT 20";
    } else
      throw new GraphError(
        "Unknown graph query. Use search, overview, trace, or query.",
      );
    const raw = remap(runEngine(p, binary, tool, a), p);
    const data = kind === "search" ? compactSearch(raw) : raw;
    if (snapshot(p).fingerprint !== state.fingerprint)
      throw new GraphError(
        "Code changed during the query. Retry for a fresh graph result.",
      );
    return bounded(
      {
        project: p.root,
        fresh: true,
        indexedAt: state.indexedAt,
        kind,
        source: "local static code graph; verify source before editing",
        data,
      },
      args.budget_bytes,
    );
  });
}
module.exports = {
  GraphError,
  project,
  platformAsset,
  installEngine,
  engineLocation,
  engineEnv,
  runEngine,
  snapshot,
  refresh,
  status,
  query,
  bounded,
  compactSearch,
  atomic,
  json,
  hash,
  withLock,
};
