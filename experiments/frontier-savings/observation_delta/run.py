"""Offline lossless observation-delta experiment. No provider or application calls.

Inputs are controlled observations, including copies of repository files.
Exact reconstruction checks are not LLM comprehension or task-success checks.
"""
from __future__ import annotations
import difflib
import hashlib
import json
import random
from pathlib import Path
import tiktoken

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
ENC = tiktoken.get_encoding('cl100k_base')
RATES = dict(write=3.75, read=.30, output=15.0)  # hypothetical USD/M


def wire(x):
    return json.dumps(x, separators=(',', ':'), ensure_ascii=False)


def tokens(x):
    return len(ENC.encode(x if isinstance(x, str) else wire(x), disallowed_special=()))


def sha(x):
    return hashlib.sha256(x.encode()).hexdigest()


def encode(base, text):
    full = dict(kind='full', sha=sha(text), text=text)
    if base is None:
        return full
    a, b = base.splitlines(True), text.splitlines(True)
    edits = [[i, j, b[k:l]] for op, i, j, k, l in
             difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if op != 'equal']
    delta = dict(kind='delta', base=sha(base), sha=sha(text), edits=edits)
    return min((full, delta), key=tokens)


def decode(base, packet):
    if packet['kind'] == 'full':
        text = packet['text']
    elif packet['kind'] == 'delta':
        if base is None or sha(base) != packet['base']:
            raise ValueError('missing or stale base')
        lines = base.splitlines(True)
        previous = len(lines) + 1
        for start, end, replacement in reversed(packet['edits']):
            if not 0 <= start <= end <= len(lines) or end > previous:
                raise ValueError('invalid or overlapping edit')
            lines[start:end] = replacement
            previous = start
        text = ''.join(lines)
    else:
        raise ValueError('unknown packet')
    if sha(text) != packet['sha']:
        raise ValueError('corrupt reconstruction')
    return text


def cost(lengths, reset_every=None, extra_output=0, recovery_lengths=None):
    # First prefix is cold; subsequent history reads hit cache. New observations
    # are written once and retained, and output is read on later requests.
    prior = 0
    total = 0.0
    for i, n in enumerate(lengths):
        cold = i == 0 or (reset_every and i % reset_every == 0)
        if cold:
            prior = 0
        n += (recovery_lengths[i] if recovery_lengths else 0)
        total += (20000 * (RATES['write'] if cold else RATES['read'])
                  + prior * RATES['read'] + n * RATES['write']
                  + (400 + extra_output) * RATES['output']) / 1e6
        prior += n + 400 + extra_output
    return total


def fixtures():
    for path in ('horizon/proxy/cache_keeper.py', 'api/billing.py',
                 'dashboard/src/pages/Subscriptions.tsx'):
        source = (ROOT / path).read_text(encoding='utf-8')
        # A controlled comment change is explicitly synthetic, not real history.
        marker = '// observation version ' if path.endswith('tsx') else '# observation version '
        observations = [source + '\n' + marker + str(i) + '\n' for i in range(20)]
        yield dict(name=path, observations=observations, reset=None, scope='repo copy; synthetic one-line revisions')
    observations = [''.join(f'test_case_{j:04d}: {"FAILED" if j == i else "PASSED"}\n'
                            for j in range(300)) for i in range(20)]
    yield dict(name='repeated_diagnostics', observations=observations, reset=None, scope='synthetic 300-test diagnostics')
    yield dict(name='diagnostics_compaction_resets', observations=observations, reset=5, scope='same diagnostics, full base every 5 turns')
    yield dict(name='tiny_output', observations=[f'result={i}\n' for i in range(20)], reset=None, scope='negative control')
    rng = random.Random(73)
    observations = ['\n'.join(''.join(rng.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=50))
                              for _ in range(150)) + '\n' for _ in range(20)]
    yield dict(name='high_churn', observations=observations, reset=None, scope='negative control; fixed seed 73')


def main():
    rows = []
    exact = 0
    for case in fixtures():
        base = None
        raw, receipt, native = [], [], []
        kinds = []
        for i, text in enumerate(case['observations']):
            if case['reset'] and i % case['reset'] == 0:
                base = None
            packet = encode(base, text)
            assert decode(base, packet) == text
            exact += 1
            raw.append(tokens(text))
            receipt.append(tokens(packet))
            diff = ''.join(difflib.unified_diff(base.splitlines(True), text.splitlines(True),
                            fromfile='prior', tofile='current', n=3)) if base is not None else text
            native.append(min(tokens(text), tokens(diff)))
            kinds.append(packet['kind'])
            base = text
        costs = {name: cost(lengths, case['reset']) for name, lengths in
                 [('full', raw), ('receipt_delta', receipt), ('native_diff', native)]}
        # Illustrative, unmeasured comprehension tax: 50 extra output tokens
        # each turn and a full duplicate retrieval every fifth turn.
        recovery = [raw[i] if i % 5 == 4 else 0 for i in range(len(raw))]
        costs['receipt_with_recovery_tax'] = cost(receipt, case['reset'], 50, recovery)
        rows.append(dict(name=case['name'], scope=case['scope'], observations=len(raw),
                         reset_every=case['reset'], tokens_full=sum(raw), tokens_receipt=sum(receipt),
                         tokens_native_diff=sum(native), packet_kinds=kinds, costs_usd=costs,
                         savings_vs_full_pct=100*(1-costs['receipt_delta']/costs['full']),
                         savings_vs_native_pct=100*(1-costs['receipt_delta']/costs['native_diff']),
                         stressed_savings_vs_full_pct=100*(1-costs['receipt_with_recovery_tax']/costs['full'])))
    packet = encode('a\nb\n', 'a\nc\n' * 100)
    # Force a valid delta, independent of token-size fallback.
    packet = dict(kind='delta', base=sha('a\nb\n'), sha=sha('a\nc\n'), edits=[[1, 2, ['c\n']]])
    guards = []
    for name, base, altered in [('missing_base', None, packet), ('stale_base', 'a\nz\n', packet),
                               ('corrupt_sha', 'a\nb\n', {**packet, 'sha': '0'*64})]:
        try:
            decode(base, altered)
        except ValueError:
            guards.append(dict(case=name, rejected=True))
        else:
            raise AssertionError(name)
    result = dict(evaluation='offline controlled observations; zero model/provider calls',
                  tokenizer=ENC.name, hypothetical_usd_per_million=RATES,
                  exact_reconstruction_checks=exact, guards=guards, cases=rows,
                  limitations=['No measurement of model comprehension, retrieval frequency, task success or real billing.',
                               'Native diff is already smaller than receipt packets on many tasks.',
                               'Always preserve base snapshots; reset and rehydrate after compaction.',
                               'Transport compression decoded before the model saves no model tokens.'])
    (HERE/'results.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'exact_checks': exact, 'guards': guards,
                      'cases': [{k:v for k,v in r.items() if k not in ('packet_kinds',)} for r in rows]}, indent=2))


if __name__ == '__main__':
    main()
