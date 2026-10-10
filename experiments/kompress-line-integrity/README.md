# Kompress line integrity (headroom #3119): savings lost vs safety gained

Before #3119, Kompress flattened a tool result into one word list, so it could
join words from unrelated lines onto one invented line and crush `ls -l` rows
unevenly. The fix keeps each word on its source line and keeps fixed-width
table rows whole. This measured what that costs and buys before porting it
(2026-10-10, Windows, kompress-v2-base on ONNX, the Pi 5's pinned snapshots).

## Method

- `corpus.py`: 125 tool outputs from public or sample sources only (Python
  stdlib sources and `ls -la`, `git log --stat` of the public Headroom clone,
  `grep -rn`, `pip list`, package READMEs, pytest runs of a throwaway sample
  project). Owner names and local paths are scrubbed; no user code.
- `measure.py`: each build compresses every item as the newest Bash tool
  result (the full `compress()` pipeline), and Kompress alone.
- `analyze.py`: savings per category with a bootstrap interval; output lines
  that are exact, trimmed or invented; fact pairs (file and change count,
  package and version, size and name, grep location and text) whose span on an
  output line still comes from its own source line.

Outputs were identical across repeated and concurrent runs (deterministic).

## Results (full pipeline)

| | Before | With #3119 |
|---|---|---|
| Tokens removed, all 125 items | 14.5% | 11.4% |
| Same, without 4 README files | 12.0% | 11.7% |
| Items with invented lines | 47 | 10 (grep reformatting, identical in both) |
| Fact pairs intact | 3,065 of 3,486 | 3,486 of 3,486 |
| `git log --stat` file/count pairs intact | 66% | 100% |

92% of the savings lost comes from four READMEs: Kompress glued ~40 lines of
badge HTML into one line, which dense-line elision then replaced with a
retrievable marker. Elsewhere the fix costs 2.2% of savings and saves 15% more
on code. Before the fix a `git log --stat` window became one line with one
commit's counts and date next to another's subject.

Decision: ported. A follow-up could elide runs of badge/HTML lines directly to
win the README savings back without gluing.

Usage: `python corpus.py <Lib> <site-packages> <public clone> out`, then
`python measure.py <build> <label> out/corpus.json out/runs` per build, then
`python analyze.py out/runs/<a>.json out/runs/<b>.json out/corpus.json [pipeline|kompress]`.
