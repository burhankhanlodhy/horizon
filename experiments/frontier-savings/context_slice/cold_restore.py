"""Offline cold-restore investigation on local client history and source slices.

Exports counts/cost scenarios only. No raw history is written or sent anywhere.
Known source entrypoints are oracle inputs; this is not a model quality eval.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from prototype import Index,TASKS,PROFILES,count,price

HERE = Path(__file__).resolve().parent
PROJECT = Path.home()/'.claude/projects/C--Users-Burhan-OneDrive-Desktop-ContextShrink'


def wire(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)


def histories():
    corpus = hashlib.sha256()
    episodes = []
    files = sorted(PROJECT.rglob('*.jsonl'))
    for path in files:
        data = path.read_bytes()
        corpus.update(hashlib.sha256(data).digest())
        groups,assistants,calls,seen = [],{},{},set()
        for line in data.splitlines():
            try:
                row = json.loads(line)
            except (ValueError,UnicodeDecodeError):
                continue
            if row.get('subtype') == 'compact_boundary' or row.get('isCompactSummary'):
                if groups:
                    episodes.append(groups)
                groups,assistants,calls,seen = [],{},{},set()
            kind = row.get('type')
            message = row.get('message') or {}
            content = message.get('content')
            if kind == 'assistant':
                key = message.get('id') or row.get('uuid')
                if key not in assistants:
                    assistants[key] = dict(parts=[],instruction=False,tool_ids=set(),results=set())
                    groups.append(assistants[key])
                group = assistants[key]
                blocks = content if isinstance(content,list) else [{'type':'text','text':content or ''}]
                for block in blocks:
                    marker = (key,wire(block))
                    if marker in seen:
                        continue
                    seen.add(marker)
                    if block.get('type') == 'tool_use':
                        identity = block.get('id')
                        calls[identity] = group
                        group['tool_ids'].add(identity)
                        group['parts'].append(wire(block))
                    elif block.get('type') in ('text','thinking'):
                        group['parts'].append(block.get('text') or block.get('thinking') or '')
            elif kind == 'user':
                if isinstance(content,str):
                    groups.append(dict(parts=[content],instruction=True,tool_ids=set(),results=set()))
                elif isinstance(content,list):
                    for block in content:
                        if block.get('type') == 'tool_result':
                            identity = block.get('tool_use_id')
                            marker = ('result',identity)
                            if marker in seen:
                                continue
                            seen.add(marker)
                            group = calls.get(identity)
                            if group is None:
                                # Orphan results are retained as independent evidence;
                                # their protocol validity cannot be certified.
                                group = dict(parts=[],instruction=False,tool_ids=set(),results={identity})
                                groups.append(group)
                            group['results'].add(identity)
                            group['parts'].append(wire(block.get('content')))
                        elif block.get('type') == 'text':
                            groups.append(dict(parts=[block.get('text','')],instruction=True,tool_ids=set(),results=set()))
        if groups:
            episodes.append(groups)
    for episode in episodes:
        for group in episode:
            group['tokens'] = count('\n'.join(group['parts']))
    return files,corpus.hexdigest(),episodes


def gate(full,kept,rates,horizon,recoveries,state,price_known=True,source_valid=True,pairs_valid=True):
    if state != 'cold':
        return dict(allow=False,reason='preserve_warm_or_unknown_prefix')
    if not price_known or not source_valid or not pairs_valid:
        return dict(allow=False,reason='unverified_price_source_or_pairs')
    ledger = price(full,kept,horizon,rates,True,recoveries,reread_fraction=.02)
    return dict(allow=ledger['saving_usd']>0,reason='positive_model' if ledger['saving_usd']>0 else 'overhead_exceeds_benefit',**ledger)


def main():
    files,fingerprint,episodes = histories()
    if not episodes:
        raise ValueError('No local histories available')
    largest = max(episodes,key=lambda e:sum(g['tokens'] for g in e))
    index = Index({m for t in TASKS for m in t['scopes']})
    targets = (100000,250000,500000)
    rows = []
    for target in targets:
        window = []
        n = 0
        for group in reversed(largest):
            if n >= target:
                break
            window.append(group)
            n += group['tokens']
        window.reverse()
        if n < target:
            rows.append(dict(target_tokens=target,available_tokens=n,status='insufficient_history'))
            continue
        # Preserve every visible user instruction and the most recent 12 atomic
        # groups. Tool call/results in one group are never separated.
        protected = [g for i,g in enumerate(window) if g['instruction'] or i >= len(window)-12]
        protected_tokens = count('\n'.join('\n'.join(g['parts']) for g in protected))
        dangling = sum(bool(g['tool_ids'] ^ g['results']) for g in protected)
        for task in TASKS:
            seed = (task['module'],task['entry'])
            selected = index.closure(seed)
            sliced = index.pack(selected)
            all_source = '\n'.join(index.sources[m] for m in task['scopes'])
            plain_narrow = '\n'.join(''.join(index.preludes[m])+'\n'+
                                     '\n'.join(index.units[k].text for k in sorted(selected) if k[0]==m)
                                     for m in sorted({m for m,_ in selected}))
            full = n+count(all_source)
            kept = protected_tokens+count(sliced)
            narrow = protected_tokens+count(plain_narrow)
            checks = [f in sliced for f in task['facts']]+[
                any(s==name for _,s in selected) for name in task['support']]
            scenarios = {}
            for label,rates in PROFILES.items():
                scenarios[label] = {}
                for horizon in (1,5,20):
                    for recovery in (0,1,3):
                        scenarios[label][f'h{horizon}_r{recovery}'] = gate(full,kept,rates,horizon,recovery,'cold',pairs_valid=dangling==0)
                # Strong already-narrow control, same evidence and no recovery.
                scenarios[label]['already_narrow_control'] = price(narrow,kept,20,rates,True,0)
            rows.append(dict(target_tokens=target,actual_history_text_tokens=n,status='evaluated',task=task['id'],
                             protected_history_tokens=protected_tokens,full_with_source_tokens=full,
                             retained_with_receipts_tokens=kept,already_narrow_tokens=narrow,
                             retained_fraction=kept/full,user_instruction_groups=sum(g['instruction'] for g in window),
                             preserved_user_instruction_groups=sum(g['instruction'] for g in protected),
                             protected_atomic_groups=len(protected),dangling_tool_groups=dangling,
                             source_checks_passed=sum(checks),source_checks_total=len(checks),scenarios=scenarios))
    rates = PROFILES['anthropic_like_1h']
    guards = {name:gate(500000,50000,rates,20,1,state,**kw)['allow'] for name,state,kw in [
        ('warm','warm',{}),('unknown_cache','unknown',{}),('unknown_price','cold',{'price_known':False}),
        ('stale_source','cold',{'source_valid':False}),('dangling_tool_pair','cold',{'pairs_valid':False})]}
    assert not any(guards.values())
    assert all(r['source_checks_passed']==r['source_checks_total'] and r['preserved_user_instruction_groups']==r['user_instruction_groups'] for r in rows if r['status']=='evaluated')
    result = dict(evaluation='Private local history counts + oracle source slices + hypothetical economics; no model calls.',
                  corpus_files=len(files),corpus_sha256=fingerprint,history_episodes=len(episodes),
                  largest_episode_text_tokens=sum(g['tokens'] for g in largest),guards_blocked={k:not v for k,v in guards.items()},
                  hypothetical_rates=PROFILES,cases=rows,
                  limitations=['Client history text is not the exact forwarded provider request; system, signatures and protocol overhead differ.',
                               'Known entrypoints and task supports are oracle inputs; no model task-success measurement.',
                               'Older assistant decisions/observations can be omitted; preserving all user text does not preserve all requirements.',
                               'Same protected-history plus narrow-source control already obtains the source reduction.',
                               'The gate is conservative on warm/unknown states, but hypothetical economic positivity does not certify semantic safety.',
                               'A production cold boundary and current model price must be verified separately.'])
    (HERE/'cold_restore_results.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(dict(corpus_files=len(files),episodes=len(episodes),largest_text_tokens=result['largest_episode_text_tokens'],
                         guards=result['guards_blocked'],cases=[{k:r[k] for k in ('target_tokens','status','task','actual_history_text_tokens','protected_history_tokens','retained_with_receipts_tokens','dangling_tool_groups') if k in r} for r in rows]),indent=2))


if __name__ == '__main__':
    main()
