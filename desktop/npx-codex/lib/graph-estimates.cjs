"use strict";
// Counterfactual discovery estimates, never provider-reported usage.
const fs = require("node:fs");
const path = require("node:path");
const core = require("./graph-core.cjs");
const policy = require("./graph-policy.cjs");
let encoder;
function count(text) {
  if (!encoder) {
    const { Tiktoken } = require("js-tiktoken/lite");
    encoder = new Tiktoken(require("js-tiktoken/ranks/o200k_base"));
  }
  return encoder.encode(text, [], []).length;
}
const CAP = 4000;
const OVERHEAD = 1024;
function baseline(p, query) {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length || words.some((w) => w.length < 3)) return null;
  const state = core.json(p.state);
  let text = "",
    scanned = 0;
  for (const file of state.files.slice(0, 10000)) {
    const source = path.join(p.mirror, file.path);
    if (!fs.existsSync(source)) continue;
    const content = fs.readFileSync(source, "utf8");
    scanned += Buffer.byteLength(content);
    if (scanned > 64 * 1024 * 1024) break;
    if (content.includes("\0")) continue;
    const lines = content.split(/\r?\n/);
    for (let i = 0; i < lines.length; i++) {
      const lower = lines[i].toLowerCase();
      if (!words.some((w) => lower.includes(w))) continue;
      text = (text + `${file.path}:${i + 1}:${lines[i]}\n`).slice(0, 16000);
      if (text.length >= 16000 || count(text) >= CAP) {
        return {
          tokens: Math.min(CAP, count(text)),
          digest: core.hash(text),
          snapshot: state.fingerprint,
        };
      }
    }
  }
  return {
    tokens: Math.min(CAP, count(text)),
    digest: core.hash(text),
    snapshot: state.fingerprint,
  };
}
function attach(root, args, value, env = process.env) {
  // A bounded repository-wide alternative is defensible only for explicit
  // discovery. File-scoped lookups need their own baseline; give no credit.
  if (args.intent !== "discovery" || args.file_pattern) return value;
  if (!value.data?.results?.length) return value;
  const p = core.project(root, env);
  if (value.indexedAt && value.indexedAt !== core.json(p.state)?.indexedAt)
    return value;
  const alternative = baseline(p, args.query);
  if (
    !alternative ||
    !alternative.tokens ||
    !core.status(root, env).fresh ||
    core.json(p.state).fingerprint !== alternative.snapshot
  )
    return value;
  const receipt = {
    v: 1,
    id: core.hash(
      JSON.stringify([p.id, alternative.snapshot, alternative.digest]),
    ),
    project: p.id,
    snapshot: alternative.snapshot,
    baseline_tokens: alternative.tokens,
    overhead_tokens: OVERHEAD,
    tokenizer: "o200k_base",
    basis: "bounded-literal-search-v1",
  };
  const result = { ...value, contextshrink_graph_estimate: receipt };
  return Buffer.byteLength(JSON.stringify(result)) <=
    (args.budget_bytes || policy.DEFAULT_BUDGET)
    ? result
    : value;
}
module.exports = { attach, count, baseline, CAP, OVERHEAD };
