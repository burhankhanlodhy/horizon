'use strict';
const { parse } = require('smol-toml');
const { isDeepStrictEqual } = require('node:util');
const equivalent = (a, b) => isDeepStrictEqual(structuredClone(a), structuredClone(b));
const ID = 'contextshrink_npx';
const START = '# --- ContextShrink npx Codex provider ---';
const END = '# --- end ContextShrink npx Codex provider ---';
const SELECTOR = `model_provider = "${ID}"`;
class ConfigError extends Error {}
function document(text) {
  try { return parse(text.replace(/^\uFEFF/, '')); }
  catch { throw new ConfigError('config.toml is not valid TOML. It was left unchanged.'); }
}
function layout(text) {
  const doc = document(text);
  if (text.includes(START) || text.includes(END)) throw new ConfigError('A managed provider block already exists without matching state. Run status before changing it.');
  if (doc.model_providers?.[ID]) throw new ConfigError('The ContextShrink provider name is already in use. It was left unchanged.');
  // Keep all user comments and tables verbatim. Only a simple root assignment
  // may be replaced; complex/quoted/dotted forms are refused rather than guessed.
  const lines = text.split(/(?<=\n)/);
  let rootEnd = 0;
  while (rootEnd < lines.length && !/^\s*\[/.test(lines[rootEnd])) rootEnd++;
  const root = lines.slice(0, rootEnd);
  const matches = root.map((line, i) => /^\s*model_provider\s*=/.test(line) ? i : -1).filter(i => i >= 0);
  if (doc.model_provider !== undefined && matches.length !== 1) throw new ConfigError('The model_provider assignment uses an unsupported layout. It was left unchanged.');
  const index = matches[0];
  if (index !== undefined && !/^\s*model_provider\s*=\s*(?:"[^"\r\n]*"|'[^'\r\n]*')\s*(?:#[^\r\n]*)?\r?\n?$/.test(root[index])) {
    throw new ConfigError('The model_provider assignment must be a single-line string. It was left unchanged.');
  }
  return { doc, lines, index };
}
// The provider carries no account key: the local engine adds nothing and holds the
// device key itself. ChatGPT sign-in is kept (requires_openai_auth).
function connectText(original, base) {
  const { doc, lines, index } = layout(original);
  if (doc.model_provider && doc.model_provider !== 'openai') throw new ConfigError('Another model provider already manages Codex. Disconnect it first; no settings were changed.');
  if (doc.openai_base_url || doc.profile || doc.profiles || doc.model_providers?.horizon) {
    throw new ConfigError('Existing proxy or profile routing needs to be resolved first. No settings were changed.');
  }
  const newline = original.includes('\r\n') ? '\r\n' : '\n';
  const originalLine = index === undefined ? null : lines[index];
  const selectorLine = SELECTOR + newline;
  if (index === undefined) lines.unshift(selectorLine);
  else lines[index] = selectorLine;
  const body = lines.join('');
  const separator = body.endsWith('\n') ? newline : newline + newline;
  const block = [START, `[model_providers.${ID}]`, 'name = "ContextShrink"',
    `base_url = ${JSON.stringify(base)}`, 'wire_api = "responses"',
    'requires_openai_auth = true', 'supports_websockets = true',
    'http_headers = { "X-Horizon-Model-Catalog" = "codex" }', END, ''].join(newline);
  const written = body + separator + block;
  const result = document(written);
  const expected = structuredClone(doc);
  expected.model_provider = ID;
  expected.model_providers = { ...expected.model_providers, [ID]: result.model_providers?.[ID] };
  if (result.model_provider !== ID || !equivalent(expected, result)) {
    throw new ConfigError('This TOML layout cannot be changed safely. No settings were changed.');
  }
  return { written, block, separator, selectorLine, originalLine, original };
}
function restore(current, state) {
  const doc = document(current);
  if (current === state.written) return state.original;
  // Preserve unrelated edits but refuse ambiguous edits to our own provider.
  const marker = state.separator + state.block;
  if (current.split(marker).length !== 2 || doc.model_provider !== ID) {
    throw new ConfigError('Codex routing changed since connect. Files and state were kept; restore the managed provider before retrying disconnect.');
  }
  let body = current.replace(marker, '');
  const lines = body.split(/(?<=\n)/);
  const index = lines.findIndex(line => line === state.selectorLine);
  if (index < 0) throw new ConfigError('The managed provider selector changed. Files were kept.');
  if (state.originalLine === null) lines.splice(index, 1);
  else lines[index] = state.originalLine;
  body = lines.join('');
  const restored = document(body);
  const expected = structuredClone(doc);
  delete expected.model_providers[ID];
  const original = document(state.original);
  if (!original.model_providers && Object.keys(expected.model_providers).length === 0) delete expected.model_providers;
  if (original.model_provider === undefined) delete expected.model_provider;
  else expected.model_provider = original.model_provider;
  if (!equivalent(expected, restored)) throw new ConfigError('Config edits cannot be restored safely. Files were kept.');
  return body;
}
module.exports = { ConfigError, document, layout, connectText, restore, ID };
