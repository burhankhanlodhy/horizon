"""Isolated preview MCP: reads before.py, writes candidate.py, never edits source."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from adapter import Adapter, Rejected, digest

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--work',type=Path,required=True)
    parser.add_argument('--arm',choices=('native','compact'),required=True)
    args = parser.parse_args()
    work = args.work.resolve()
    work.relative_to((HERE/'runs').resolve())
    before = work/'before.py'
    candidate = work/'candidate.py'
    if before.is_symlink() or candidate.is_symlink():
        raise ValueError('symlink fixture rejected')
    adapter = Adapter()
    server = FastMCP('pilot',log_level='ERROR')

    def record(item):
        with (work/'tools.jsonl').open('a',encoding='utf-8') as f:
            f.write(json.dumps(item,separators=(',',':'))+'\n')

    @server.tool()
    def read_fixture() -> dict:
        """Read the one immutable Python fixture and its snapshot receipt r."""
        result = adapter.read(before.read_text(encoding='utf-8'))
        record(dict(tool='read_fixture',result=result))
        return result

    def preview(payload, native):
        source = before.read_text(encoding='utf-8')
        try:
            updated = adapter.native(source,payload) if native else adapter.compact(source,payload)
            candidate.write_text(updated,encoding='utf-8')
            result = dict(ok=True,preview_only=True,sha=digest(updated))
        except Rejected as exc:
            result = dict(ok=False,error=str(exc),preview_only=True)
        record(dict(tool='native_fallback' if native and args.arm=='compact' else 'edit',payload=payload,result=result))
        return result

    if args.arm == 'compact':
        @server.tool()
        def edit(payload: dict) -> dict:
            """Preview a compact edit: {r,ops:[op]}. Supported ops:
            [literal,Python_scope,old_value,new_value,exact_count];
            [keyword,callee,keyword,old_value,new_value,exact_count];
            [clone_dict,variable,source_key,[new_keys]]. Literal types must match.
            Clone only literal ModelPricing rows, replacing key and model keyword.
            No rename/new logic. Use native_fallback for unsupported edits.
            Count exactly, use receipt from read_fixture, preserve other bytes.
            """
            return preview(payload,False)

        @server.tool()
        def native_fallback(payload: dict) -> dict:
            """Preview unsupported logic with {r,edits:[{old_string,new_string,
            replace_all:false}]}. old_string must occur exactly once unless
            replace_all:true. Preserve other source. Does not modify before.py.
            """
            return preview(payload,True)
    else:
        @server.tool()
        def edit(payload: dict) -> dict:
            """Preview exact native search/replace with {r,edits:[{old_string,
            new_string,replace_all:false}]}. old_string must occur exactly once
            unless replace_all:true. Preserve other source. Does not modify before.py.
            """
            return preview(payload,True)
    server.run(transport='stdio')


if __name__ == '__main__':
    main()
