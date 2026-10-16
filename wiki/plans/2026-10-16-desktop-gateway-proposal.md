# Proposal: ContextShrink Gateway for Claude Desktop

Status: proposal (2026-10-16). Nothing is built yet.

## The problem with the extension route

An MCP extension only sees what Claude reads through its tools. In a long chat
the bill is mostly re-reading history, which Claude Desktop controls: the
owner's 2.5-hour session was about 85% cache reads. The extension cannot touch
that history, Claude's output, pasted text, or ordinary chatting. Whatever we
do there, the extension's ceiling is "large files read repeatedly".

## The opening: Desktop's third-party gateway mode

Claude Desktop has a supported "3P" deployment mode.

- **Routing.** With `inferenceProvider: "gateway"` and
  `inferenceGatewayBaseUrl`, all inference for Chat, Cowork and Code goes to
  any gateway that implements the Anthropic Messages API.
  - The endpoint is `POST /v1/messages` with streaming and tool use. `GET
    /v1/models` is optional.
  - Configuration is read from managed settings (`HKLM`/`HKCU\SOFTWARE\Policies\Claude`
    on Windows) or the user-writable `%LOCALAPPDATA%\Claude-3p\configLibrary\`.
- **The engine.** In 3P mode, Chat, Cowork and Code run on the agentic engine
  (Claude Code), the same request shape our proxy already handles. Attached
  files are copied to a local uploads folder and read with tools, so attachment
  content arrives as tool results.
- **Trade-offs**, which are Anthropic's enduring differences:
  - billing is token-based through the organization's provider (Anthropic API,
    Bedrock, Vertex or Foundry), not claude.ai seats;
  - conversations stay on the device;
  - there is no claude.ai web or mobile access, no cloud sessions, no sharing
    and no voice.
  - Projects, artifacts, memory (local), uploads, local and remote MCP, skills
    and plugins are all available.
- **The requirement.** The gateway must preserve `cache_control`, and must
  keep the system prompt and tool list stable from one request to the next.

A gateway sees every token of every request. That makes "cut the cost of any
chat" possible in a way the extension never can.

## What makes it different from a generic compressing proxy

Headroom-style proxies compress content but avoid touching cached history,
because breaking the cache costs money. We measured what that leaves on
Claude Code: 0.5–2.5% of the bill, since Read results are protected and the
cached history is untouched. The new idea is to **treat the prompt cache as an
economic system and optimize the total bill**, not tokens.

Rates relative to fresh input: cache read about 0.1×, cache write 1.25× (5
minutes) or 2× (1 hour), output about 5×.

### 1. Compress once, at the door

- Every large block is compressed the first time it enters the conversation:
  attachments read through tools, pasted text, tool and MCP results, long
  logs, JSON and web pages.
- The transform is deterministic, and its output is recorded per session, so
  every later request carries byte-identical compressed text. The cache keeps
  hitting, and each later turn re-reads the small version.
- The value of a removed token is (1.25 or 2) + 0.1 × the number of later
  turns. In a 40-turn chat that is about five times what headroom-style
  savings count.
- **It is reversible (CCR).** The original is stored locally and a retrieve
  tool is injected. When the model calls retrieve, the gateway answers it
  itself (`buffered_ccr_response.py`) and continues the generation, so the
  client never sees the tool.
- Retrieved text is used for that turn only. It never becomes permanent
  history.
- For documents the default is selection, not lossy prose rewriting: an
  outline plus `find`/retrieve, which is the 0.1.4 lesson. Prose compression is
  used only where measured safe.

### 2. Economic context rebasing (the main new idea)

- Claude's own compaction fires only near the context limit, and it is lossy.
  Long before that point, the history is full of dead weight:
  - superseded versions of a file or code block;
  - resolved tool explorations;
  - search results already used;
  - long answers whose essence is now a decision.
- Folding that history earlier breaks the cache once. The gateway decides with
  a cost model:
  - `fold when  Δ_tokens × 0.1 × expected_remaining_turns  >  rewritten_suffix_tokens × write_rate`
  - Example: folding 80k down to 20k costs about 25k write-equivalents and saves
    about 6k read-equivalents per turn, so it breaks even after about 4 turns.
- **Folds are reversible.** Folded spans go to the CCR store, with a
  one-line pointer left in place, so details remain retrievable. This fixes
  the known weakness of lossy summaries.
- **Lossless folds come first:**
  - file contents in `Write`/`Edit` tool calls are on disk, so replace them
    with a pointer;
  - superseded code versions keep only the latest;
  - duplicate pasted content is collapsed;
  - stale `Read` results of files written since are dropped.
- Summaries are a last resort.
- Expected remaining turns are learned per user and per session type from the
  gateway's own logs.

### 3. Fixed-overhead diet

- **Tool definitions.**
  - In 3P, tool search is off by default for gateways. `toolSearchEnabled` can
    turn it on as a configuration change; our memory notes that tool
    definitions were about 20% of the bill in Claude Code.
  - The gateway can also prune connector tools the user never calls, decided
    once at session start so the list stays stable.
- **Housekeeping requests.** Title generation, compaction summaries and
  similar requests are recognised by their shape and routed to a small model.
  The risk is low.
- **Cache keep-alive**, which already exists (`cache_keeper.py`): avoid the
  full re-write after a 5-minute idle when the user is still in the chat.

### 4. Output governor (optional, policy-controlled)

- Output is the most expensive token class. The gateway can cap thinking on
  pure continuation turns (a turn after a tool result in an agent loop) and
  keep full effort on user-facing turns.
- This needs a quality guard, so it is off by default.

### 5. Exact metering

- The gateway reads real `usage` fields (input, cache read, cache write and
  output) on every request. That gives exact cost per chat and per feature, and
  ends manual meter readings.
- It also allows true counterfactual accounting: removed tokens priced at the
  rate they would actually have been billed.

## Expected effect (model, not measured)

| Chat type | Main lever | Rough expectation |
|---|---|---|
| Long chat, no files | Rebasing old turns | Large in long chats; none in short ones |
| Long chat with small files | Compress at the door, rebasing | Moderate to large |
| Large files, repeated questions | Selection plus CCR | Large (the same lesson as 0.1.4/0.1.6) |
| Building code or documents (Cowork/Code) | Lossless folds of Write/Edit and superseded versions, tool diet | Moderate to large in long sessions |
| One short question | Tool diet only | Small. The floor is the system prompt plus output. |

All of these are hypotheses until the gateway's own metering measures them.
Short chats cannot be cut much by any method.

## How to get there

1. **Passthrough plus metering (days).** Run the existing proxy as the 3P
   gateway on the owner's PC with an Anthropic API key. Confirm Chat, Cowork
   and Code work, that `cache_read_input_tokens` is high, and record exact
   usage per session. Desktop's 3P config goes in
   `%LOCALAPPDATA%\Claude-3p\configLibrary\` when no policy is set.
2. **Compress at the door plus CCR for Desktop traffic.** Allow selective
   compression of `Read` results from the uploads folder (documents, not
   code), keep everything byte-stable across turns, and handle retrieve
   inside the gateway.
3. **Lossless rebasing.** Fold Write/Edit payloads, superseded versions,
   duplicates and stale reads, using the cost model.
4. **Tool diet and housekeeping routing.**
5. **Measure.** Run paired real sessions with exact gateway usage. A replay
   harness re-sends recorded early turns through both arms for controlled
   comparisons.

## Risks

- **Quality:** an over-eager fold loses a detail. Mitigations: lossless first,
  CCR pointers, and a quality check in step 5.
- **Cache breakage** from non-deterministic transforms would raise cost. Every
  transform must be byte-stable, and there is a cache-miss watch
  (`cache_miss_watch.py` exists).
- **Adoption:** 3P mode is an organization decision. It changes billing, and
  it gives up web, mobile and cloud sessions. Through a first-party claude.ai
  account there is no gateway hook, so there the MCP extension remains the only
  route, together with the "checkpoint and continue in a fresh chat" idea
  below.
- **Privacy:** the gateway sees all content. It runs locally by default, and
  the compressed store stays on the device.

## For first-party (claude.ai) Desktop, without a gateway

- **Checkpoint and continue.** An extension tool writes a compact, structured
  state of the chat (decisions, open items, current files) into the local
  store. A new chat starts from it, and details stay retrievable through
  `find`.
- Starting fresh resets the history cost, which is the dominant cost in long
  chats. It is a user-driven version of rebasing.
