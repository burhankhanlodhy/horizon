"""Fold at rewrite, end to end through the Anthropic handler.

Two turns of one conversation go through ``handle_anthropic_messages``: a cold
first turn (a resumed session with no tracker) folds the old Bash output, and a
warm second turn re-sends the raw history and must forward the byte-identical
stub. Shadow and flag-off paths must forward the raw bytes unchanged.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import anyio
import pytest

from horizon.cache.compression_store import reset_compression_store
from horizon.cache.fold_originals import ccr_lookup_store
from horizon.config import TransformResult
from horizon.proxy import fold_at_rewrite as fold
from horizon.proxy.handlers.anthropic import AnthropicHandlerMixin
from horizon.proxy.models import ProxyConfig
from tests.test_cache_mode_cold_start import (
    _build_request,
    _DummyMetrics,
    _DummyTokenizer,
    _ResponseStub,
)

BIG = "verbose compile output line\n" * 300  # ~8.4 KB, past the 2 KB floor


def _identity_pipeline(messages, model, **kwargs):
    return TransformResult(
        messages=list(messages),
        tokens_before=0,
        tokens_after=0,
        transforms_applied=[],
    )


class _Tracker:
    """The prefix tracker's surface the handler reads, backed by plain lists."""

    _cached_token_count = 0
    read_maturation_manager = None
    flash_view = None

    def __init__(self) -> None:
        self.original: list = []
        self.forwarded: list = []
        self.idle = 0.0

    @property
    def _idle_seconds_at_fetch(self) -> float:
        return self.idle

    def get_frozen_message_count(self) -> int:
        return 0

    def get_last_original_messages(self):
        return self.original

    def get_last_forwarded_messages(self):
        return self.forwarded

    def resolved_cache_ttl_seconds(self) -> int:
        return 300

    def classify_cache_miss(self, *args, **kwargs):
        return SimpleNamespace(is_miss=False)

    def update_from_response(
        self,
        cache_read_tokens=0,
        cache_write_tokens=0,
        messages=None,
        original_messages=None,
        **kwargs,
    ):
        self.forwarded = json.loads(json.dumps(messages))
        self.original = json.loads(json.dumps(original_messages or messages))

    def keep_alive_touch(self) -> None:
        return None


class _Handler(AnthropicHandlerMixin):
    ANTHROPIC_API_URL = "https://api.anthropic.com"

    def __init__(self, tracker: _Tracker) -> None:
        self.rate_limiter = None
        self.metrics = _DummyMetrics()
        self.config = ProxyConfig(
            optimize=True,
            image_optimize=False,
            retry_max_attempts=1,
            retry_base_delay_ms=1,
            retry_max_delay_ms=1,
            connect_timeout_seconds=10,
            mode="cache",
            cache_enabled=False,
            rate_limit_enabled=False,
            prefix_freeze_enabled=False,
            memory_enabled=False,
            ccr_inject_tool=True,
        )
        self.usage_reporter = None
        self.anthropic_provider = SimpleNamespace(get_context_limit=lambda model: 200_000)
        self.anthropic_pipeline = SimpleNamespace(apply=_identity_pipeline)
        self.anthropic_backend = None
        self.cost_tracker = None
        self.memory_handler = None
        self.cache = None
        self.security = None
        self.ccr_context_tracker = None
        self.ccr_injector = None
        self.ccr_response_handler = None
        self.ccr_feedback = None
        self.ccr_batch_processor = None
        self.ccr_mcp_server = None
        self.traffic_learner = None
        self.tool_injector = None
        self.read_lifecycle_manager = None
        self.logger = SimpleNamespace(log=lambda *a, **k: None)
        self.request_logger = self.logger
        self.usage_observer = None
        self.image_compressor = None
        self.session_tracker_store = SimpleNamespace(
            compute_session_id=lambda *a, **k: "sess-fold",
            get_or_create=lambda *a, **k: tracker,
            resolve_tracker=lambda *a, **k: tracker,
        )
        self._background_compression_enabled = False
        self.recorded_tags: dict = {}
        self.outcomes: list = []
        self.captured_body: dict = {}
        self.tracker = tracker

    async def _record_request_outcome(self, outcome) -> None:
        self.recorded_tags = dict(outcome.tags or {})
        self.outcomes.append(outcome)

    async def _run_compression_in_executor(self, fn, timeout):
        return fn()

    async def _next_request_id(self) -> str:
        return "req-fold-test"

    async def _retry_request(self, method, url, headers, body, **_kwargs):
        self.captured_body = json.loads(json.dumps(body))
        return _ResponseStub()


