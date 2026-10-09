"""Flash Observations (next-turn form) on Codex WebSocket response.create frames."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from unittest.mock import patch

import pytest

from horizon.transforms.flash_openai import NOTICE
from tests.test_openai_codex_ws_lifecycle import (
    _DummyOpenAIHandler,
    _FakeUpstream,
    _FakeWebSocket,
    _make_fake_websockets_module,
)

LOG = "\n".join(f"tests/test_io.py::test_{i} PASSED" for i in range(800))


def _input(turns: int) -> list[dict]:
    items: list[dict] = [{"type": "message", "role": "user", "content": "run the tests"}]
    for t in range(turns):
        items += [
            {"type": "reasoning", "id": f"rs{t}", "summary": [], "encrypted_content": f"e{t}"},
            {"type": "function_call", "call_id": f"c{t}", "name": "shell", "arguments": "{}"},
            {"type": "function_call_output", "call_id": f"c{t}", "output": LOG + f"\n#{t}"},
        ]
    return items


def _frame(turns: int, **extra) -> str:
    return json.dumps(
        {
            "type": "response.create",
            "response": {"model": "gpt-6.1-sol", "input": _input(turns), **extra},
        }
    )


async def _run(frames: list[str]) -> list[dict]:
    upstream = _FakeUpstream([], hold_after_events=True)
    client_ws = _FakeWebSocket(
        frames=frames, hold_after_initial=True, disconnect_after_n_sends=None
    )
    handler = _DummyOpenAIHandler()
    handler.config.optimize = False

    async def _trigger() -> None:
        await asyncio.sleep(0.1)
        client_ws.trigger_disconnect()

    with patch.dict(sys.modules, {"websockets": _make_fake_websockets_module(upstream)}):
        trigger = asyncio.create_task(_trigger())
        try:
            await asyncio.wait_for(handler.handle_openai_responses_ws(client_ws), timeout=5.0)
        finally:
            trigger.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await trigger
    return [json.loads(raw)["response"] for raw in upstream.sent]


def _outputs(payload: dict) -> list[str]:
    return [i["output"] for i in payload["input"] if i.get("type") == "function_call_output"]


@pytest.fixture
def flash_on(monkeypatch, tmp_path):
    monkeypatch.setenv("HORIZON_FLASH_OPENAI", "1")
    monkeypatch.delenv("HORIZON_FLASH_ANY_UPSTREAM", raising=False)
    monkeypatch.setenv("HORIZON_WORKSPACE_DIR", str(tmp_path))


@pytest.mark.asyncio
async def test_full_input_frames_are_flashed(flash_on) -> None:
    sent = await _run([_frame(1), _frame(2)])
    assert len(sent) == 2
    first, second = (_outputs(p) for p in sent)
    assert first == [LOG + "\n#0" + NOTICE]
    assert second[0].startswith("[Horizon flash") and LOG not in second[0]
    assert second[1] == LOG + "\n#1" + NOTICE
    # Append-only: the second frame repeats the first up to its newest output.
    assert sent[1]["input"][: len(sent[0]["input"]) - 1] == sent[0]["input"][:-1]


@pytest.mark.asyncio
async def test_chained_frames_are_left_alone(flash_on) -> None:
    sent = await _run([_frame(1), _frame(2, previous_response_id="resp_1")])
    assert _outputs(sent[1]) == [LOG + "\n#0", LOG + "\n#1"]
