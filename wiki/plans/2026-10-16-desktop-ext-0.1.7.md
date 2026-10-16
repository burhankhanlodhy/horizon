# Desktop extension 0.1.7: compact once, answer from context

## Why

In the session test (2026-10-16, 0.1.6), both chats got 12/12, and the costs
were C_normal $0.87 and B $0.91. The per-question numbers suggest how the money
was spent; nothing was measured call by call:

- **C (files attached):** one model call per question. Each call re-reads the
  cached attachments, roughly 105k tokens, at the cache-read rate, which comes
  to about $0.07 per question.
- **B (`find`):** two calls per question, the tool call and then the answer.
  Each re-reads Desktop's instructions, and Opus thinks at both steps. The
  smaller documents saved about as much as the extra call and thinking cost.

To beat the default, B must answer with **one call per question** while carrying
**much smaller copies** of the files. Claude loads a lossless compact copy of a
file once, then answers later questions from context, like an attachment but
smaller. `find` stays the tool for a single quick lookup.

## W1: lossless table encoding for JSON (agent A)

New module `horizon/desktop_mcp/json_table.py`.

It holds two functions:

- `encode(value) -> str`, a compact text form of a parsed JSON value;
- `decode(text) -> value`, its exact inverse.

The encoding follows TOON (Token-Oriented Object Notation), a published format
meant for LLM input. A language model is more likely to read a documented format
correctly than an invented one.

Rules:

- **Objects** are written as `key: value` lines, indented two spaces per level.
- **Arrays of objects whose keys are all identical and in the same order** are
  written as a table:
  - a header `name[N]{col1,col2,...}:`, then one row per element, values
    separated by `,`;
  - a column whose values are themselves uniform object arrays may be written
    as a nested table, or the row may fall back to a nested block. Pick one rule
    and document it in the decoding hint.
- **Arrays of scalars** are written inline as `name[N]: a,b,c`.
- **Any other array** is written as list items.
- **Strings are quoted whenever leaving them bare would be ambiguous.** That
  covers a string that looks like a number, boolean or null, is empty, has
  leading or trailing spaces, or contains the delimiter, a colon, a quote, a
  backslash, a bracket, a brace or a newline. JSON escapes are used inside the
  quotes.
- **Numbers keep their original spelling.** Parse with `Decimal`, as the
  existing `_parse_json` does, and emit the source lexeme where possible.
  Equality is checked through the same parser.

Integration in `compression.py`:

- `compact_json` tries the table encoding first.
- It is applied only when **both** of these hold:
  - `decode(encode(parsed)) == parsed`, with types preserved (string "127.63"
    against number 127.63, true against "true", and so on);
  - the result is at least 15% smaller in UTF-8 bytes than the whitespace
    minification.
- Otherwise the current `json_whitespace` transform is used.
- The new transform is `json_table`, with fidelity `normalized_json_table`.
  Add it to the fidelity literal and the measurement and model-view
  allowlists. Grep for `normalized_json_whitespace` to find every place.
- `_DECODING_HINTS["json_table"]` is a short and exact description of the
  format, written so that Claude can read values without guessing. A key that
  collides with the syntax is quoted.

Tests:

- round trips on the session dataset's `orders_export.json` and
  `platform_config.json`;
- hand-written edge cases: number-like strings, `true`/`"true"`, null, empty
  strings and containers, unicode, newlines in strings, keys with `:` or `,`,
  non-uniform arrays, deep nesting, big decimals and exponents, duplicate keys
  (rejected as today);
- a property-style randomized round trip of at least 500 generated values,
  with a fixed seed;
- the size reduction on the dataset files.

Report bytes and o200k tokens (tiktoken is installed) before and after for:

- the session dataset's `orders_export.json` and `platform_config.json`;
- **two realistic shapes not made by us:** a GitHub-API-like issues list (nested
  user objects, labels arrays, null fields, long body strings), and a
  package-lock-like nested dependency object. Write them as test fixtures.

## W2: lossless log line templates (agent B)

New module `horizon/desktop_mcp/log_templates.py`, with transform
`log_templates` and fidelity `reversible_fold`.

The encoding:

- For line-oriented logs where most lines begin with a timestamp, the text after
  the timestamp and its single following space is the line's "rest".
- Any rest that occurs at least 3 times becomes a legend entry. The legend sits
  at the top and is written as `@k = <rest>`, numbered by first occurrence.
- A line whose rest is in the legend is written as `<timestamp> @k`. Every
  other line is unchanged.
- **Timestamps are always written in full, exactly as in the source**, so exact
  answers such as "the timestamp as written" stay safe.
- The decoding hint explains how to expand it.

It must reproduce the source **byte for byte**, including the final newline,
CRLF line endings and lines that already look like `@k` or a legend line. When a
source collides with the syntax, do not apply the transform. Apply it only when
the result is at least 15% smaller.

Integration:

- `infer_compaction_kind` maps `.txt` to `log` when at least 80% of its
  non-blank lines start with an ISO-8601 or common log timestamp (choose a
  strict regex and document it). Otherwise `.txt` stays `text`.
- For `log`, try the existing `log_runs` first, then `log_templates` on the
  result, or alone if that gives the smaller verified result. Keep it simple:
  one transform per result is fine. Pick the smaller verified candidate.

Tests:

- a byte-exact round trip on the session dataset's `service_events.txt`;
- CRLF, a missing final newline, and collisions (source lines containing ` @1`
  or starting with `@1 = `);
- mixed lines without a timestamp;
- a realistic nginx-access-like log fixture and a JSON-lines app log fixture,
  since `.jsonl` logs keep their existing behaviour, so just assert they are
  not broken;
- the size reduction.

Report bytes and o200k tokens before and after.

## W3: guidance, version and measurement (agent C)

- **`contextshrink_open` description.** Add one short sentence: "When the user
  will ask several questions about a file, open it once in compact mode and
  answer later questions from that result; use find for a single lookup."
  - Keep the tool listing growth within the existing test budget, and report
    the token change.
  - The existing sentence "For each new question, call find again instead of
    re-reading the whole file" contradicts this. Rewrite the two together so
    the guidance is coherent: compact once for several questions about a
    structured file (JSON, logs, config), find for one-off lookups or for prose.
- **The compact result tells Claude what it saved.** It includes
  `compression_ratio` (delivered divided by raw estimated tokens, two decimals)
  so Claude can judge whether keeping the compact copy is worthwhile. Add it to
  the model-view `KEPT` list only when compression was applied.
- **Version:** `VERSION = "0.1.7"`, plus the version tests (in the lifecycle
  cases 0.1.6 is older, and 0.1.7 is not).
- **Measurement script:** `desktop/mcp/self-test/measure_compact.py
  --documents DIR`. For each file it prints raw and compact bytes, the o200k
  tokens and the transform, using the real `LocalBackend` compact open and the
  model view. A test runs it on the session dataset.

## Check before release

- the full `tests/desktop_mcp` suite plus ruff;
- a frozen-build smoke: compact opens of the four session files, with each
  decoded or expanded copy equal to the source;
- a manual read: the coordinator answers the 12 session questions from the
  compact outputs alone;
- then the same two-chat test on the Enterprise laptop.

**Success bar:** B is at least 30% cheaper than C_normal, with 12/12 correct.
Otherwise we report that the extension cannot beat Desktop's attachment
handling on cost.