def _turn(client_messages: list) -> dict:
    return {"model": "claude-3-5-sonnet-latest", "max_tokens": 64, "messages": client_messages}


def _conversation() -> list:
    """One big Bash result, then 12 turns of ordinary text, then the current question."""
    msgs: list = [{"role": "user", "content": "fix the build"}]
    msgs.append(
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_build",
                    "name": "Bash",
                    "input": {"command": "make"},
                }
            ],
        }
    )
    msgs.append(
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_build", "content": BIG}],
        }
    )
    for _ in range(12):
        msgs.append({"role": "assistant", "content": "still looking"})
        msgs.append({"role": "user", "content": "ok"})
    msgs.append({"role": "user", "content": "what now?"})
    return msgs


def _run_turn(handler: _Handler, client_messages: list) -> None:
    anyio.run(handler.handle_anthropic_messages, _build_request(_turn(client_messages)))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    import horizon.tokenizers as _tk

    monkeypatch.setattr(_tk, "get_tokenizer", lambda model: _DummyTokenizer())
    monkeypatch.delenv(fold.FLAG_ENV, raising=False)
    reset_compression_store()
    fold.reset_fold_store()
    yield
    reset_compression_store()
    fold.reset_fold_store()


def _tool_result_content(body_messages: list) -> str:
    for msg in body_messages:
        if msg["role"] == "user" and isinstance(msg["content"], list):
            for block in msg["content"]:
                if block.get("type") == "tool_result" and block["tool_use_id"] == "toolu_build":
                    return block["content"]
    raise AssertionError("tool_result toolu_build not forwarded")


def test_cold_turn_folds_and_warm_turn_forwards_identical_bytes(monkeypatch):
    monkeypatch.setenv(fold.FLAG_ENV, "1")
    tracker = _Tracker()
    handler = _Handler(tracker)

    # Turn 1: a resumed session (prior assistant turns, no tracker): cold.
    turn1 = _conversation()
    _run_turn(handler, turn1)
    forwarded1 = handler.captured_body["messages"]
    stub = _tool_result_content(forwarded1)
    assert stub.startswith(fold.STUB_PREFIX)
    assert "Retrieve more: hash=" in stub
    assert len(stub) < len(BIG) * fold.MIN_GAIN_RATIO
    # The retrieve tool is injected on this same request, so the marker is redeemable.
    tool_names = [t.get("name") for t in handler.captured_body.get("tools") or []]
    assert "horizon_retrieve" in tool_names
    # The original is in the store under the stub's key.
    key = stub.rsplit("hash=", 1)[1].rstrip("]")
    assert ccr_lookup_store().retrieve(key).original_content == BIG
    # The fold saving is booked on the request (tokens recounted, transform labelled).
    outcome1 = handler.outcomes[-1]
    assert outcome1.tokens_saved > 0
    assert any(str(t).startswith("fold_at_rewrite:") for t in outcome1.transforms_applied)

    # Turn 2: the client re-sends the raw history plus one more exchange. Warm.
    turn2 = turn1 + [
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "thanks"},
    ]
    _run_turn(handler, turn2)
    forwarded2 = handler.captured_body["messages"]
    # The cached prefix (everything turn 1 forwarded) is byte-identical on turn 2.
    prefix_len = len(forwarded1)
    assert json.dumps(forwarded2[:prefix_len], sort_keys=True) == json.dumps(
        forwarded1, sort_keys=True
    )
    assert _tool_result_content(forwarded2) == stub


def test_flag_off_forwards_raw_bytes_and_books_no_fold(monkeypatch):
    tracker = _Tracker()
    handler = _Handler(tracker)
    turn1 = _conversation()
    _run_turn(handler, turn1)
    forwarded = handler.captured_body["messages"]
    assert _tool_result_content(forwarded) == BIG
    assert all(
        not str(t).startswith("fold_at_rewrite") for t in handler.outcomes[-1].transforms_applied
    )
    assert "fold_shadow" not in handler.recorded_tags


def test_shadow_mode_forwards_raw_bytes_and_reports_what_it_would_fold(monkeypatch):
    monkeypatch.setenv(fold.FLAG_ENV, "shadow")
    tracker = _Tracker()
    handler = _Handler(tracker)
    turn1 = _conversation()
    _run_turn(handler, turn1)
    forwarded = handler.captured_body["messages"]
    assert _tool_result_content(forwarded) == BIG
    shadow = handler.recorded_tags["fold_shadow"]
    assert shadow["would_fold"] == 1
    assert shadow["token_delta"] > 0
    assert handler.outcomes[-1].tokens_saved == 0
    assert fold.get_fold_store().decisions("sess-fold") == {}


