"""Preview-only compact edit adapter for an isolated pilot, not production.

No source file writes. Exact expansion delegates to the tested offline codec;
this wrapper restricts its grammar and verifies a real snapshot receipt.
"""
from __future__ import annotations
import ast
import hashlib
import importlib.util
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('edit_prototype', HERE.parent/'edit_codec/prototype.py')
codec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(codec)


class Rejected(ValueError):
    pass


def digest(source):
    return hashlib.sha256(source.encode()).hexdigest()


def scalar(value):
    return (type(value) in (str,int,float,bool) or value is None) and (
        not isinstance(value,str) or len(value) <= 200) and (
        not isinstance(value,float) or math.isfinite(value))


class Adapter:
    def __init__(self):
        self.snapshots = {}

    def read(self, source):
        ast.parse(source)
        receipt = 'r'+str(len(self.snapshots))
        self.snapshots[receipt] = (digest(source),source)
        return dict(r=receipt,source=source)

    def validate(self, source, payload, key):
        if type(payload) is not dict or set(payload) != {'r',key}:
            raise Rejected('invalid fields')
        receipt = payload['r']
        if not isinstance(receipt,str) or receipt not in self.snapshots:
            raise Rejected('unknown receipt; read again')
        snapshot_hash, snapshot = self.snapshots[receipt]
        if digest(source) != snapshot_hash:
            raise Rejected('stale receipt; read again')
        return snapshot

    def compact(self, source, payload):
        self.validate(source,payload,'ops')
        operations = payload['ops']
        if not isinstance(operations,list) or not 1 <= len(operations) <= 4:
            raise Rejected('one to four operations required')
        current = source
        try:
            for op in operations:
                if not isinstance(op,list) or not op:
                    raise Rejected('invalid operation')
                kind = op[0]
                if kind == 'literal':
                    if len(op) != 5 or not isinstance(op[1],str):
                        raise Rejected('literal signature')
                    if not scalar(op[2]) or type(op[2]) is not type(op[3]) or not scalar(op[3]):
                        raise Rejected('literal type mismatch')
                elif kind == 'keyword':
                    if len(op) != 6 or not all(isinstance(x,str) for x in op[1:3]):
                        raise Rejected('keyword signature')
                    if not scalar(op[3]) or type(op[3]) is not type(op[4]) or not scalar(op[4]):
                        raise Rejected('keyword type mismatch')
                elif kind == 'clone_dict':
                    if len(op) != 4 or not all(isinstance(x,str) for x in op[1:3]):
                        raise Rejected('clone signature')
                    keys = op[3]
                    if not isinstance(keys,list) or not 1 <= len(keys) <= 16 or not all(
                            isinstance(k,str) and 0 < len(k) <= 80 for k in keys):
                        raise Rejected('bounded clone keys required')
                    self._safe_template(current,op[1],op[2])
                else:
                    raise Rejected('unsupported operation; use native_fallback')
                if kind != 'clone_dict' and (type(op[-1]) is not int or not 1 <= op[-1] <= 128):
                    raise Rejected('bounded exact count required')
                current = codec.apply_operation(current,op)
        except (ValueError,TypeError,IndexError,SyntaxError) as exc:
            raise Rejected(str(exc)) from exc
        if current == source:
            raise Rejected('no change')
        ast.parse(current)
        return current

    def _safe_template(self, source, variable, key):
        matches = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node,ast.AnnAssign) and isinstance(node.target,ast.Name) and node.target.id == variable and isinstance(node.value,ast.Dict):
                for k,v in zip(node.value.keys,node.value.values):
                    if isinstance(k,ast.Constant) and k.value == key:
                        matches.append(v)
        if len(matches) != 1:
            raise Rejected('missing or ambiguous clone template')
        value = matches[0]
        if not isinstance(value,ast.Call) or not isinstance(value.func,ast.Name) or value.func.id != 'ModelPricing' or value.args or any(
                kw.arg is None or not isinstance(kw.value,ast.Constant) for kw in value.keywords):
            raise Rejected('clone accepts literal-only ModelPricing templates')

    def native(self, source, payload):
        self.validate(source,payload,'edits')
        edits = payload['edits']
        if not isinstance(edits,list) or not 1 <= len(edits) <= 16:
            raise Rejected('one to sixteen exact edits required')
        current = source
        for edit in edits:
            if type(edit) is not dict or not {'old_string','new_string'} <= set(edit) or not set(edit) <= {'old_string','new_string','replace_all'}:
                raise Rejected('invalid native edit fields')
            old,new = edit['old_string'],edit['new_string']
            all_matches = edit.get('replace_all',False)
            if not isinstance(old,str) or not old or not isinstance(new,str) or type(all_matches) is not bool:
                raise Rejected('invalid native replacement')
            if len(old)+len(new) > 100000:
                raise Rejected('oversized native replacement')
            count = current.count(old)
            if count == 0 or (count != 1 and not all_matches):
                raise Rejected('old_string missing or ambiguous')
            current = current.replace(old,new) if all_matches else current.replace(old,new,1)
        try:
            ast.parse(current)
        except SyntaxError as exc:
            raise Rejected('native edit does not parse') from exc
        if current == source:
            raise Rejected('no change')
        return current
