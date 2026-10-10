"""Cache keep-alive for Codex Responses WebSocket sessions (GPT-5.6+).

Measured 2026-10-10 on gpt-6.1-sol: on an open connection with ``store:
false``, a ``response.create`` chained to the last response with empty input
and ``prompt_cache_options.prewarm`` bills only cache reads; afterwards the
connection holds only the pre-warm's response, so the next turn must chain to
the pre-warm's id (the earlier id is ``previous_response_not_found``).
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from horizon.proxy.cache_keeper import OPENAI_RESPONSES_WS, TTL_30M, CacheKeeper
from horizon.proxy.ws_prewarm import PrewarmUnavailable, WsPrewarmChannel
from tests.test_openai_codex_ws_lifecycle import (
    _DummyOpenAIHandler,
    _FakeHeaders,
    _make_fake_websockets_module,
)

TOOLS = [{"type": "function", "name": "shell", "parameters": {"type": "object"}}]


def _turn(previous: str | None = None, text: str = "fix the bug") -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "gpt-6.1-sol",
        "store": False,
        "reasoning": {"effort": "medium"},
        "tools": TOOLS,
        "input": [{"role": "user", "content": text}],
    }
    if previous:
        body["previous_response_id"] = previous
    return body


def _frame(previous: str | None = None, text: str = "fix the bug") -> str:
    return json.dumps({"type": "response.create", **_turn(previous, text)})


def _completed(rid: str, read: int, write: int, uncached: int = 10, output: int = 0) -> dict:
    return {
        "type": "response.completed",
        "response": {
            "id": rid,
            "model": "gpt-6.1-sol",
            "usage": {
                "input_tokens": read + write + uncached,
                "input_tokens_details": {"cached_tokens": read, "cache_write_tokens": write},
                "output_tokens": output,
            },
        },
    }


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


# -- the channel on its own ------------------------------------------------


def test_prewarm_frame_chains_to_the_last_response_and_keeps_the_key() -> None:
    async def scenario() -> None:
        sent: list[str] = []

        async def send(raw: str) -> None:
            sent.append(raw)

        ch = WsPrewarmChannel(send)
        ch.client_turn_started()
        assert ch.observe_upstream_event(_completed("r1", 0, 9000)) is False  # the client's
        task = asyncio.create_task(ch.prewarm({**_turn("r0"), "stream": True}))
        await asyncio.sleep(0)
        frame = json.loads(sent[0])
        assert frame["type"] == "response.create"
        assert frame["previous_response_id"] == "r1" and frame["input"] == []
        assert frame["prompt_cache_options"] == {"prewarm": True}
        assert "stream" not in frame
        for key in ("model", "store", "reasoning", "tools"):
            assert frame[key] == _turn()[key]
        # Its events never reach the client.
        assert ch.observe_upstream_event({"type": "response.created"}) is True
        assert ch.observe_upstream_event(_completed("p1", 9000, 0)) is True
        status, usage = await task
        assert status == 200 and usage["input_tokens_details"]["cached_tokens"] == 9000

        # The next turn chains to the pre-warm, which holds the same context.
        ch.client_turn_started()
        out = json.loads(await ch.prepare_client_frame(_frame("r1", "next")))
        assert out["previous_response_id"] == "p1"
        assert out["input"] == [{"role": "user", "content": "next"}]
        # Other ids and other frames are left alone.
        assert json.loads(await ch.prepare_client_frame(_frame("zzz")))["previous_response_id"] == (
            "zzz"
        )
        assert await ch.prepare_client_frame('{"type":"response.cancel"}') == (
            '{"type":"response.cancel"}'
        )

    asyncio.run(scenario())


def test_never_prewarms_during_a_client_turn_or_after_close() -> None:
    async def scenario() -> None:
        async def send(raw: str) -> None:
            pass

        ch = WsPrewarmChannel(send)
        with pytest.raises(PrewarmUnavailable) as exc:
            await ch.prewarm(_turn())  # nothing answered yet
        assert exc.value.closed is False
        ch.observe_upstream_event(_completed("r1", 0, 9000))
        ch.client_turn_started()
        with pytest.raises(PrewarmUnavailable):
            await ch.prewarm(_turn())
        ch.observe_upstream_event({"type": "error", "error": {"code": "x"}})  # turn over
        ch.close()
        with pytest.raises(PrewarmUnavailable) as exc:
            await ch.prewarm(_turn())
        assert exc.value.closed is True

    asyncio.run(scenario())


def test_a_client_turn_waits_for_the_prewarm_and_never_for_long() -> None:
    async def scenario() -> None:
        async def send(raw: str) -> None:
            pass

        ch = WsPrewarmChannel(send, client_wait_seconds=0.05)
        ch.observe_upstream_event(_completed("r1", 0, 9000))
        task = asyncio.create_task(ch.prewarm(_turn()))
        await asyncio.sleep(0)
        ch.client_turn_started()
        out = json.loads(await ch.prepare_client_frame(_frame("r1")))
        # Unanswered: given up, booked as a miss, the turn goes out unchanged.
        assert out["previous_response_id"] == "r1"
        assert await task == (0, {})
        assert ch.observe_upstream_event({"type": "response.created"}) is False

    asyncio.run(scenario())


def test_a_failed_prewarm_keeps_the_clients_chain() -> None:
    async def scenario() -> None:
        async def send(raw: str) -> None:
            pass

        ch = WsPrewarmChannel(send)
        ch.observe_upstream_event(_completed("r1", 0, 9000))
        task = asyncio.create_task(ch.prewarm(_turn()))
        await asyncio.sleep(0)
        error = {"type": "error", "error": {"code": "previous_response_not_found"}}
        assert ch.observe_upstream_event(error) is True
        assert (await task)[0] == 400
        assert json.loads(await ch.prepare_client_frame(_frame("r1")))["previous_response_id"] == (
            "r1"
        )

    asyncio.run(scenario())


# -- the keeper drives it ----------------------------------------------------


class _Billing:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def __call__(self, owner: Any, record: dict[str, Any]) -> None:
        self.rows.append(record)


async def _http_sender(url, headers, body):  # pragma: no cover - never used for WS
    raise AssertionError("a WebSocket session is never pinged over HTTP")


def test_keeper_needs_a_channel_and_a_closed_one_is_released_unbilled() -> None:
    async def scenario() -> None:
        clock, billing = _Clock(), _Billing()
        keeper = CacheKeeper(_http_sender, clock=clock, reporter=billing, min_context=100)
        owner = SimpleNamespace(user_id="u1", compression_allowed=True)
        template = {k: v for k, v in _turn().items() if k != "input"}
        keeper.record_request(
            "a", url="wss://x", headers={}, body=template, flavor=OPENAI_RESPONSES_WS
        )
        assert "a" not in keeper._pending  # no connection to warm it on

        async def send(raw: str) -> None:
            pass

        ch = WsPrewarmChannel(send)
        ch.observe_upstream_event(_completed("r1", 0, 9000))
        keeper.record_request(
            "b",
            url="wss://x",
            headers={},
            body=template,
            flavor=OPENAI_RESPONSES_WS,
            channel=ch,
            liveness_id="live-1",
            owner=owner,
        )
        keeper.record_usage("b", model="gpt-6.1-sol", cache_read=0, cache_write=9000, uncached=10)
        ch.close()
        clock.t += TTL_30M - 100
        assert await keeper.tick() == 0
        assert billing.rows == [] and not next(iter(keeper._groups.values())).body

    asyncio.run(scenario())


# -- end to end through the relay ----------------------------------------------


class _ReactiveUpstream:
    """Answers each response.create the way the API does on one connection."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self._events: asyncio.Queue[str] = asyncio.Queue()
        self.response = SimpleNamespace(headers=_FakeHeaders([]))
        self.closed = False
        self._n = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    async def close(self) -> None:
        self.closed = True

    async def send(self, raw: str) -> None:
        frame = json.loads(raw)
        self.sent.append(frame)
        self._n += 1
        inner = frame.get("response", frame)
        prewarm = (inner.get("prompt_cache_options") or {}).get("prewarm")
        rid = f"p{self._n}" if prewarm else f"r{self._n}"
        events = [
            {"type": "response.created", "response": {"id": rid, "model": "gpt-6.1-sol"}},
            _completed(rid, 30_000, 0, output=0)
            if prewarm
            else _completed(rid, 0 if self._n == 1 else 30_000, 30_000 if self._n == 1 else 50),
        ]
        for event in events:
            self._events.put_nowait(json.dumps(event))

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        return await self._events.get()


