"""Private local replay of repeated Claude tool observations for THIS repository.

Writes aggregate counts only. No raw bodies, arguments, session IDs or paths
from transcripts are exported. No provider calls and no dollar attribution.
"""
from __future__ import annotations
import hashlib
import json
from collections import Counter
from pathlib import Path
from run import encode, tokens

HERE = Path(__file__).resolve().parent
PROJECT = Path.home()/'.claude/projects/C--Users-Burhan-OneDrive-Desktop-ContextShrink'


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(c.get('text', '') for c in content if isinstance(c, dict) and c.get('type') == 'text')
    return ''


def main():
    paths = sorted(PROJECT.rglob('*.jsonl'))
    corpus = hashlib.sha256()
    total = Counter()
    eligible = Counter()
    invalid_lines = 0
    for path in paths:
        # Freeze one file at a time; exact observed bytes go into the aggregate
        # fingerprint. The active session may append after it was read.
        data = path.read_bytes()
        corpus.update(hashlib.sha256(data).digest())
        calls, prior, seen = {}, {}, set()
        for line in data.splitlines():
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                invalid_lines += 1
                continue
            if row.get('subtype') == 'compact_boundary' or row.get('isCompactSummary'):
                prior.clear()
            content = (row.get('message') or {}).get('content')
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'tool_use':
                    arguments = json.dumps([block.get('name'), block.get('input')], sort_keys=True)
                    calls[block.get('id')] = (block.get('name'), hashlib.sha256(arguments.encode()).hexdigest())
                elif block.get('type') == 'tool_result':
                    call_id = block.get('tool_use_id')
                    if call_id in seen:
                        continue
                    seen.add(call_id)
                    text = text_of(block.get('content'))
                    total['tool_results'] += 1
                    total['result_tokens'] += tokens(text)
                    name, key = calls.get(call_id, (None, None))
                    # Narrow read-only tool boundary; Bash may contain side
                    # effects or nondeterminism even with identical arguments.
                    if name not in ('Read', 'Grep', 'Glob') or not text:
                        continue
                    base = prior.get(key)
                    raw_n = tokens(text)
                    packet_n = tokens(encode(base, text))
                    eligible['observations'] += 1
                    eligible['raw_tokens'] += raw_n
                    eligible['receipt_tokens'] += packet_n
                    # Oracle size-only gate, no cost for deciding/communicating
                    # which mode. Do not mistake it for a deployed controller.
                    eligible['oracle_best_tokens'] += min(raw_n, packet_n)
                    if base is not None:
                        eligible['repeated_same_arguments'] += 1
                        eligible['repeated_raw_tokens'] += raw_n
                        eligible['exact_duplicate_results'] += int(base == text)
                    prior[key] = text
    result = dict(evaluation='local transcript corpus census; no dollar or quality claims',
                  scope='Claude project transcripts for ContextShrink including nested sessions',
                  source_files=len(paths), corpus_sha256=corpus.hexdigest(),
                  malformed_or_partial_lines=invalid_lines, totals=dict(total), eligible_read_tools=dict(eligible),
                  emitted_token_reduction_upper_bound_pct=(100*(1-eligible['oracle_best_tokens']/eligible['raw_tokens'])
                                                           if eligible['raw_tokens'] else 0),
                  limitations=['Inputs are client logs, not captured forwarded proxy requests.',
                               'Current proxy compression and native repeat-read elision are not subtracted.',
                               'Compaction markers reset base availability; missed markers bias the ceiling upward.',
                               'Same-argument reads miss overlapping ranges and changed read parameters.',
                               'No savings value from this census belongs in billing.'])
    (HERE/'transcript_results.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__ == '__main__':
    main()
