"""Mechanical validation only: synthetic qualification bounds are not a release profile."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from horizon.proxy.account_analytics import AccountMiddleware, account_id
from horizon.proxy.anthropic_wire import AnthropicSSEEnvelope
from horizon.proxy.compact_edits import (
    Candidate,
    CompactEditController,
    CompactEditError,
    CostBounds,
    NativeContract,
    Qualification,
    ReplayJournal,
    Scope,
    SourceSnapshot,
    compile_candidate,
    fingerprint,
)
from horizon.proxy.compact_edits.cohort import (
    ClaudeCohort,
    CompactCohortMiddleware,
    Reply,
    has_attested_read,
)

EDIT = {
    "name": "Edit",
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
        },
        "required": ["file_path", "old_string", "new_string"],
    },
}
SOURCE = Path(__file__).resolve().parents[2] / "horizon/pricing/anthropic_prices.py"


@pytest.fixture
def setup(tmp_path):
    scope = Scope("account-a", "conversation-a", "workspace-a", "anthropic", "model-a")
    candidate = Candidate(
        SourceSnapshot("catalog.py", "/workspace/catalog.py", SOURCE.read_text(encoding="utf-8")),
        "ANTHROPIC_PRICES",
        "claude-3-5-sonnet-latest",
        tuple(f"fixture-{i}" for i in range(4)),
    )
    contract = NativeContract("claude_edit", fingerprint(EDIT))
    qualification = Qualification(
        "test-only",
        scope.provider,
        scope.model,
        contract,
        scope.workspace,
        candidate.snapshot.path,
        candidate.snapshot.native_path,
        "horizon-model-pricing-literals-v1",
        "exact_full_source_match",
        time.time() + 600,
        CostBounds(0.1, 0.01),
    )
    controller = CompactEditController()
    with ReplayJournal(tmp_path / "journal.sqlite") as journal:
        request = {
            "model": scope.model,
            "tools": [EDIT],
            "messages": [{"role": "user", "content": "fixture"}],
            "stream": False,
            "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
        }
        yield scope, candidate, qualification, controller, journal, request


def admit(data):
    scope, candidate, qualification, controller, journal, request = data
    return controller.prepare(
        request,
        scope=scope,
        candidate=candidate,
        qualification=qualification,
        journal=journal,
        cold_boundary=True,
        execution_guard_ready=True,
    )


def response(turn, *, receipt=None):
    return {
        "type": "message",
        "role": "assistant",
        "model": turn.scope.model,
        "id": "msg_test",
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 11, "output_tokens": 7},
        "content": [
            {"type": "thinking", "thinking": "opaque", "signature": "signature-unchanged"},
            {
                "type": "tool_use",
                "id": "edit-1",
                "name": "horizon_compact_edit_v1",
                "input": {
                    "receipt": receipt or turn.receipt,
                    "table": turn.candidate.table,
                    "template": turn.candidate.template,
                    "keys": list(turn.candidate.keys),
                },
            },
        ],
    }


def test_compile_only_permitted_ast(setup):
    edit = compile_candidate(setup[1])
    assert edit.after.count("fixture-") == 8
    assert edit.patch.startswith("*** Begin Patch\n*** Update File: catalog.py\n")
    assert setup[1].snapshot.source == SOURCE.read_text(encoding="utf-8")


@pytest.mark.parametrize("count", [0, 1, 3, 17])
def test_unsupported_count(setup, count):
    with pytest.raises(CompactEditError):
        compile_candidate(replace(setup[1], keys=tuple(f"fixture-{i}" for i in range(count))))


@pytest.mark.parametrize("keys", [("same",) * 4, ("a", "b", "c", "claude-3-5-sonnet-latest")])
def test_duplicate_or_existing_keys(setup, keys):
    with pytest.raises(CompactEditError):
        compile_candidate(replace(setup[1], keys=keys))


def test_unknown_contract_native_passthrough(setup):
    data = list(setup)
    data[2] = replace(data[2], contract=replace(data[2].contract, definition_sha256="wrong"))
    assert admit(data).turn is None
    assert admit(data).request == setup[5]


def test_economics_native_passthrough(setup):
    data = list(setup)
    data[2] = replace(data[2], costs=CostBounds(0.01, 0.02))
    assert admit(data).turn is None


def test_translation_replay_usage_and_signatures(setup):
    admission = admit(setup)
    controller, journal = setup[3:5]
    original = response(admission.turn)
    native = controller.translate_response(original, admission.turn)
    assert native["content"][1]["name"] == "Edit"
    assert native["usage"] == original["usage"]
    assert native["content"][0] == original["content"][0]
    request = copy.deepcopy(setup[5])
    request["messages"].append({"role": "assistant", "content": native["content"]})
    restored = controller.normalize_replay(
        request, scope=setup[0], kind="claude_edit", journal=journal
    )
    assert restored["messages"][-1]["content"] == original["content"]
    assert request["messages"][-1]["content"][1]["name"] == "Edit"


def test_duplicate_publication_stops(setup):
    turn = admit(setup).turn
    setup[3].translate_response(response(turn), turn)
    with pytest.raises(CompactEditError, match="reserved"):
        setup[3].translate_response(response(turn), turn)


def test_bad_receipt_never_reserved(setup):
    turn = admit(setup).turn
    with pytest.raises(CompactEditError):
        setup[3].translate_response(response(turn, receipt="wrong"), turn)
    assert setup[4].get(setup[0].key, "admission")["reserved_call"] is None


def test_restart_and_tenant_isolation(setup):
    turn = admit(setup).turn
    setup[3].translate_response(response(turn), turn)
    restarted = CompactEditController()
    restored = restarted.restore_admission(setup[0], setup[4])
    assert restored.candidate == setup[1]
    assert restored.reserved_call == "edit-1"
    assert restarted.restore_admission(replace(setup[0], account="other-account"), setup[4]) is None


def test_modified_replay_stops(setup):
    turn = admit(setup).turn
    native = setup[3].translate_response(response(turn), turn)
    native["content"][1]["input"]["new_string"] = "malicious replacement"
    request = dict(setup[5], messages=[{"role": "assistant", "content": native["content"]}])
    with pytest.raises(CompactEditError, match="arguments differ"):
        setup[3].normalize_replay(request, scope=setup[0], kind="claude_edit", journal=setup[4])


def test_journal_capacity_native_passthrough(setup, tmp_path):
    with ReplayJournal(tmp_path / "small.sqlite", max_bytes=100) as small:
        data = list(setup)
        data[4] = small
        assert admit(data).turn is None
        assert small.get(setup[0].key, "admission") is None


@pytest.mark.parametrize("stream", [False, True])
def test_cohort_recovers_once_without_virtual_leak(setup, stream):
    scope, candidate, qualification, controller, journal, request = setup
    cohort = ClaudeCohort(
        controller=controller,
        scope=scope,
        candidate=candidate,
        qualification=qualification,
        journal=journal,
        cold_boundary_ready=True,
    )
    request = copy.deepcopy(request)
    request["messages"].extend(
        [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "read-1",
                        "name": "Read",
                        "input": {"file_path": candidate.snapshot.native_path},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "read-1",
                        "content": "\n".join(
                            f"{index}\t{line}"
                            for index, line in enumerate(candidate.snapshot.source.split("\n"), 1)
                        ),
                    }
                ],
            },
        ]
    )
    requests = []

    async def invoke(body):
        requests.append(body)
        virtual = body["tools"][-1]
        receipt = virtual["input_schema"]["properties"]["receipt"]["enum"][0]
        content = [
            {
                "type": "tool_use",
                "id": "native-1",
                "name": "Edit",
                "input": {
                    "file_path": candidate.snapshot.native_path,
                    "old_string": candidate.snapshot.source,
                    "new_string": compile_candidate(candidate).after,
                    "replace_all": False,
                },
            }
        ]
        if len(requests) == 1:
            content = [
                {
                    "type": "tool_use",
                    "id": "bad-1",
                    "name": "horizon_compact_edit_v1",
                    "input": {
                        "receipt": receipt + "bad",
                        "table": candidate.table,
                        "template": candidate.template,
                        "keys": list(candidate.keys),
                    },
                }
            ]
        message = {
            "type": "message",
            "role": "assistant",
            "id": "msg_test",
            "model": scope.model,
            "content": content,
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 20, "output_tokens": 10},
        }
        return Reply(200, [(b"content-type", b"application/json")], json.dumps(message).encode())

    result = asyncio.run(cohort.respond(dict(request, stream=stream), invoke))
    message = AnthropicSSEEnvelope.parse(result.body).message if stream else json.loads(result.body)
    assert message["content"][0]["name"] == "Edit"
    assert len(requests) == 2 and cohort.recoveries == 1
    assert len(cohort.attempt_usage) == 2
    assert requests[0]["tools"] == requests[1]["tools"]
    assert requests[1]["tool_choice"]["name"] == "Edit"


def test_middleware_no_resolver_native_stream_unchanged():
    output = []

    async def app(scope, receive, send):
        await send({"type": "http.response.body", "body": b"native-stream"})

    async def resolver(scope):
        return None

    async def receive():
        raise AssertionError("passthrough must not pre-read request")

    async def send(frame):
        output.append(frame)

    asyncio.run(
        CompactCohortMiddleware(app, resolve=resolver)(
            {"type": "http", "method": "POST", "path": "/v1/messages"}, receive, send
        )
    )
    assert output[0]["body"] == b"native-stream"


def test_account_authentication_runs_before_cohort_resolution():
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.routing import Route

    resolved = []

    async def authorize(key, required):
        return 200, {"user_id": "verified-account", "key_id": "verified-key"}

    async def resolve(scope):
        assert scope["state"]["account_user_id"] == "verified-account"
        assert account_id() == "verified-account"
        assert b"x-horizon-user-id" not in dict(scope["headers"])
        resolved.append(True)
        return None

    async def endpoint(request):
        return Response(b"native")

    app = Starlette(routes=[Route("/v1/messages", endpoint, methods=["POST"])])
    app.add_middleware(CompactCohortMiddleware, resolve=resolve)
    app.add_middleware(
        AccountMiddleware,
        service=SimpleNamespace(
            enabled=True,
            authorize=authorize,
            runtime_id="00000000-0000-0000-0000-000000000001",
            _enqueue=lambda event: None,
        ),
    )
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/messages",
        "query_string": b"",
        "headers": [(b"x-horizon-proxy-token", b"cs_live_test"), (b"x-horizon-user-id", b"forged")],
        "scheme": "http",
        "server": ("localhost", 80),
        "client": ("localhost", 1234),
    }

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(frame):
        pass

    asyncio.run(app(scope, receive, send))
    assert resolved == [True] and account_id() is None


def test_actual_result_acknowledged_and_changed_replay_stops(setup):
    turn = admit(setup).turn
    setup[3].translate_response(response(turn), turn)
    result = {"type": "tool_result", "tool_use_id": "edit-1", "content": "native success"}
    request = dict(setup[5], messages=[{"role": "user", "content": [result]}])
    setup[3].normalize_replay(request, scope=setup[0], kind="claude_edit", journal=setup[4])
    assert setup[4].get(setup[0].key, "call:edit-1")["native_result"]["sha256"]
    setup[3].normalize_replay(request, scope=setup[0], kind="claude_edit", journal=setup[4])
    result["content"] = "changed report"
    with pytest.raises(CompactEditError, match="result changed"):
        setup[3].normalize_replay(request, scope=setup[0], kind="claude_edit", journal=setup[4])


def test_uncertain_delivery_never_triggers_another_model_call(setup):
    turn = admit(setup).turn
    setup[3].translate_response(response(turn), turn)
    cohort = ClaudeCohort(
        controller=setup[3],
        scope=setup[0],
        candidate=setup[1],
        qualification=setup[2],
        journal=setup[4],
        cold_boundary_ready=True,
    )

    async def invoke(body):
        raise AssertionError("uncertain native delivery cannot cause another call")

    with pytest.raises(CompactEditError, match="no client result"):
        asyncio.run(cohort.respond(setup[5], invoke))


def test_partial_read_cannot_become_source_attestation(setup):
    candidate = setup[1]
    result = "\n".join(
        f"{i}\t{line}" for i, line in enumerate(candidate.snapshot.source.split("\n"), 1)
    )
    request = {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "read",
                        "name": "Read",
                        "input": {"file_path": candidate.snapshot.native_path},
                    }
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "read", "content": result}],
            },
        ]
    }
    assert has_attested_read(request, candidate)
    request["messages"][-1]["content"][0]["content"] = "\n".join(result.splitlines()[:20])
    assert not has_attested_read(request, candidate)


@pytest.mark.parametrize("kind", ["codex_custom_patch", "codex_function_patch"])
def test_codex_wire_mapping_isolated_from_execution(setup, kind):
    scope, candidate, qualification, controller, journal, request = setup
    if kind == "codex_custom_patch":
        definition = {
            "type": "custom",
            "name": "apply_patch",
            "format": {"type": "grammar", "syntax": "lark", "definition": "test-only"},
        }
    else:
        definition = {
            "type": "function",
            "name": "apply_patch",
            "parameters": {
                "type": "object",
                "properties": {"patch": {"type": "string"}},
                "required": ["patch"],
            },
        }
    scope = replace(scope, provider="openai")
    contract = NativeContract(kind, fingerprint(definition))
    qualification = replace(
        qualification, provider="openai", contract=contract, version_guard="atomic_client_sha256"
    )
    request = {
        "model": scope.model,
        "tools": [definition],
        "input": [],
        "stream": False,
        "parallel_tool_calls": False,
    }
    turn = controller.prepare(
        request,
        scope=scope,
        candidate=candidate,
        qualification=qualification,
        journal=journal,
        cold_boundary=True,
        execution_guard_ready=True,
    ).turn
    item = {
        "type": "function_call",
        "id": "fc_test",
        "call_id": "call_test",
        "name": "horizon_compact_edit_v1",
        "arguments": json.dumps(response(turn)["content"][1]["input"]),
    }
    message = {
        "object": "response",
        "status": "completed",
        "output": [item],
        "usage": {"output_tokens": 5},
    }
    native = controller.translate_response(message, turn)["output"][0]
    result = {
        "type": "custom_tool_call_output"
        if kind == "codex_custom_patch"
        else "function_call_output",
        "call_id": "call_test",
        "output": "actual client report",
    }
    restored = controller.normalize_replay(
        dict(request, input=[native, result]), scope=scope, kind=kind, journal=journal
    )
    assert restored["input"][0] == item
    assert restored["input"][1]["type"] == "function_call_output"
    assert restored["input"][1]["output"] == "actual client report"
    assert journal.get(scope.key, "call:call_test")["native_result"]


def test_codex_unready_guard_never_admits(setup):
    scope, candidate, qualification, controller, journal, request = setup
    admission = controller.prepare(
        request,
        scope=scope,
        candidate=candidate,
        qualification=qualification,
        journal=journal,
        cold_boundary=True,
        execution_guard_ready=False,
    )
    assert admission.turn is None and admission.request == request


def read_history(candidate):
    return [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "read",
                    "name": "Read",
                    "input": {"file_path": candidate.snapshot.native_path},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "read",
                    "content": "\n".join(
                        f"{i}\t{line}"
                        for i, line in enumerate(candidate.snapshot.source.split("\n"), 1)
                    ),
                }
            ],
        },
    ]


def test_missing_journal_does_not_prove_a_cold_boundary(setup):
    cohort = ClaudeCohort(
        controller=setup[3],
        scope=setup[0],
        candidate=setup[1],
        qualification=setup[2],
        journal=setup[4],
    )
    calls = []

    async def invoke(body):
        calls.append(body)
        return Reply(200, [], b"native-unchanged")

    assert asyncio.run(cohort.respond(setup[5], invoke)).body == b"native-unchanged"
    assert calls == [setup[5]] and setup[4].get(setup[0].key, "admission") is None
    cohort.resume_required = True
    with pytest.raises(CompactEditError, match="lost its replay journal"):
        asyncio.run(cohort.respond(setup[5], invoke))
    assert len(calls) == 1


def test_unsupported_history_does_not_leave_admission(setup):
    data = list(setup)
    data[5] = dict(data[5], messages="unsupported")
    assert admit(data).turn is None
    assert setup[4].get(setup[0].key, "admission") is None


def test_new_partial_or_pending_read_supersedes_old_attestation(setup):
    history = read_history(setup[1])
    assert has_attested_read({"messages": history}, setup[1])
    history.append(
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "new-read",
                    "name": "Read",
                    "input": {"file_path": setup[1].snapshot.native_path, "offset": 20},
                }
            ],
        }
    )
    assert not has_attested_read({"messages": history}, setup[1])


def test_upstream_model_mismatch_never_publishes(setup):
    cohort = ClaudeCohort(
        controller=setup[3],
        scope=setup[0],
        candidate=setup[1],
        qualification=setup[2],
        journal=setup[4],
        cold_boundary_ready=True,
    )

    async def invoke(body):
        message = {"model": "unqualified-model", "content": [], "usage": {"output_tokens": 1}}
        return Reply(200, [], json.dumps(message).encode())

    with pytest.raises(CompactEditError, match="upstream model"):
        asyncio.run(cohort.respond(setup[5], invoke))
    assert len(cohort.attempt_usage) == 1
    assert setup[4].get(setup[0].key, "admission")["reserved_call"] is None


@pytest.mark.parametrize("mode", ["no-read", "exhausted"])
def test_bounded_recovery_without_read_and_exhaustion(setup, mode):
    cohort = ClaudeCohort(
        controller=setup[3],
        scope=setup[0],
        candidate=setup[1],
        qualification=setup[2],
        journal=setup[4],
        cold_boundary_ready=True,
    )
    calls = []

    async def invoke(body):
        calls.append(body)
        turn = setup[3].restore_admission(setup[0], setup[4])
        receipt = body["tools"][-1]["input_schema"]["properties"]["receipt"]["enum"][0]
        content = [
            {
                "type": "tool_use",
                "id": "virtual",
                "name": "horizon_compact_edit_v1",
                "input": {
                    "receipt": receipt,
                    "table": turn.candidate.table,
                    "template": turn.candidate.template,
                    "keys": list(turn.candidate.keys),
                },
            }
        ]
        if len(calls) == 2 and mode == "no-read":
            content = [
                {
                    "type": "tool_use",
                    "id": "native-read",
                    "name": "Read",
                    "input": {"file_path": setup[1].snapshot.native_path},
                }
            ]
        message = {
            "type": "message",
            "model": setup[0].model,
            "role": "assistant",
            "id": "msg",
            "stop_reason": "tool_use",
            "content": content,
            "usage": {"output_tokens": 3},
        }
        return Reply(200, [], json.dumps(message).encode())

    if mode == "exhausted":
        with pytest.raises(CompactEditError, match="native recovery emitted"):
            asyncio.run(cohort.respond(setup[5], invoke))
    else:
        reply = asyncio.run(cohort.respond(setup[5], invoke))
        assert json.loads(reply.body)["content"][0]["name"] == "Read"
    assert len(calls) == 2 and len(cohort.attempt_usage) == 2
    assert calls[1]["tool_choice"]["name"] == "Read"
    assert setup[4].get(setup[0].key, "admission")["reserved_call"] is None


@pytest.mark.parametrize("mode", ["tenant-mismatch", "safeguards", "beta", "oversized", "gzip"])
def test_middleware_unsupported_native_payload_preserved(setup, mode):
    cohort = ClaudeCohort(
        controller=setup[3],
        scope=setup[0],
        candidate=setup[1],
        qualification=setup[2],
        journal=setup[4],
        cold_boundary_ready=True,
    )
    request = copy.deepcopy(setup[5])
    headers = []
    if mode == "safeguards":
        request["safeguards"] = {"opaque": "unchanged"}
    if mode == "beta":
        headers.append((b"anthropic-beta", b"dangerous-tool-use-2026-02-01"))
    if mode == "gzip":
        headers.append((b"content-encoding", b"gzip"))
    payload = json.dumps(request).encode() if mode != "oversized" else b"x" * 2_000_001
    frames = [
        {"type": "http.request", "body": payload[:10], "more_body": True},
        {"type": "http.request", "body": payload[10:], "more_body": False},
    ]
    received, output = [], []

    async def receive():
        return frames.pop(0)

    async def send(frame):
        output.append(frame)

    async def resolve(scope):
        return cohort

    async def app(scope, receive, send):
        while True:
            frame = await receive()
            received.append(frame["body"])
            if not frame.get("more_body"):
                break
        await send({"type": "http.response.body", "body": b"native"})

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/messages",
        "headers": headers,
        "state": {"account_user_id": "other" if mode == "tenant-mismatch" else setup[0].account},
    }
    asyncio.run(CompactCohortMiddleware(app, resolve=resolve)(scope, receive, send))
    assert b"".join(received) == payload and output[0]["body"] == b"native"
    assert setup[4].get(setup[0].key, "admission") is None


@pytest.mark.parametrize("stream", [False, True])
def test_authenticated_middleware_translates_only_client_view(setup, stream):
    cohort = ClaudeCohort(
        controller=setup[3],
        scope=setup[0],
        candidate=setup[1],
        qualification=setup[2],
        journal=setup[4],
        cold_boundary_ready=True,
    )
    request = copy.deepcopy(setup[5])
    request["stream"] = stream
    request["messages"].extend(read_history(setup[1]))
    payload = json.dumps(request).encode()
    output, forwarded = [], []

    async def resolve(scope):
        return cohort

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(frame):
        output.append(frame)

    async def app(scope, receive, send):
        body = json.loads((await receive())["body"])
        forwarded.append(body)
        assert body["stream"] is False
        assert (
            dict(scope["headers"])[b"content-length"]
            == str(len(json.dumps(body, ensure_ascii=False).encode())).encode()
        )
        admission = setup[3].prepare(
            body | {"tools": [EDIT]},
            scope=setup[0],
            candidate=setup[1],
            qualification=setup[2],
            journal=setup[4],
            cold_boundary=False,
            execution_guard_ready=True,
        )
        message = response(admission.turn)
        encoded = json.dumps(message).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": encoded[:20], "more_body": True})
        await send({"type": "http.response.body", "body": encoded[20:], "more_body": False})

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/messages",
        "state": {"account_user_id": setup[0].account},
        "headers": [(b"content-length", str(len(payload)).encode())],
    }
    asyncio.run(CompactCohortMiddleware(app, resolve=resolve)(scope, receive, send))
    assert output[0]["status"] == 200 and len(forwarded) == 1
    wire = output[1]["body"]
    message = AnthropicSSEEnvelope.parse(wire).message if stream else json.loads(wire)
    assert message["content"][1]["name"] == "Edit"
    assert forwarded[0]["tools"][-1]["name"] == "horizon_compact_edit_v1"
    assert message["usage"] == {"input_tokens": 11, "output_tokens": 7}
