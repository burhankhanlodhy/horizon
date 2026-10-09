"""Paired live tool-adoption pilot, using the existing Claude login/model.

Only a stdio preview MCP is available. Raw CLI output stays in ignored runs/.
Costs from a subscription login are API-equivalent estimates, not invoices.
"""
from __future__ import annotations
import argparse
import ast
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime,timezone
from pathlib import Path
from adapter import codec,digest

HERE = Path(__file__).resolve().parent
CLAUDE = Path.home()/'AppData/Roaming/npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe'
TASKS = {
    'one_literal': 'Change only the random byte count in new_session_token from 32 to 48.',
    'five_repeated_keyword_updates': 'Change all five ModelPricing context_window keyword values from 200000 to 260000. Preserve everything else.',
    'clone_catalog_row_4': 'Add four exact clones of ANTHROPIC_PRICES["claude-3-5-sonnet-latest"] under keys fixture-sonnet-0 through fixture-sonnet-3, in that order. Change each cloned model keyword to its new key. Keep all other fields and existing rows identical.',
    'clone_catalog_row_12': 'Add twelve exact clones of ANTHROPIC_PRICES["claude-3-5-sonnet-latest"] under keys fixture-sonnet-0 through fixture-sonnet-11, in that order. Change each cloned model keyword to its new key. Keep all other fields and existing rows identical.',
    'one_short_constant': 'Change only the module constant CACHE_READ_MULTIPLIER from 0.10 to 0.11.',
    'novel_logic_falls_back_to_full_function': 'In PricingRegistry.estimate_cost, change only the regular-input guard to require both input_tokens > 0 and model.strip(). Preserve every other statement and documentation.',
}
SYSTEM = ('You are evaluating an isolated source-edit tool. Read the fixture, then propose exactly the requested change with the provided MCP tool. '
          'All tools generate previews only. Use no unrelated tools. After a successful preview, respond DONE. '
          'If a compact operation is unsupported, use native_fallback. Do not repeat a successful edit or make extra changes.')


