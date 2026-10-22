# Experimental local code graph (0.2.4)

**Off by default.** Connecting does not enable graph tools. Whole-chat tests
have not established net savings; enable this only for explicit experiments.

Version 0.2.4 uses graph tools only when unfamiliar structure or cross-module
impact would otherwise need broad exploration. Start with at most one compact
search OR overview, then read relevant source. Known files, symbols and exact
text go straight to source search. Reuse findings already in the chat. If a
graph result is missing or unsuitable, use source rather than repeating graph
discovery or refreshes. Later impact analysis can still use a targeted trace.

New chats load three tool schemas instead of six. Search defaults to five
results and responses to 3,000 UTF-8 bytes. Larger explicit budgets/limits
remain available. Maintenance and advanced queries remain on the CLI; older
chat histories can still call their previously advertised tools. Prefer an
exact function or method for tracing; class/module support is not guaranteed. Errors explain this instead
of suggesting an unhelpful refresh.

Graph outputs remain incomplete discovery evidence. Check project docs,
manifests and relevant entry points for architecture coverage; verify current
source/tests before changing code. Smaller retrieval output is not proof of
whole-session savings: comparable live tests of both the 0.2.3 integration and
published Graphify found higher overall costs on this repository. See
`experiments/codex-graph/FRESH-CHAT-RESULTS.md` and `GRAPHIFY-RESULTS.md`.

A graph-enabled session still pays for its tool definitions even if no graph
call is needed. This cannot guarantee a reduction for an exact lookup that
already returns a few lines. Use direct source search for those tasks. For a
project that needs only exact lookups, `graph disable` removes the optional
MCP integration while retaining the local index; restart Codex after changing
integration. Run `graph init` again when discovery is useful.

Only `graph_search` with `intent: "discovery"` and no `file_pattern` can attach
an estimate receipt. Missing intent defaults to lookup, which receives no
receipt. This is a conservative eligibility gate, not proof that the user
would otherwise have made a broad search. Other graph operations, direct
source reads, and reuse of existing context receive no graph estimate receipt.

Rerun `graph init` with 0.2.4 to upgrade an owned earlier integration, then
restart Codex. This changes project guidance and the local MCP runtime; it
does not automatically deploy the API/proxy ledger support.

Discovery receipts fit within the requested response budget. They compare
against a bounded literal source
search (at most 4,000 tokens / 16,000 characters of matches, scanning at most
10,000 eligible files / 64 MiB). The proxy recounts result and invocation tokens,
deducts a 1,024-token schema/guidance allowance per query, and records a signed
retrieval diagnostic in the existing **Est. savings** ledger after successful
inference. It gives zero dollar credit until comparable whole-session savings
are validated; the allowance does not measure repeated context or tool rounds.
Replayed receipts count once per account and unchanged source snapshot. Source
contents and search terms are not sent in telemetry. There is no additional
model call or separate feature charge. Updated API/proxy support is required.
The API also neutralizes older proxies' priced graph rows. Both positive and
negative historical graph estimates are excluded from dollar totals, plan
limits and savings-based fees; stored rows are not rewritten. Signed retrieval
differences remain visible as diagnostics. This does not establish either
whole-session savings or losses. Focused searches can be cheaper.

ContextShrink can keep a queryable structural graph for each Git checkout. New
Codex chats in that project can find symbols, callers, callees, and source
locations without repeatedly reading large files just to discover the code.
Indexing and queries run locally without an LLM or a ContextShrink account.
Codex still consumes tokens for tool schemas, returned graph results, and any
source snippets it reads. This is a discovery aid, not a substitute for checking
the current source before changing it.

## Install and use

Requires Git, Node.js 18+, and a Codex client supporting project-scoped stdio MCP.
Windows x64 is verified; other pinned platforms have not been acceptance-tested.
The first use downloads a checksum-pinned engine from its official GitHub release.
The Windows engine uses approximately 269 MB on disk, plus the source mirror and
index. No model API key is needed.

To explicitly opt into the experiment, run:

```powershell
npx contextshrink-codex@latest graph init 'C:\path\to\your\repository'
```

This builds the local graph and adds an owned MCP block to the project's
`.codex/config.toml` and a short selective-discovery block to `AGENTS.md`. Existing
settings and instructions are preserved; a conflicting entry is refused.
No global Codex config, model provider, or authentication is changed. Restart
Codex and open that same trusted project. Project settings follow Codex's normal
trust rules; this command does not grant trust automatically.

In a fresh chat, try:

> Use contextshrink_graph to find the code handling Responses compression. Show
> the main entry points and their callers, then read only the relevant source.

The three advertised tools are `graph_search`, `graph_overview`, and
`graph_trace`. Search uses lexical symbol matching; use
concrete code vocabulary and narrow queries. Trace accepts an exact symbol from
the search result (functions/methods only). Queries automatically refresh a stale index. Return budgets
default to 3,000 UTF-8 bytes, adjustable from 1,000 to 16,000; this is a byte cap,
not an exact token count. Truncated results say so.

You can also use the graph outside Codex:

