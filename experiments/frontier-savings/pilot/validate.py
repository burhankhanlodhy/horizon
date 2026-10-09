"""Offline guard/fixture checks for the preview adapter; no model calls."""
import copy
import json
from adapter import Adapter,Rejected,codec


def main():
    passed = 0
    fixtures = codec.fixtures()
    for f in fixtures:
        if f['recipe']['ops'][0][0] not in ('literal','keyword','clone_dict'):
            continue
        adapter = Adapter()
        receipt = adapter.read(f['before'])['r']
        payload = {**f['recipe'],'r':receipt}
        assert adapter.compact(f['before'],payload) == f['after']
        passed += 1
    f = fixtures[0]
    a = Adapter()
    r = a.read(f['before'])['r']
    good = {**f['recipe'],'r':r}
    count = copy.deepcopy(good)
    count['ops'][0][-1] = 2
    bad_second = {**good,'ops':good['ops']+[['literal','missing',1,2,1]]}
    probes = [
        ('unknown_receipt',f['before'],{**good,'r':'r404'}),
        ('stale_receipt',f['before']+'\n',good),
        ('wrong_count',f['before'],count),
        ('atomic_failed_second',f['before'],bad_second),
        ('unsafe_rename',f['before'],dict(r=r,ops=[['rename','hash_token','token','x',2]])),
        ('bool_count',f['before'],dict(r=r,ops=[['literal','new_session_token',32,48,True]])),
        ('type_mismatch',f['before'],dict(r=r,ops=[['literal','new_session_token',32,'48',1]])),
        ('extra_field',f['before'],{**good,'path':'../../api/security.py'}),
    ]
    guards = []
    for name,source,payload in probes:
        try:
            a.compact(source,payload)
        except Rejected:
            guards.append(dict(case=name,rejected=True))
        else:
            raise AssertionError(name)
    assert a.compact(f['before'],good) == f['after']
    native = dict(r=r,edits=[dict(old_string='token_urlsafe(32)',new_string='token_urlsafe(48)')])
    assert a.native(f['before'],native) == f['after']
    result = dict(supported_fixture_expansions=passed,guards=guards,native_exact_replacement=True,
                  atomic_failure_leaves_original_receipt_valid=True,model_calls=0)
    from pathlib import Path
    (Path(__file__).parent/'validation_results.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__ == '__main__':
    main()
