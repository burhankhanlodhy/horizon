"""Bounded deterministic repair search on authored toy functions only.

Visible examples select candidates; withheld examples are evaluation only.
Never executes user application code, touches production files, or calls APIs.
Passing finite tests is not proof; the weak-test control demonstrates this.
"""
from __future__ import annotations
import ast
import copy
import hashlib
import json
import time
from pathlib import Path
import tiktoken

HERE = Path(__file__).resolve().parent
ENC = tiktoken.get_encoding('cl100k_base')
RATES = dict(write=3.75, read=.3, output=15.0)  # hypothetical USD/M
CONSTANTS = (0, 1, 2, 10, 100, 1000)
SWAPS = {ast.Add: (ast.Sub, ast.Mult), ast.Sub: (ast.Add,),
         ast.Mult: (ast.Add, ast.Div), ast.Div: (ast.Mult,),
         ast.Lt: (ast.LtE, ast.Gt), ast.LtE: (ast.Lt,),
         ast.Gt: (ast.GtE, ast.Lt), ast.GtE: (ast.Gt,)}


def case(name, source, visible, withheld, category):
    return dict(name=name, source=source, visible=visible, withheld=withheld, category=category)


def fixtures():
    return [
        case('discount_denominator', 'def f(price, pct):\n return round(price*(1-pct/10),2)\n',
             [((100,10),90),((50,20),40),((20,0),20)], [((19.99,25),14.99),((200,100),0)], 'single literal'),
        case('milliseconds', 'def f(ms):\n return ms/100\n',
             [((1000,),1),((2500,),2.5)], [((1,),.001),((-3000,),-3)], 'single literal'),
        case('uplift', 'def f(price, pct):\n return price*(1+pct/10)\n',
             [((100,10),110),((50,20),60)], [((200,5),210),((30,0),30)], 'single literal'),
        case('rectangle_area', 'def f(w,h):\n return w+h\n',
             [((3,4),12),((2,5),10),((0,5),0)], [((1,1),1),((7,8),56)], 'single operator'),
        case('inclusive_upper_bound', 'def f(n):\n return n<10\n',
             [((9,),True),((10,),True),((11,),False)], [((10.5,),False),((-1,),True)], 'single comparator'),
        case('nonnegative', 'def f(x):\n return x>0\n',
             [((0,),True),((1,),True),((-1,),False)], [((.5,),True),((-.1,),False)], 'single comparator'),
        case('two_changes_needed', 'def f(qty, price):\n return qty+price+1\n',
             [((3,4),12),((2,5),10),((0,7),0)], [((7,8),56)], 'unsupported multi edit'),
        case('new_logic_needed', 'def f(x):\n return 0\n',
             [((2,),4),((3,),9)], [((5,),25)], 'unsupported new logic'),
        case('ambiguous_visible_tests', 'def f(x):\n return x*10\n',
             [((0,),0)], [((2,),200),((-1,),-100)], 'ambiguous under weak tests'),
        case('unique_but_overfitted', 'def f(x):\n return x/10\n',
             [((100,),1),((200,),2)], [((-100,),0),((0,),0)], 'unique candidate misses hidden nonnegative requirement'),
    ]


def candidates(source, limit=80):
    original = ast.parse(source)
    # Operator objects can be shared singleton instances in Python ASTs.
    # Address one field by path so changing one '+' cannot mutate two sites.
    def walk_paths(node, path=()):
        yield path, node
        for field, value in ast.iter_fields(node):
            if isinstance(value, ast.AST):
                yield from walk_paths(value, path+((field, None),))
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    if isinstance(item, ast.AST):
                        yield from walk_paths(item, path+((field, i),))
    emitted = set()
    for path, node in walk_paths(original):
        variants = []
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            variants = [ast.Constant(value=v) for v in CONSTANTS if v != node.value]
        elif type(node) in SWAPS:
            variants = [t() for t in SWAPS[type(node)]]
        for new in variants:
            tree = copy.deepcopy(original)
            parent = tree
            for field, index in path[:-1]:
                parent = getattr(parent, field) if index is None else getattr(parent, field)[index]
            field, index = path[-1]
            if index is None:
                setattr(parent, field, new)
            else:
                getattr(parent, field)[index] = new
            text = ast.unparse(ast.fix_missing_locations(tree))+'\n'
            if text not in emitted:
                emitted.add(text)
                yield text
                if len(emitted) >= limit:
                    return


