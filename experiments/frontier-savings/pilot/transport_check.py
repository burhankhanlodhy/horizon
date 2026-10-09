"""Offline MCP client/server round trip; no provider calls."""
import asyncio
import json
import sys
import uuid
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from adapter import codec

HERE = Path(__file__).resolve().parent


def unpack(result):
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(next(c.text for c in result.content if c.type == 'text'))


async def main():
    work = HERE/'runs'/('transport-'+uuid.uuid4().hex)
    work.mkdir(parents=True)
    fixture = codec.fixtures()[0]
    (work/'before.py').write_text(fixture['before'],encoding='utf-8')
    params = StdioServerParameters(command=sys.executable,args=[str(HERE/'server.py'),'--work',str(work),'--arm','compact'])
    async with stdio_client(params) as (reader,writer):
        async with ClientSession(reader,writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {'read_fixture','edit','native_fallback'}
            source = unpack(await session.call_tool('read_fixture',{}))
            payload = {**fixture['recipe'],'r':source['r']}
            response = unpack(await session.call_tool('edit',{'payload':payload}))
            assert response['ok']
            assert (work/'candidate.py').read_text(encoding='utf-8') == fixture['after']
            assert (work/'before.py').read_text(encoding='utf-8') == fixture['before']
            bad = unpack(await session.call_tool('edit',{'payload':{**payload,'r':'r404'}}))
            assert not bad['ok']
    result = dict(tool_discovery=True,receipt_roundtrip=True,exact_candidate=True,source_unchanged=True,
                  rejected_unknown_receipt=True,model_calls=0)
    (HERE/'transport_results.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__ == '__main__':
    asyncio.run(main())