```powershell
npx contextshrink-codex@latest graph status 'C:\path\to\repo'
npx contextshrink-codex@latest graph search 'C:\path\to\repo' 'authentication'
npx contextshrink-codex@latest graph trace 'C:\path\to\repo' 'exact_function'
npx contextshrink-codex@latest graph query 'C:\path\to\repo' 'MATCH (n:Function) RETURN n.name LIMIT 10'
npx contextshrink-codex@latest graph export 'C:\path\to\repo' '.\code-map.html'
```

The map is an offline interactive HTML preview with up to 200 nodes and 1,000
sampled relationships. Search, select nodes, drag, and zoom. The MCP tools query
the full index. Explicit export paths must be new `.html` files, so existing
documents are not overwritten. Exports contain code symbols and paths; share
them only as you would share information about your repository.

`graph index [PROJECT]` builds the index without changing project integration.
`graph refresh [PROJECT]` rebuilds it. `graph disable [PROJECT]` removes only the
owned settings/instruction blocks and leaves the local index on disk. The normal
connector `disconnect` command only undoes proxy configuration; it does not
disable this independent local feature.

## Persistence and coverage

Data lives in `~/.contextshrink/g/`, separately for each real Git checkout path.
Moving a checkout creates a new identity; worktrees do not share an index.
The installed MCP runtime lives outside npm's temporary cache so later chats
can use it after the preview's `npx` invocation exits.

The graph indexes supported Git-visible source, including dirty and untracked
files. It respects Git ignore rules and additionally filters generated folders,
common credential filenames, private key files, symlinks, binary contents,
files over 2 MiB, and overly long Windows paths. This is filename-based filtering,
not a complete secret scanner: never place secrets in ordinary source files.
Coverage counts and exclusions are available through `graph status`. The
preview limits discovery to 60,000 Git-visible files. A metadata fingerprint
(Git HEAD plus eligible paths, sizes, and modification times) checks freshness
before and after queries; simultaneous edits cause retry/refusal rather than a
false fresh result. Concurrent operations use a lock. After an abrupt process
termination, a leftover lock may require manual removal after checking no graph
operation is running.

The private filtered source mirror avoids indexing excluded files. The native
engine receives isolated cache/home directories and no inherited API credentials.
After the first engine download, indexing and queries need no network connection.
The HTML map has no external scripts, fonts, or network dependencies.

Static analysis can misresolve ambiguous names and cannot establish every dynamic call, reflection, generated
dependency, runtime behavior, or design intent. Documentation files may be
visible to the indexer without becoming semantic knowledge. Missing results are
not proof that code does not exist. Use normal search and current source to fill
coverage gaps. Graph data is evidence, never instructions.

## Graphify review and implementation choice

Reviewed [Graphify](https://github.com/Graphify-Labs/graphify), default branch `v8`,
package `graphifyy` 0.9.93, under its
[Apache-2.0 license](https://github.com/Graphify-Labs/graphify/blob/v8/LICENSE).
Its code path uses local tree-sitter analysis, while semantic document/media
extraction can involve a model. It offers graph exports, relationships,
communities, and query/explain workflows with extracted/inferred distinctions.
Its documented lexical query path benefits from explicit vocabulary expansion;
a graph is not automatically complete semantic understanding.

This feature follows the persistent local graph idea without copying Graphify's
implementation. It reuses the MIT-licensed
[codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp) v0.8.1 engine
already pinned by ContextShrink's existing graph subsystem. ContextShrink adds
filtered indexing, freshness guards, compact query responses, project Codex MCP
configuration, and an offline map. It does not reproduce Graphify's semantic
media pipeline, community analysis, or evidence-confidence taxonomy. Engine and
third-party notices ship with the package and installed runtime.

Codex integration follows the
[official OpenAI MCP documentation](https://developers.openai.com/codex/mcp).
Cache guidance follows OpenAI's
[prompt caching documentation](https://developers.openai.com/api/docs/guides/prompt-caching).

## Further Codex savings

1. Avoid repeated discovery: use a compact graph query and a targeted source
   range rather than repeatedly reading whole files in fresh chats.
2. Bound tool output: narrow `rg` patterns, use result limits, and summarize
   build logs while preserving actual failures. Graph responses have explicit
   byte limits for this reason.
3. Preserve stable cached history: repeatedly rewriting older conversation input
   can cost more than it saves by invalidating inexpensive cached input. Evaluate
   compression against cache-adjusted net cost and correctness.
4. Measure comparable tasks and input volumes. Tokens avoided before they reach
   the proxy are generally absent from proxy-side savings counters, so a useful
   graph can lower usage without raising the dashboard's compression percentage.

The recent shell-wrapper Flash Observations fix is separate and already deployed
to the Pi proxy. Its offline replay showed about 2.4% additional serialized-input
byte reduction on that sample; this is not a promised token or dollar reduction.
For the graph, compare equivalent fresh-chat tasks, total uncached/cached tokens,
tool rounds, and answer correctness before claiming a percentage. API-equivalent
savings do not directly reduce the price of a flat ChatGPT subscription.