def test_fold_saving_reaches_the_verified_figure_inputs(monkeypatch):
    """The request's saving, as the verified-figure path reads it, is positive."""
    from horizon.savings_ledger import verified_saving_usd

    monkeypatch.setenv(fold.FLAG_ENV, "1")
    handler = _Handler(_Tracker())
    _run_turn(handler, _conversation())
    outcome = handler.outcomes[-1]
    saved = outcome.tokens_saved
    assert saved > 0
    usd = verified_saving_usd(
        tokens_before=int(outcome.optimized_tokens) + saved,
        tokens_after=int(outcome.optimized_tokens),
        model=outcome.model,
        saved_compression=saved,
        saved_tool_schema=0,
        cache_read_tokens=outcome.cache_read_tokens,
        cache_write_tokens=outcome.cache_write_tokens,
        uncached_input_tokens=outcome.uncached_input_tokens,
        provider="anthropic",
    )
    assert usd > 0


# ---------------------------------------------------------------- flash on


GREP_TEXT = "grep hit line of output\n" * 130  # ~3.2 KB: under flash's 8 KB, over fold's 2 KB


def _flash_conversation() -> list:
    """A big Bash result flash owns, a mid-size Grep result the fold owns, then age."""
    msgs: list = [{"role": "user", "content": "fix the build"}]
    msgs.append(
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_build",
                    "name": "Bash",
                    "input": {"command": "make"},
                }
            ],
        }
    )
    msgs.append(
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_build", "content": BIG}],
        }
    )
    msgs.append(
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_grep",
                    "name": "Grep",
                    "input": {"pattern": "error"},
                }
            ],
        }
    )
    msgs.append(
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_grep", "content": GREP_TEXT}],
        }
    )
    for _ in range(12):
        msgs.append({"role": "assistant", "content": "still looking"})
        msgs.append({"role": "user", "content": "ok"})
    msgs.append({"role": "user", "content": "what now?"})
    return msgs


def _run_model(handler: _Handler, client_messages: list, model: str) -> None:
    body = {"model": model, "max_tokens": 64, "messages": client_messages}
    anyio.run(handler.handle_anthropic_messages, _build_request(body))


def _result_text(messages: list, tid: str):
    for msg in messages:
        if msg["role"] == "user" and isinstance(msg["content"], list):
            for block in msg["content"]:
                if block.get("type") == "tool_result" and block["tool_use_id"] == tid:
                    return block["content"]
    raise AssertionError(f"tool_result {tid} not forwarded")


def test_flash_and_fold_coexist_and_stay_byte_stable_on_warm_resend(monkeypatch):
    from horizon.transforms import flash_observations as flash

    monkeypatch.setenv(fold.FLAG_ENV, "1")
    monkeypatch.setenv("HORIZON_FLASH_OBSERVATIONS", "1")
    tracker = _Tracker()
    handler = _Handler(tracker)
    handler._flash_horizons = flash.FlashHorizons(None)  # in memory, no workspace file
    conv = _flash_conversation()
    # Flash owns results from the start of this conversation.
    handler._flash_horizons.horizon_for(flash.conversation_key(conv), 0)

    _run_model(handler, conv, "claude-haiku-5-5")
    fw1 = handler.captured_body["messages"]
    flash_text = _result_text(fw1, "toolu_build")
    fold_text = _result_text(fw1, "toolu_grep")
    # Flash replaced the big Bash result, and the fold skipped it.
    assert flash_text.startswith("[Horizon flash:")
    # The fold took the mid-size Grep result (flash's size floor left it alone).
    assert fold_text.startswith("[Horizon folded:")
    # The fold stub keeps its retrieval marker, so the retrieve tool is injected.
    tool_names = [t.get("name") for t in handler.captured_body.get("tools") or []]
    assert "horizon_retrieve" in tool_names
    key = fold_text.rsplit("hash=", 1)[1].rstrip("]")
    assert ccr_lookup_store().retrieve(key).original_content == GREP_TEXT

    # Warm resend: the client re-sends the raw history plus one exchange.
    _run_model(
        handler,
        conv + [{"role": "assistant", "content": "done"}, {"role": "user", "content": "thanks"}],
        "claude-haiku-5-5",
    )
    fw2 = handler.captured_body["messages"]
    assert _result_text(fw2, "toolu_build") == flash_text
    assert _result_text(fw2, "toolu_grep") == fold_text
