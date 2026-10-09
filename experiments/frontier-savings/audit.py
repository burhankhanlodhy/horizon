"""Aggregate sensitivity checks over saved offline experiments; no API calls."""
from __future__ import annotations
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def read(folder):
    return json.loads((HERE/folder/'results.json').read_text(encoding='utf-8'))


def stream_cost(outputs, receipts, instruction_tokens=0, extra_reasoning=0):
    # Shared 9-edit conversation; common 20k prefix, warm after first request.
    # Receipt enters on the next turn conceptually, charged once here. Common
    # source discovery, execution results and final prose excluded equally.
    prior = 0
    total = 0
    for i, (output, receipt) in enumerate(zip(outputs, receipts)):
        n = output+extra_reasoning
        total += ((20000+instruction_tokens)*(3.75 if i == 0 else .30)
                  + prior*.30 + n*15 + receipt*3.75)/1e6
        prior += n+receipt
    return total


def main():
    codec = read('edit_codec')
    rows = codec['cases']
    native = [r['best_baseline_tokens'] for r in rows]
    fixed = [r['recipe_output_tokens'] for r in rows]
    # Oracle intent/size selector, not measured model behavior.
    adaptive = [min(a,b) for a,b in zip(native,fixed)]
    receipts = [r['receipt_input_tokens'] if b<a else 0 for r,a,b in zip(rows,native,fixed)]
    baseline = stream_cost(native,[0]*len(rows))
    costs = dict(baseline=baseline,
                 fixed_codec=stream_cost(fixed,[r['receipt_input_tokens'] for r in rows],codec['protocol_instruction_tokens']),
                 adaptive_oracle=stream_cost(adaptive,receipts,codec['protocol_instruction_tokens']),
                 adaptive_plus_300_reasoning_each=stream_cost(adaptive,receipts,codec['protocol_instruction_tokens'],300))
    spec = importlib.util.spec_from_file_location('context_proto',HERE/'context_slice/prototype.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    ctx = read('context_slice')
    controls = {}
    for profile,rates in module.PROFILES.items():
        base = treated = 0
        for row in ctx['tasks']:
            # Same selected source; no recovery cycle in either branch.
            ledger = module.price(row['narrow_control_tokens'],row['slice_tokens'],20,rates,True,0)
            base += ledger['baseline_usd']
            treated += ledger['treatment_usd']
        controls[profile] = dict(narrow_baseline_usd=base,receipts_usd=treated,
                                 savings_pct=100*(1-treated/base))
    result = dict(evidence='offline modeled component streams; zero paid API calls',
                  codec=dict(cases=len(rows),native_output_tokens=sum(native),fixed_codec_output_tokens=sum(fixed),
                             adaptive_output_tokens=sum(adaptive),
                             adaptive_output_reduction_pct=100*(1-sum(adaptive)/sum(native)),
                             shared_nine_edit_stream_usd=costs,
                             savings_vs_baseline_pct={k:100*(1-v/baseline) for k,v in costs.items() if k!='baseline'},
                             assumptions=['One shared 9-edit conversation; 20k common prefix; $3.75/M write, $.30/M read, $15/M output.',
                                          'Common source discovery, test output and final answer costs excluded; full task percentages will be lower.',
                                          'Selector and extra reasoning are assumptions; no quality or frequency claim.']),
                  context_receipts_vs_already_narrow_source=controls,
                  combine_methods='Do not add percentages: shared prefix, recovery and avoided calls overlap.')
    (HERE/'audit_results.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__ == '__main__':
    main()
