"""Zero-network checks through create_app; synthetic qualifications only."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import socket
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from horizon.proxy.account_analytics import AccountAnalytics
from horizon.proxy.anthropic_wire import AnthropicSSEEnvelope
from horizon.proxy.compact_edits import (
    CompactEditController,
    CostBounds,
    NativeContract,
    Qualification,
    ReplayJournal,
    Scope,
    compile_candidate,
    fingerprint,
)
from horizon.proxy.compact_edits.cohort import ClaudeCohort
from horizon.proxy.compact_edits.registry import ManagedClaudeRegistry, managed_candidate
from horizon.proxy.compact_edits.wire import VIRTUAL_TOOL
from horizon.proxy.server import ProxyConfig, create_app

EDIT = {
    "name": "Edit",
    "description": "Replace exact source text",
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
MODEL = "claude-3-5-sonnet-latest"
SOURCE = Path(__file__).resolve().parents[2] / "horizon/pricing/anthropic_prices.py"


def message(content=None, *, index=1):
    return {
        "id": f"msg_{index}",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": content or [{"type": "text", "text": "complete"}],
        "stop_reason": "tool_use" if content else "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 10,
            "cache_read_input_tokens": 20,
            "cache_creation_input_tokens": 0,
        },
    }


class Harness:
    def __init__(self, tmp_path, monkeypatch, *, optimize=True, cache=True, identities=None):
        original_connect = socket.socket.connect

        def forbid_external_connect(sock, address):
            if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1"):
                raise AssertionError("External sockets are forbidden in this test")
            return original_connect(sock, address)

        monkeypatch.setattr(socket.socket, "connect", forbid_external_connect)
        import horizon.proxy.identity as identity

        monkeypatch.setattr(identity, "_resolver", None)
        self.user, self.key_id, self.other_user = identities or (
            str(uuid4()),
            str(uuid4()),
            str(uuid4()),
        )
        self.allowed, self.auth_status = True, 200
        self.account = AccountAnalytics()
        self.account.enabled = True
        self.account.path = tmp_path / "outbox.sqlite"
        with self.account._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS outbox(event_id TEXT PRIMARY KEY,payload TEXT NOT NULL)"
            )
        self.authorized = []

        async def authorize(key, required_scope):
            self.authorized.append((key, required_scope))
            if self.auth_status != 200:
                return self.auth_status, None
            return 200, {
                "user_id": self.other_user if key == "cs_live_other" else self.user,
                "key_id": self.key_id,
                "compression_allowed": self.allowed,
            }

        self.account.authorize = authorize
        monkeypatch.setattr(
            "horizon.proxy.account_analytics.AccountAnalytics", lambda: self.account
        )
        # App lifespan/background services are intentionally not started. All
        # inference, compression, prefix/cache and outcome code runs normally.
        monkeypatch.setattr("horizon.proxy.server._setup_file_logging", lambda *a, **kw: None)
        monkeypatch.setenv("HORIZON_BEACON", "off")
        monkeypatch.setenv("HORIZON_TOOL_SEARCH", "0")
        monkeypatch.setenv("HORIZON_TELEMETRY", "0")
        monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        self.app = create_app(
            ProxyConfig(
                optimize=optimize,
                cache_enabled=cache,
                rate_limit_enabled=False,
                cost_tracking_enabled=False,
                log_requests=False,
                image_optimize=False,
                periodic_toin_stats_enabled=False,
            )
        )
        self.proxy = self.app.state.proxy
        # Make the arithmetic oracle independent of LiteLLM's installed price
        # map/version. These rates match the isolated source fixture; only the
        # lookup is scripted, the actual settlement/outbox path is unchanged.
        from horizon.proxy import savings_tracker

        monkeypatch.setattr(
            savings_tracker,
            "litellm",
            SimpleNamespace(
                model_cost={
                    MODEL: {
                        "input_cost_per_token": 3 / 1_000_000,
                        "output_cost_per_token": 15 / 1_000_000,
                        "cache_read_input_token_cost": 0.3 / 1_000_000,
                    }
                },
                cost_per_token=lambda **kw: (0, 0),
            ),
        )
        monkeypatch.setattr(savings_tracker, "_resolve_litellm_model", lambda model: model)
        self.controller = self.proxy.compact_edits
        self.scope = Scope(self.user, "managed-session", "managed-workspace", "anthropic", MODEL)
        source_bytes = SOURCE.read_text(encoding="utf-8").encode()
        self.candidate = managed_candidate(
            source_bytes=source_bytes,
            expected_sha256=hashlib.sha256(source_bytes).hexdigest(),
            provenance="isolated-fixture-collector",
            path="catalog.py",
            native_path="/workspace/catalog.py",
            table="ANTHROPIC_PRICES",
            template=MODEL,
            keys=tuple(f"fixture-{i}" for i in range(4)),
        )
        self.qualification = Qualification(
            "synthetic-zero-cost-not-certified",
            "anthropic",
            MODEL,
            NativeContract("claude_edit", fingerprint(EDIT)),
            self.scope.workspace,
            self.candidate.snapshot.path,
            self.candidate.snapshot.native_path,
            "horizon-model-pricing-literals-v1",
            "exact_full_source_match",
            time.time() + 600,
            CostBounds(0.1, 0.01),
        )
        self.journal_path = tmp_path / "replay.sqlite"
        self.journal = ReplayJournal(self.journal_path)
        self.registry = ManagedClaudeRegistry(
            controller=self.controller, journal=self.journal, worker_processes=1
        )
        self.cohort = self.registry.bind(
            scope=self.scope,
            candidate=self.candidate,
            qualification=self.qualification,
            request_path="/p/fixture/v1/messages",
            cold_boundary_ready=True,
        )
        self.resolved, self.calls, self.answers = [], [], []
        self.delay_seconds = 0

        async def resolve(scope):
            self.resolved.append(scope.get("state", {}).get("account_user_id"))
            return await self.registry.resolve(scope)

        self.app.state.compact_edit_cohort_resolver = resolve

        async def provider(request):
            assert request.url.host == "api.anthropic.com", "Unexpected egress"
            body = json.loads(request.content)
            self.calls.append((body, dict(request.headers)))
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            answer = self.answers.pop(0) if self.answers else message(index=len(self.calls))
            if callable(answer):
                answer = answer(body)
            if isinstance(answer, httpx.Response):
                return answer
            return httpx.Response(
                200, json=answer, headers={"request-id": f"upstream-{len(self.calls)}"}
            )

        self.proxy.http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(provider), trust_env=False
        )
        self.proxy.http_client_h1 = self.proxy.http_client
        self.client = TestClient(self.app)

    def new_cohort(self, *, resume=False):
        return ClaudeCohort(
            controller=self.controller,
            scope=self.scope,
            candidate=self.candidate,
            qualification=self.qualification,
            journal=self.journal,
            cold_boundary_ready=not resume,
            resume_required=resume,
        )

    def request(self, *, read=True, stream=False):
        history = [{"role": "user", "content": "Add the four fixture pricing rows."}]
        if read:
            numbered = "\n".join(
                f"{i}\t{line}"
                for i, line in enumerate(self.candidate.snapshot.source.splitlines(), 1)
            )
            history.extend(
                [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "read-1",
                                "name": "Read",
                                "input": {"file_path": self.candidate.snapshot.native_path},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "read-1", "content": numbered}
                        ],
                    },
                ]
            )
        return {
            "model": MODEL,
            "max_tokens": 4096,
            "tools": [copy.deepcopy(EDIT)],
            "messages": history,
            "stream": stream,
        }

    def compact(self, body, *, bad=False):
        tool = next(t for t in body["tools"] if t["name"] == VIRTUAL_TOOL)
        props = tool["input_schema"]["properties"]
        args = {k: (v["items"]["enum"] if k == "keys" else v["enum"][0]) for k, v in props.items()}
        if bad:
            args["receipt"] = "invalid"
        return message(
            [{"type": "tool_use", "id": "compact-1", "name": VIRTUAL_TOOL, "input": args}]
        )

    def post(self, body=None, *, headers=None, path="/p/fixture/v1/messages"):
        return self.client.post(
            path,
            json=body or self.request(),
            headers={
                "x-horizon-proxy-token": "cs_live_fixture",
                "x-api-key": "dummy-provider",
                "anthropic-version": "2023-06-01",
                "x-horizon-session-id": self.scope.conversation,
                **(headers or {}),
            },
        )

    def events(self):
        return [json.loads(row[1]) for row in self.account._batch()]

    def close(self):
        self.client.close()
        asyncio.run(self.proxy.http_client.aclose())
        self.journal.close()
        # No lifespan resources were started; release constructor executors.
        self.proxy._background_compression_executor.shutdown(wait=False)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    value = Harness(tmp_path, monkeypatch)
    yield value
    value.close()


@pytest.mark.parametrize("stream", [False, True])
def test_full_pipeline_expansion_and_account_usage(harness, stream):
    h = harness
    h.answers = [h.compact]
    reply = h.post(h.request(stream=stream), path="/p/fixture/v1/messages")
    assert reply.status_code == 200, reply.text
    client = AnthropicSSEEnvelope.parse(reply.content).message if stream else reply.json()
    native = client["content"][0]
    assert native["name"] == "Edit"
    assert native["input"]["new_string"] == compile_candidate(h.candidate).after
    assert h.resolved == [h.user]
    assert h.calls[0][0]["stream"] is False
    assert VIRTUAL_TOOL not in reply.text
    assert all(k not in h.calls[0][1] for k in ["x-horizon-proxy-token", "x-horizon-user-id"])
    events = h.events()
    assert len(events) == 1
    assert events[0]["user_id"] == h.user
    assert events[0]["project"] == "fixture"
    assert events[0]["tokens_out"] == 10
    assert events[0]["cache_read"] == 20
    assert events[0]["cost_usd"] == pytest.approx((100 * 3 + 20 * 0.3 + 10 * 15) / 1_000_000)
    assert h.proxy.compact_edits.stats()["credited_savings_usd"] == 0


def test_rejected_private_preview_and_recovery_both_settle(harness):
    h = harness
    h.answers = [
        lambda b: h.compact(b, bad=True),
        message(
            [
                {
                    "type": "tool_use",
                    "id": "native-1",
                    "name": "Edit",
                    "input": {
                        "file_path": "/workspace/catalog.py",
                        "old_string": "a",
                        "new_string": "b",
                    },
                }
            ],
            index=2,
        ),
    ]
    reply = h.post()
    assert reply.status_code == 200, reply.text
    assert reply.json()["content"][0]["name"] == "Edit"
    assert len(h.calls) == len(h.events()) == 2
    assert len({e["event_id"] for e in h.events()}) == 2
    assert sum(e["cost_usd"] for e in h.events()) == pytest.approx(2 * 0.000456)
    assert h.calls[1][0]["tool_choice"]["name"] == "Edit"
    assert h.calls[0][0]["messages"] == h.calls[1][0]["messages"]
    assert h.journal.get(h.scope.key, "admission")["reserved_call"] is None


def test_recovery_failure_keeps_both_successful_provider_attempts(harness):
    h = harness
    h.answers = [lambda b: h.compact(b, bad=True), h.compact]
    reply = h.post()
    assert reply.status_code == 502
    assert VIRTUAL_TOOL not in reply.text
    assert len(h.calls) == len(h.events()) == 2
    assert all(e["status"] == 200 and e["cost_usd"] > 0 for e in h.events())


@pytest.mark.parametrize("status", [401, 403, 503])
def test_authentication_precedes_registry_and_provider(harness, status):
    h = harness
    h.auth_status = status
    assert h.post().status_code == status
    assert not h.resolved and not h.calls and not h.events()


def test_other_account_cannot_select_candidate(harness):
    h = harness
    assert (
        h.post(
            headers={"x-horizon-proxy-token": "cs_live_other", "x-horizon-user-id": h.user}
        ).status_code
        == 200
    )
    assert VIRTUAL_TOOL not in json.dumps(h.calls[0][0]["tools"])
    assert h.events()[0]["user_id"] == h.other_user
    assert h.journal.get(h.scope.key, "admission") is None


def test_response_cache_stores_provider_view_and_never_reexecutes(harness):
    h = harness
    h.answers = [h.compact]
    body = h.request()
    first = h.post(body)
    assert first.status_code == 200
    entries = list(h.proxy.cache._cache.values())
    assert len(entries) == 1
    cached = json.loads(entries[0].response_body)
    assert cached["content"][0]["name"] == VIRTUAL_TOOL
    # An unanswered published call cannot be re-delivered from response cache.
    assert h.post(body).status_code == 502
    assert len(h.calls) == 1


def test_restart_normalizes_actual_native_result_before_prefix_cache(harness):
    h = harness
    h.answers = [h.compact]
    body = h.request()
    first = h.post(body)
    assert first.status_code == 200
    body["messages"].extend(
        [
            {"role": "assistant", "content": first.json()["content"]},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "compact-1",
                        "content": "permission denied",
                        "is_error": True,
                    }
                ],
            },
        ]
    )
    h.journal.close()
    h.journal = ReplayJournal(h.journal_path)
    h.controller = CompactEditController()
    restored = h.controller.restore_admission(h.scope, h.journal)
    assert restored.reserved_call == "compact-1"
    h.candidate = restored.candidate
    h.registry = ManagedClaudeRegistry(
        controller=h.controller, journal=h.journal, worker_processes=1
    )
    h.cohort = h.registry.restore(scope=h.scope, qualification=h.qualification)
    assert h.post(body).status_code == 200
    provider_call = h.calls[-1][0]["messages"][-2]["content"][0]
    assert provider_call["name"] == VIRTUAL_TOOL
    assert h.calls[-1][0]["messages"][-1]["content"][0]["is_error"] is True
    assert h.journal.get(h.scope.key, "call:compact-1")["native_result"]["is_error"] is True
    trackers = h.proxy.session_tracker_store._trackers.values()
    assert any(VIRTUAL_TOOL in json.dumps(t.get_last_original_messages()) for t in trackers)
    # A identical continuation is cache-served without another upstream charge.
    assert h.post(body).status_code == 200
    assert len(h.calls) == 2
    assert len(h.events()) == 3
    assert h.events()[-1]["response_cached"] is True
    assert h.events()[-1]["cost_usd"] == 0


@pytest.mark.parametrize("policy", ["cap", "bypass", "passthrough", "disabled"])
def test_existing_proxy_policy_prevents_new_compact_admission(harness, policy):
    h = harness
    headers = {}
    if policy == "cap":
        h.allowed = False
    elif policy == "disabled":
        h.proxy.config.optimize = False
    else:
        headers["x-horizon-bypass" if policy == "bypass" else "x-horizon-mode"] = (
            "true" if policy == "bypass" else "passthrough"
        )
    assert h.post(headers=headers).status_code == 200
    assert VIRTUAL_TOOL not in json.dumps(h.calls[0][0]["tools"])
    assert h.journal.get(h.scope.key, "admission") is None


@pytest.mark.parametrize("change", ["cap", "disabled", "bypass", "route", "bad_json"])
def test_active_catalog_never_disappears_on_policy_or_route_change(harness, change):
    h = harness
    assert h.post().status_code == 200
    headers = {}
    if change == "cap":
        h.allowed = False
    elif change == "disabled":
        h.proxy.config.optimize = False
    elif change == "bypass":
        headers["x-horizon-bypass"] = "true"
    elif change == "route":
        headers["x-horizon-base-url"] = "https://example.invalid"
    if change == "bad_json":
        reply = h.client.post(
            "/p/fixture/v1/messages",
            content=b"{",
            headers={
                "x-horizon-proxy-token": "cs_live_fixture",
                "x-horizon-session-id": h.scope.conversation,
            },
        )
    else:
        reply = h.post(headers=headers)
    assert reply.status_code == 502
    assert len(h.calls) == 1


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"input_tokens": -1, "output_tokens": 10},
        {"input_tokens": 10, "output_tokens": True},
    ],
)
def test_missing_or_invalid_usage_never_publishes_compact_call(harness, value):
    h = harness

    def invalid_usage(body):
        result = h.compact(body)
        result["usage"] = value
        return result

    h.answers = [invalid_usage]
    reply = h.post()
    assert reply.status_code == 502
    assert VIRTUAL_TOOL not in reply.text
    assert h.journal.get(h.scope.key, "admission")["reserved_call"] is None


def test_entire_request_timeout_is_bounded(harness):
    h = harness
    h.delay_seconds = 0.1
    h.cohort.request_timeout_seconds = 0.01
    assert h.post().status_code == 504
    assert len(h.calls) <= 1
    assert h.journal.get(h.scope.key, "admission")["reserved_call"] is None
    assert not h.cohort.lock.locked()


def test_response_buffer_bound_never_exposes_partial_tool(harness):
    h = harness

    def oversized(body):
        result = h.compact(body)
        result["content"].insert(0, {"type": "text", "text": "x" * 4_000_001})
        return result

    h.answers = [oversized]
    reply = h.post()
    assert reply.status_code == 502
    assert len(reply.content) < 1000
    assert h.journal.get(h.scope.key, "admission")["reserved_call"] is None
    assert len(h.events()) == 1


def test_operator_install_checks_actual_worker_count(harness):
    from horizon.proxy.compact_edits import CompactEditError

    h = harness
    h.app.state.compact_edit_cohort_resolver = None
    h.proxy.config.worker_processes = 2
    with pytest.raises(CompactEditError, match="actual proxy worker"):
        h.registry.install(h.app)
    h.proxy.config.worker_processes = 1
    h.registry.install(h.app)
    assert h.post().status_code == 200
    with pytest.raises(CompactEditError, match="replace"):
        h.registry.install(h.app)


def test_changed_replayed_result_stops_generation(harness):
    h = harness
    h.answers = [h.compact]
    body = h.request()
    first = h.post(body)
    body["messages"].extend(
        [
            {"role": "assistant", "content": first.json()["content"]},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "compact-1", "content": "edited"}
                ],
            },
        ]
    )
    assert h.post(body).status_code == 200
    body["messages"][-1]["content"][0]["content"] = "changed result"
    assert h.post(body).status_code == 502
    assert len(h.calls) == 2


@pytest.mark.asyncio
async def test_cancelled_thread_mutation_finishes_before_unlock(harness):
    import threading

    h = harness
    entered, release = threading.Event(), threading.Event()
    original = h.controller.translate_response

    def slow_translate(*args):
        entered.set()
        assert release.wait(timeout=2)
        return original(*args)

    h.controller.translate_response = slow_translate
    h.answers = [h.compact]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h.app), base_url="http://test"
    ) as client:
        pending = asyncio.create_task(
            client.post(
                "/p/fixture/v1/messages",
                json=h.request(),
                headers={
                    "x-horizon-proxy-token": "cs_live_fixture",
                    "x-api-key": "dummy-provider",
                    "x-horizon-session-id": h.scope.conversation,
                },
            )
        )
        assert await asyncio.to_thread(entered.wait, 2)
        pending.cancel()
        await asyncio.sleep(0.02)
        assert h.cohort.lock.locked()
        pending.cancel()
        await asyncio.sleep(0.02)
        assert h.cohort.lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert not h.cohort.lock.locked()
    assert h.journal.get(h.scope.key, "admission")["reserved_call"] == "compact-1"
    assert len(h.events()) == 1
    assert h.journal.get(h.scope.key, "admission") is not None


@pytest.mark.parametrize("field", ["session", "route", "schema"])
def test_unregistered_or_unsupported_requests_remain_native(harness, field):
    h = harness
    body, headers, path = h.request(), {}, "/p/fixture/v1/messages"
    if field == "session":
        headers["x-horizon-session-id"] = "unregistered"
    elif field == "route":
        path = "/v1/messages"
    else:
        body["tools"][0]["description"] = "unknown client definition"
    assert h.post(body, headers=headers, path=path).status_code == 200
    assert VIRTUAL_TOOL not in json.dumps(h.calls[0][0]["tools"])
    assert h.journal.get(h.scope.key, "admission") is None


@pytest.mark.parametrize("bad", ["digest", "encoding", "newline", "provenance"])
def test_managed_source_bridge_rejects_unattested_bytes(harness, bad):
    from horizon.proxy.compact_edits import CompactEditError

    source = harness.candidate.snapshot.source.encode()
    if bad == "encoding":
        source = b"\xff"
    elif bad == "newline":
        source = source.rstrip(b"\n")
    with pytest.raises(CompactEditError):
        managed_candidate(
            source_bytes=source,
            expected_sha256="bad" if bad == "digest" else hashlib.sha256(source).hexdigest(),
            provenance="" if bad == "provenance" else "fixture",
            path="catalog.py",
            native_path="/workspace/catalog.py",
            table="ANTHROPIC_PRICES",
            template=MODEL,
            keys=tuple(f"fixture-{i}" for i in range(4)),
        )


def test_registry_refuses_duplicate_binding_and_multiple_workers(harness):
    from horizon.proxy.compact_edits import CompactEditError

    h = harness
    with pytest.raises(CompactEditError, match="already bound"):
        h.registry.bind(scope=h.scope, candidate=h.candidate, qualification=h.qualification)
    with pytest.raises(CompactEditError, match="one worker"):
        ManagedClaudeRegistry(controller=h.controller, journal=h.journal, worker_processes=2)


def test_missing_journal_on_resume_fails_before_provider(harness):
    h = harness
    assert h.post().status_code == 200
    h.journal.close()
    h.journal = ReplayJournal(h.journal_path.parent / "missing.sqlite")
    h.cohort.journal = h.journal
    h.cohort.resume_required = True
    assert h.post().status_code == 502
    assert len(h.calls) == 1


def test_wrong_actual_model_never_publishes_tool(harness):
    h = harness

    def wrong(body):
        result = h.compact(body)
        result["model"] = "unqualified-model"
        return result

    h.answers = [wrong]
    assert h.post().status_code == 502
    assert h.journal.get(h.scope.key, "admission")["reserved_call"] is None
    assert len(h.events()) == 1 and h.events()[0]["cost_usd"] > 0


def test_cache_is_partitioned_by_authenticated_account(harness):
    h = harness
    # Ordinary native calls have no receipt to incidentally partition the key.
    headers = {"x-horizon-session-id": "unregistered"}
    assert h.post(headers=headers).status_code == 200
    assert h.post(headers=headers).status_code == 200
    assert len(h.calls) == 1
    assert h.post(headers={**headers, "x-horizon-proxy-token": "cs_live_other"}).status_code == 200
    assert len(h.calls) == 2
    assert [e["user_id"] for e in h.events()] == [h.user, h.user, h.other_user]


def test_outbox_survives_restart_and_deduplicates_same_event(harness):
    h = harness
    assert h.post().status_code == 200
    before = h.events()
    service = AccountAnalytics()
    service.path = h.account.path
    service._enqueue(before[0])
    assert [json.loads(row[1]) for row in service._batch()] == before


@pytest.mark.asyncio
async def test_parallel_requests_do_not_publish_compact_edit_twice(harness):
    h = harness
    h.answers = [h.compact]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h.app), base_url="http://test"
    ) as client:
        headers = {
            "x-horizon-proxy-token": "cs_live_fixture",
            "x-api-key": "dummy-provider",
            "x-horizon-session-id": h.scope.conversation,
        }
        replies = await asyncio.gather(
            *[
                client.post("/p/fixture/v1/messages", json=h.request(), headers=headers)
                for _ in range(2)
            ]
        )
    assert sorted(r.status_code for r in replies) == [200, 502]
    assert len(h.calls) == 1
    assert h.journal.get(h.scope.key, "admission")["reserved_call"] == "compact-1"


def test_queue_timeout_is_bounded_and_never_calls_provider(harness):
    h = harness
    h.cohort.queue_timeout_seconds = 0.01
    asyncio.run(h.cohort.lock.acquire())
    try:
        assert h.post().status_code == 504
        assert not h.calls
    finally:
        h.cohort.lock.release()


def test_fresh_proxy_app_restores_history_and_durable_outbox(harness, monkeypatch):
    h = harness
    h.answers = [h.compact]
    body = h.request()
    first = h.post(body)
    assert first.status_code == 200
    before = h.events()
    body["messages"].extend(
        [
            {"role": "assistant", "content": first.json()["content"]},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "compact-1", "content": "edited"}
                ],
            },
        ]
    )
    fresh = Harness(h.journal_path.parent, monkeypatch, identities=(h.user, h.key_id, h.other_user))
    try:
        fresh.registry = ManagedClaudeRegistry(
            controller=fresh.controller, journal=fresh.journal, worker_processes=1
        )
        fresh.cohort = fresh.registry.restore(scope=fresh.scope, qualification=fresh.qualification)
        assert not fresh.proxy.cache._cache
        assert not fresh.proxy.session_tracker_store._trackers
        assert fresh.post(body).status_code == 200
        assert fresh.calls[0][0]["messages"][-2]["content"][0]["name"] == VIRTUAL_TOOL
        events = fresh.events()
        assert len(events) == 2 and events[0] == before[0]
        assert events[0]["event_id"] != events[1]["event_id"]
        assert events[0]["runtime_id"] != events[1]["runtime_id"]
    finally:
        fresh.close()


def test_unattested_read_recovers_with_native_read(harness):
    h = harness
    h.answers = [
        h.compact,
        message(
            [
                {
                    "type": "tool_use",
                    "id": "read-again",
                    "name": "Read",
                    "input": {"file_path": h.candidate.snapshot.native_path},
                }
            ],
            index=2,
        ),
    ]
    reply = h.post(h.request(read=False))
    assert reply.status_code == 200
    assert reply.json()["content"][0]["name"] == "Read"
    assert h.calls[1][0]["tool_choice"]["name"] == "Read"
    assert len(h.events()) == 2
    assert h.journal.get(h.scope.key, "admission")["reserved_call"] is None


@pytest.mark.asyncio
async def test_disconnect_after_reservation_keeps_mapping_and_settlement(harness):
    h = harness
    h.answers = [h.compact]
    encoded = json.dumps(h.request()).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/p/fixture/v1/messages",
        "raw_path": b"/p/fixture/v1/messages",
        "query_string": b"",
        "server": ("test", 80),
        "client": ("127.0.0.1", 12),
        "headers": [
            (b"x-horizon-proxy-token", b"cs_live_fixture"),
            (b"x-api-key", b"dummy-provider"),
            (b"x-horizon-session-id", h.scope.conversation.encode()),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(encoded)).encode()),
        ],
    }
    delivered = False
    wait_forever = asyncio.Event()

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": encoded, "more_body": False}
        await wait_forever.wait()
        return {"type": "http.disconnect"}

    async def send(frame):
        if frame["type"] == "http.response.body":
            raise OSError("client disconnected after durable reservation")

    with pytest.raises(OSError):
        await h.app(scope, receive, send)
    assert len(h.events()) == 1 and h.events()[0]["cost_usd"] > 0
    assert h.journal.get(h.scope.key, "admission")["reserved_call"] == "compact-1"
    assert h.journal.get(h.scope.key, "call:compact-1").get("native_result") is None
    # No model retry or tool retransmission follows an uncertain publication.
    assert h.post().status_code == 502
    assert len(h.calls) == 1
