from __future__ import annotations

import multiprocessing
import threading
import time
import uuid
from typing import Any

import pytest

from horizon.desktop_mcp.broker_transport import (
    BrokerTransportError,
    _validate_operation_args,
    current_user_sid,
    decode_frame,
    exchange,
    pipe_name_for,
    serve_pipe,
)


def _pipe_server(name: str, sid: str, stop: Any) -> None:
    serve_pipe(
        name,
        lambda operation, args: {"operation": operation, "args": args},
        stop_event=stop,
        timeout_seconds=4,
        expected_sid=sid,
    )


def _pipe_client(name: str, output: Any, operation: str) -> None:
    try:
        output.put(exchange(name, operation, {}, timeout_seconds=4))
    except BrokerTransportError as exc:
        output.put({"error": exc.code})


def test_request_decoder_has_closed_keys_and_no_role_field() -> None:
    assert (
        decode_frame(b'{"operation":"stats","args":{},"request_id":"request-1"}')["operation"]
        == "stats"
    )
    for frame in (
        b'{"operation":"stats","args":{},"request_id":"r","role":"admin"}',
        b'{"operation":"delete","args":{},"request_id":"r"}',
    ):
        with pytest.raises(BrokerTransportError):
            request = decode_frame(frame)
            if request["operation"] not in {"search", "open", "retrieve", "stats"}:
                raise BrokerTransportError("operation_unavailable")


def test_transport_accepts_legacy_and_extended_exact_retrieval_shapes() -> None:
    legacy_open = {"source": "cs1_example", "start_line": None, "end_line": None}
    _validate_operation_args("open", legacy_open)
    for mode in ("exact", "compact", "focused", "overview"):
        _validate_operation_args(
            "open",
            {
                **legacy_open,
                "mode": mode,
                "query": "renewal" if mode == "focused" else None,
                "result_token_budget": 512,
                "cursor": "opaque-cursor",
            },
        )

    legacy_retrieve = {"source_id": "cs1_example", "start_line": 1, "end_line": 3}
    _validate_operation_args("retrieve", legacy_retrieve)
    selectors = (
        {"type": "lines", "start": 1, "end": 3},
        {"type": "markdown_section", "heading": "Renewal", "occurrence": 2},
        {"type": "json_pointer", "pointer": "/items/0/name"},
        {"type": "rows", "start": 2, "end": 5},
    )
    for selector in selectors:
        _validate_operation_args(
            "retrieve",
            {
                "source_id": "cs1_example",
                "selector": selector,
                "result_token_budget": 512,
                "cursor": "opaque-cursor",
            },
        )


def test_transport_rejects_unknown_fields_malformed_selectors_and_unbounded_cursor() -> None:
    bad_requests = (
        ("open", {"source": "cs1_x", "start_line": None, "end_line": None, "role": "admin"}),
        (
            "retrieve",
            {
                "source_id": "cs1_x",
                "selector": {"type": "lines", "start": 1, "end": 2, "path": "x"},
            },
        ),
        (
            "retrieve",
            {"source_id": "cs1_x", "selector": {"type": "json_pointer", "pointer": "a/b"}},
        ),
        (
            "open",
            {"source": "cs1_x", "start_line": None, "end_line": None, "cursor": "c" * 257},
        ),
    )
    for operation, args in bad_requests:
        with pytest.raises(BrokerTransportError) as caught:
            _validate_operation_args(operation, args)
        assert caught.value.code == "invalid_arguments"


def test_oversized_prefix_rejected_before_reading_body(monkeypatch: pytest.MonkeyPatch) -> None:
    import horizon.desktop_mcp.broker_transport as transport

    reads: list[int] = []

    def read_exact(pipe: object, size: int, deadline: float) -> bytes:
        reads.append(size)
        if size == 8:
            return b"CSM\x01" + (transport.MAX_FRAME_BYTES + 1).to_bytes(4, "big")
        raise AssertionError("oversized body must not be read")

    monkeypatch.setattr(transport, "_read_exact", read_exact)
    monkeypatch.setattr(transport, "_client_sid", lambda _pipe: "S-1-test")
    with pytest.raises(BrokerTransportError) as caught:
        transport._read_request(object(), 1, "S-1-test")
    assert caught.value.code == "frame_too_large"
    assert reads == [8]


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows named pipe transport")
def test_two_independent_process_clients_share_bounded_named_pipe() -> None:
    context = multiprocessing.get_context("spawn")
    sid = current_user_sid()
    name = pipe_name_for(sid, f"test-{uuid.uuid4().hex[:12]}")
    stop = context.Event()
    output = context.Queue()
    server = context.Process(target=_pipe_server, args=(name, sid, stop))
    server.start()
    clients = [context.Process(target=_pipe_client, args=(name, output, "stats")) for _ in range(2)]
    for client in clients:
        client.start()
    try:
        for client in clients:
            client.join(8)
            assert client.exitcode == 0
        results = [output.get(timeout=2) for _ in clients]
        assert results == [
            {"operation": "stats", "args": {}},
            {"operation": "stats", "args": {}},
        ]
    finally:
        stop.set()
        server.join(3)
        if server.is_alive():
            server.terminate()
            server.join(2)
        for client in clients:
            if client.is_alive():
                client.terminate()
                client.join(2)


def test_pipe_name_is_deterministic_and_broker_scoped() -> None:
    assert pipe_name_for("S-1-user", "desktop") == pipe_name_for("S-1-user", "desktop")
    assert pipe_name_for("S-1-user", "desktop") != pipe_name_for("S-1-user", "other")


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows named pipe transport")
def test_one_pipe_instance_handles_repeated_requests_without_rebinding() -> None:
    sid = current_user_sid()
    name = pipe_name_for(sid, f"test-{uuid.uuid4().hex[:12]}")
    stop = threading.Event()
    server = threading.Thread(
        target=serve_pipe,
        args=(name, lambda operation, args: {"operation": operation, "args": args}),
        kwargs={"stop_event": stop, "timeout_seconds": 2, "expected_sid": sid},
        daemon=True,
    )
    server.start()
    try:
        deadline = time.monotonic() + 3
        successes = 0
        while successes < 100 and time.monotonic() < deadline:
            result = exchange(name, "stats", {}, timeout_seconds=2)
            assert result == {"operation": "stats", "args": {}}
            successes += 1
        assert successes == 100
    finally:
        stop.set()
        server.join(3)
    assert not server.is_alive()