def passes(source, examples):
    # The experiment's own fixtures contain only bounded arithmetic expressions.
    # This is NOT a safe runner for arbitrary submitted programs.
    scope = {'__builtins__': {'round': round}}
    try:
        exec(compile(source, '<authored repair fixture>', 'exec'), scope)
        for args, wanted in examples:
            actual = scope['f'](*args)
            if type(wanted) is bool:
                if type(actual) is not bool or actual is not wanted:
                    return False
            elif abs(actual-wanted) > 1e-9:
                return False
        return True
    except (ArithmeticError, TypeError, NameError):
        return False


def main():
    started = time.perf_counter()
    rows = []
    for task in fixtures():
        assert not passes(task['source'], task['visible']) or task['name'] == 'ambiguous_visible_tests'
        population = list(candidates(task['source']))
        passing = [s for s in population if passes(s, task['visible'])]
        accepted = passing[0] if len(passing) == 1 else None
        # Held-out examples are first consulted after selection is finished.
        correct = passes(accepted, task['withheld']) if accepted else None
        receipt = dict(task=task['name'], source_sha=hashlib.sha256(task['source'].encode()).hexdigest(),
                       candidates=len(population), visible_passing=len(passing),
                       status='candidate_for_review' if accepted else 'fallback_to_agent')
        rows.append(dict(name=task['name'], category=task['category'], candidates=len(population),
                         visible_passing=len(passing), selected=accepted, withheld_pass=correct,
                         receipt_tokens=len(ENC.encode(json.dumps(receipt, separators=(',',':'))))))
    correct = sum(r['withheld_pass'] is True for r in rows)
    wrong = sum(r['withheld_pass'] is False for r in rows)
    fallback = sum(r['withheld_pass'] is None for r in rows)
    # Optimistic economic scenario: each genuinely correct local repair avoids
    # exactly one 40k cached-prefix / 500 new-input / 600 output model retry.
    # All cases still pay for a receipt on a later request and operator review.
    round_cost = (40000*.3+500*3.75+600*15)/1e6
    receipt_cost = sum(r['receipt_tokens']*3.75/1e6 for r in rows)
    result = dict(evaluation='toy feasibility only; zero model/provider calls', tokenizer=ENC.name,
                  hypothetical_usd_per_million=RATES, cases=rows,
                  totals=dict(cases=len(rows), correct_on_withheld=correct, wrong_on_withheld=wrong,
                              fallback=fallback, candidate_executions=sum(r['candidates'] for r in rows)),
                  optimistic_retry_scenario=dict(assumed_avoided_retry_cost_usd=round_cost,
                      baseline_ten_retries_usd=10*round_cost, receipts_usd=receipt_cost,
                      correctly_avoided_retry_gross_usd=correct*round_cost,
                      net_before_review_compute_and_false_acceptance_usd=correct*round_cost-receipt_cost,
                      savings_pct_before_those_costs=100*(correct*round_cost-receipt_cost)/(10*round_cost)),
                  runtime_seconds=time.perf_counter()-started,
                  limitations=['Toy fixtures intentionally include repairable mutations; no estimate of real bug prevalence.',
                               'Finite tests permit false acceptance even with one unique passing candidate.',
                               'Do not apply automatically or bill users for simulated avoided requests.',
                               'A production runner needs process isolation, timeouts, resource limits, clean snapshots and explicit client authorization.',
                               'A proxy cannot run tests against a remote user workspace without a client companion.'])
    assert correct >= 1 and wrong >= 1 and fallback >= 1
    (HERE/'results.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
