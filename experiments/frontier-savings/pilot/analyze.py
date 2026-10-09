"""Aggregate live results; adjudicate unspecified clone insertion position.

Keep original strict AST grades intact. This post hoc adjudication permits only
the placement of new dictionary entries to vary, not their fields, order among
new entries, order among existing entries, or other source AST changes.
Raw client logs are read locally and never included in the exported aggregate.
"""
from __future__ import annotations
import ast
import copy
import json
from pathlib import Path
from adapter import codec

HERE = Path(__file__).resolve().parent


def catalog(tree):
    return next(n.value for n in ast.walk(tree) if isinstance(n,ast.AnnAssign)
                and isinstance(n.target,ast.Name) and n.target.id=='ANTHROPIC_PRICES')


def adjudicate(row, run_id, fixtures):
    grade = row['passed']
    reason = 'strict_ast_grade'
    if row['task'].startswith('clone_catalog') and not grade and not row['error'] and row['exit']==0 and row['source_unchanged']:
        work = HERE/'runs'/run_id/f"{row['task']}-r{row['rep']}-{row['arm']}"
        if (work/'candidate.py').exists():
            actual=ast.parse((work/'candidate.py').read_text(encoding='utf-8'))
            expected=ast.parse(fixtures[row['task']]['after'])
            original=ast.parse(fixtures[row['task']]['before'])
            a,e,o=catalog(actual),catalog(expected),catalog(original)
            ak,ek,ok=([k.value for k in d.keys] for d in (a,e,o))
            same_keys=len(ak)==len(set(ak)) and set(ak)==set(ek)
            existing_order=[k for k in ak if k in ok]==ok
            new_order=[k for k in ak if k not in ok]==[k for k in ek if k not in ok]
            if same_keys and existing_order and new_order:
                mapping=dict(zip(ak,a.values))
                a.keys=copy.deepcopy(e.keys)
                a.values=[mapping[k] for k in ek]
                grade=ast.dump(actual,include_attributes=False)==ast.dump(expected,include_attributes=False)
                if grade:
                    reason='post_hoc_valid_clone_insertion_position'
    return dict(**row,task_passed=grade,grade_reason=reason)


def aggregate(rows):
    totals={}
    for arm in ('native','compact'):
        rr=[r for r in rows if r['arm']==arm]
        totals[arm]=dict(sessions=len(rr),strict_passes=sum(r['passed'] for r in rr),
            task_passes=sum(r['task_passed'] for r in rr),
            **{k:sum(r[k] or 0 for r in rr) for k in ('api_equivalent_usd','input_tokens','cache_read_tokens',
                'cache_write_tokens','output_tokens','edit_payload_tokens','seconds','rejected_edits','native_fallbacks')})
    n,c=totals['native'],totals['compact']
    return dict(arms=totals,cost_reduction_pct=100*(1-c['api_equivalent_usd']/n['api_equivalent_usd']) if n['api_equivalent_usd'] else None,
        output_reduction_pct=100*(1-c['output_tokens']/n['output_tokens']) if n['output_tokens'] else None,
        cost_saved_usd=n['api_equivalent_usd']-c['api_equivalent_usd'])


def main():
    fixtures={f['name']:f for f in codec.fixtures()}
    studies={}
    for label,name in [('primary','live_results.json'),('higher_output_cap','sensitivity_results.json')]:
        path=HERE/name
        if not path.exists():continue
        s=json.loads(path.read_text(encoding='utf-8'))
        rows=[adjudicate(r,s['run_id'],fixtures) for r in s['rows']]
        pairs=[]
        for task,rep in sorted(set((r['task'],r['rep']) for r in rows)):
            p={r['arm']:r for r in rows if r['task']==task and r['rep']==rep}
            if set(p)=={'native','compact'}:
                pairs.append(dict(task=task,rep=rep,both_task_pass=p['native']['task_passed'] and p['compact']['task_passed'],
                    savings_usd=p['native']['api_equivalent_usd']-p['compact']['api_equivalent_usd']))
        studies[label]=dict(run_id=s['run_id'],model=s['model'],output_cap_per_request=s['output_cap_per_request'],
            summary=aggregate(rows),by_task={t:aggregate([r for r in rows if r['task']==t]) for t in sorted(set(r['task'] for r in rows))},
            non_clone_tasks=aggregate([r for r in rows if not r['task'].startswith('clone_catalog')]) if label=='primary' else None,
            without_output_truncated_task=aggregate([r for r in rows if r['task']!='clone_catalog_row_12']) if label=='primary' else None,
            pairs=pairs,rows=rows)
    total=sum(v['summary']['arms'][a]['api_equivalent_usd'] for v in studies.values() for a in ('native','compact'))
    result=dict(studies=studies,total_api_equivalent_usd=total,budget_usd=3,
        accounting='Claude Pro login; API-equivalent CLI estimate, not provider invoice.',
        adjudication='Post hoc insertion-position tolerance only; original strict grades retained.',
        limitations=['Six authored task types, not random representative client tasks.',
            'Identical repetitions may hit provider prompt caches; not independent observations.',
            'Primary output cap truncates native twelve-row tool output; report sensitivity separately.',
            'AST comparison does not certify comments/formatting or behavioral correctness.'])
    (HERE/'analysis_results.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(dict(total_api_equivalent_usd=total,studies={k:v['summary'] for k,v in studies.items()}),indent=2))


if __name__=='__main__':main()