def parse_result(stdout):
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value,dict) and value.get('type') == 'result':
            return value
    return {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pairs',type=int,default=3)
    parser.add_argument('--budget',type=float,default=3)
    parser.add_argument('--output-cap',type=int,choices=(2048,8192),default=2048)
    parser.add_argument('--model',default=None)
    parser.add_argument('--summary-name',choices=('live_results.json','sensitivity_results.json'),default='live_results.json')
    parser.add_argument('--tasks',nargs='*',default=list(TASKS))
    parser.add_argument('--run-id',default=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    args = parser.parse_args()
    if not 1 <= args.pairs <= 3 or not 0 < args.budget <= 3 or any(t not in TASKS for t in args.tasks):
        raise ValueError('pilot bounded at three pairs and $3')
    if not args.run_id.replace('-','').replace('_','').isalnum():
        raise ValueError('invalid run id')
    root = HERE/'runs'/args.run_id
    root.mkdir(parents=True,exist_ok=False)
    fixtures = {f['name']:f for f in codec.fixtures()}
    plan = []
    rng = random.Random(20261009)
    for rep in range(args.pairs):
        for task in args.tasks:
            arms = ['native','compact']
            rng.shuffle(arms)
            plan.extend((rep,task,arm) for arm in arms)
    rows = []
    spent = 0.0
    reserve = .40  # do not start another CLI session close to the total cap
    model = args.model
    for rep,task,arm in plan:
        if spent+reserve > args.budget:
            print(json.dumps(dict(status='budget_stop',spent_api_equivalent_usd=spent)),flush=True)
            break
        work = root/f'{task}-r{rep}-{arm}'
        work.mkdir()
        fixture = fixtures[task]
        (work/'before.py').write_text(fixture['before'],encoding='utf-8')
        mcp = dict(mcpServers=dict(pilot=dict(command=sys.executable,args=[str(HERE/'server.py'),'--work',str(work),'--arm',arm])))
        config = work/'mcp.json'
        config.write_text(json.dumps(mcp),encoding='utf-8')
        per_session_cap = min(.12,args.budget-spent-reserve)
        cmd = [str(CLAUDE),'-p',TASKS[task],'--output-format','json','--system-prompt',SYSTEM,
               '--tools','','--allowedTools','mcp__pilot__read_fixture','mcp__pilot__edit','mcp__pilot__native_fallback',
               '--mcp-config',str(config),'--strict-mcp-config','--no-session-persistence',
               '--disable-slash-commands','--settings','{"disableAllHooks":true}',
               '--max-turns','6','--max-budget-usd',str(per_session_cap)]
        if model:
            cmd.extend(['--model',model])
        env = dict(os.environ,PYTHONIOENCODING='utf-8',CLAUDE_CODE_MAX_OUTPUT_TOKENS=str(args.output_cap))
        env.pop('CLAUDECODE',None)
        started = time.perf_counter()
        p = subprocess.Popen(cmd,cwd=work,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace')
        timed_out = False
        try:
            stdout,stderr = p.communicate(timeout=150)
        except subprocess.TimeoutExpired:
            timed_out = True
            subprocess.run(['taskkill','/PID',str(p.pid),'/T','/F'],capture_output=True,timeout=15)
            stdout,stderr = p.communicate(timeout=15)
        (work/'stdout.txt').write_text(stdout,encoding='utf-8')
        (work/'stderr.txt').write_text(stderr,encoding='utf-8')
        result = parse_result(stdout)
        usage = result.get('usage') or {}
        cost = result.get('total_cost_usd')
        observed_models = sorted((result.get('modelUsage') or {}).keys())
        if model is None and len(observed_models) == 1:
            model = observed_models[0]
        events = [json.loads(line) for line in (work/'tools.jsonl').read_text(encoding='utf-8').splitlines()] if (work/'tools.jsonl').exists() else []
        candidate = work/'candidate.py'
        matches = False
        if candidate.exists():
            try:
                matches = ast.dump(ast.parse(candidate.read_text(encoding='utf-8')),include_attributes=False) == ast.dump(ast.parse(fixture['after']),include_attributes=False)
            except SyntaxError:
                pass
        unchanged = digest((work/'before.py').read_text(encoding='utf-8')) == digest(fixture['before'])
        row = dict(rep=rep,task=task,arm=arm,models=observed_models,exit=p.returncode,
                   timed_out=timed_out,error=bool(result.get('is_error')),
                   result_subtype=result.get('subtype'),candidate_ast_matches=matches,
                   source_unchanged=unchanged,passed=matches and unchanged and not result.get('is_error') and p.returncode==0,
                   tool_calls=len(events),rejected_edits=sum(e.get('result',{}).get('ok') is False for e in events),
                   native_fallbacks=sum(e['tool']=='native_fallback' for e in events),
                   edit_payload_tokens=sum(codec.tokens(codec.wire(e['payload'])) for e in events if 'payload' in e),
                   input_tokens=usage.get('input_tokens'),cache_read_tokens=usage.get('cache_read_input_tokens'),
                   cache_write_tokens=usage.get('cache_creation_input_tokens'),output_tokens=usage.get('output_tokens'),
                   api_equivalent_usd=cost,seconds=round(time.perf_counter()-started,3))
        rows.append(row)
        (root/'results.json').write_text(json.dumps(dict(rows=rows,budget_usd=args.budget),indent=2)+'\n',encoding='utf-8')
        print(json.dumps(row),flush=True)
        if cost is not None:
            spent += cost
        if cost is None or timed_out or not result or not observed_models or (model and observed_models != [model]):
            print(json.dumps(dict(status='stop_unverifiable_usage_or_model')),flush=True)
            break
        if cost > .35:
            print(json.dumps(dict(status='stop_single_run_exceeded_reserve_assumption')),flush=True)
            break
    completed_pairs = []
    for rep in range(args.pairs):
        for task in args.tasks:
            pair = {r['arm']:r for r in rows if r['rep']==rep and r['task']==task}
            if set(pair)=={'native','compact'}:
                n,c = pair['native'],pair['compact']
                completed_pairs.append(dict(rep=rep,task=task,both_pass=n['passed'] and c['passed'],
                    native_usd=n['api_equivalent_usd'],compact_usd=c['api_equivalent_usd'],
                    savings_usd=(n['api_equivalent_usd']-c['api_equivalent_usd']) if n['api_equivalent_usd'] is not None and c['api_equivalent_usd'] is not None else None))
    summary = dict(run_id=args.run_id,model=model,budget_usd=args.budget,spent_api_equivalent_usd=spent,
                   calls_completed=len(rows),planned_calls=len(plan),pairs=completed_pairs,rows=rows,
                   scope='Preview MCP tool adoption, not full deployed-proxy end-to-end evaluation.',
                   accounting='Existing Claude Pro login; CLI cost is API-equivalent, not a provider invoice.',
                   output_cap_per_request=args.output_cap,per_session_budget_cap=.12,
                   prompt='Custom system prompt identical across arms; existing configured model discovered and pinned.',
                   grading='Complete Python AST equality against independent expected fixture; no source modifications.')
    (root/'summary.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
    # Public summary includes no raw prompts, tool contents, credentials or IDs.
    (HERE/args.summary_name).write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(dict(status='finished',model=model,calls=len(rows),complete_pairs=len(completed_pairs),spent_api_equivalent_usd=spent)),flush=True)


if __name__ == '__main__':
    main()