class _ScriptedClient:
    def __init__(self) -> None:
        self.headers = {"authorization": "Bearer sk-test"}
        self.client = SimpleNamespace(host="127.0.0.1", port=1)
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.received: list[dict[str, Any]] = []

    async def accept(self, subprotocol=None, headers=None) -> None:
        pass

    async def receive_text(self) -> str:
        frame = await self.inbox.get()
        if frame is None:
            from tests.test_openai_codex_ws_lifecycle import _FakeWebSocketDisconnect

            raise _FakeWebSocketDisconnect("client closed")
        return frame

    async def send_text(self, text: str) -> None:
        self.received.append(json.loads(text))

    async def send_bytes(self, data: bytes) -> None:
        pass

    async def close(self, code=None, reason=None) -> None:
        pass


async def _until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


def test_an_idle_codex_session_is_warmed_on_its_connection() -> None:
    async def scenario() -> None:
        clock, billing = _Clock(), _Billing()
        handler = _DummyOpenAIHandler()
        handler.cache_keeper = CacheKeeper(
            _http_sender, clock=clock, reporter=billing, min_context=1000
        )
        upstream = _ReactiveUpstream()
        client = _ScriptedClient()
        client.inbox.put_nowait(_frame())
        with patch.dict(sys.modules, {"websockets": _make_fake_websockets_module(upstream)}):
            relay = asyncio.create_task(handler.handle_openai_responses_ws(client))

            def completed() -> list[str]:
                return [
                    e["response"]["id"]
                    for e in client.received
                    if e.get("type") == "response.completed"
                ]

            await _until(lambda: completed() == ["r1"])
            await _until(lambda: handler.cache_keeper._groups)

            # Idle until shortly before the 30-minute entry expires: pinged in place.
            clock.t += TTL_30M - 100
            assert await handler.cache_keeper.tick() == 1
            prewarm = upstream.sent[-1]
            assert prewarm["previous_response_id"] == "r1" and prewarm["input"] == []
            assert prewarm["prompt_cache_options"]["prewarm"] is True
            assert [e["response"]["id"] for e in client.received if "response" in e] == [
                "r1",
                "r1",
            ]  # Codex never saw the pre-warm
            assert billing.rows == []  # local proxy: no account to bill

            # Codex resumes on the id it knows; upstream gets the pre-warm's.
            client.inbox.put_nowait(_frame("r1", "and the tests"))
            await _until(lambda: len(completed()) == 2)
            assert upstream.sent[-1]["previous_response_id"] == "p2"
            assert upstream.sent[-1]["input"] == [{"role": "user", "content": "and the tests"}]

            client.inbox.put_nowait(None)
            await asyncio.wait_for(relay, 5)
        # The connection is gone, and with it the chain: nothing left to warm.
        assert all(not g.body for g in handler.cache_keeper._groups.values())

    asyncio.run(scenario())
