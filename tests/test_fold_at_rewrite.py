"""Fold stale tool output at cache rewrites: module-level contract.

The fold is only worth doing when the prompt cache is being rewritten anyway,
so these tests pin: warm requests never fold new results, cold requests fold old
eligible ones, a folded result is re-sent as the identical stub forever, the
stub's retrieval key redeems the original, recent turns / Read results still in
use / the last user message / errors are never touched, tool pairing survives,
shadow mode changes no bytes, accounting equals the token delta, and the flag
off path is a no-op.
"""

from __future__ import annotations

import json

import pytest

from horizon.cache.compression_store import format_retrieval_miss_detail, reset_compression_store
from horizon.cache.fold_originals import (
    CcrLookupStore,
    FoldOriginalStore,
    ccr_lookup_store,
    detail_for,
    get_fold_originals,
)
from horizon.ccr.tool_injection import CCRToolInjector
from horizon.proxy import fold_at_rewrite as fold
from horizon.proxy.savings_attribution import SAVINGS_ATTRIBUTION_TAG

BIG = "line of verbose build output\n" * 300  # ~8.4 KB, well over the 2 KB floor


def get_compression_store_or_none():
    from horizon.cache.compression_store import get_compression_store

    return get_compression_store()


def _count(text: str) -> int:
    return len(text) // 4


def _tool_turn(tool: str, tid: str, text: str = BIG, inp: dict | None = None):
    """An assistant tool_use followed by the user's tool_result."""
    use = {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": tid, "name": tool, "input": inp or {"command": "make"}}
        ],
    }
    result = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}],
    }
    return [use, result]


def _conversation(old: int = 3, recent_turns: int = 2, filler: int = 10):
    """Big Bash results, then `filler` text turns (so they are past age), then recent ones."""
    msgs: list = [{"role": "user", "content": "fix the build"}]
    for k in range(old):
        msgs += _tool_turn("Bash", f"toolu_old_{k}")
    for _ in range(filler):
        msgs += [{"role": "assistant", "content": "thinking"}, {"role": "user", "content": "ok"}]
    for k in range(recent_turns):
        msgs += _tool_turn("Bash", f"toolu_recent_{k}", text="small")
    msgs.append({"role": "user", "content": "now what?"})
    return msgs


def _ages(messages):
    """Assistant-turn index bookkeeping used by the age test (for readability)."""
    return [i for i, m in enumerate(messages) if m["role"] == "assistant"]


class _Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(fold.FLAG_ENV, raising=False)
    monkeypatch.delenv(fold.AGE_TURNS_ENV, raising=False)
    monkeypatch.delenv(fold.MIN_CHARS_ENV, raising=False)
    monkeypatch.delenv("HORIZON_FOLD_TTL_SECONDS", raising=False)
    monkeypatch.delenv("HORIZON_FOLD_STORE_MAX_MB", raising=False)
    monkeypatch.delenv("HORIZON_FOLD_IDLE_SECONDS", raising=False)
    reset_compression_store()
    fold.reset_fold_store()
    yield
    reset_compression_store()
    fold.reset_fold_store()


def _store(clock=None) -> fold.FoldSessionStore:
    return fold.FoldSessionStore(clock=clock or _Clock())


def _run(messages, *, store, cold=True, mode="on", session="s1", original=None, **kw):
    return fold.run_fold(
        messages,
        mode=mode,
        cold=cold,
        session_id=session,
        store=store,
        original_messages=original,
        count_text=_count,
        age=kw.pop("age", 10),
        floor_chars=kw.pop("floor_chars", 2000),
        writer=kw.pop("writer", None),
        **kw,
    )


# ---------------------------------------------------------------- flag / config


def test_flag_off_by_default_and_modes_parse(monkeypatch):
    assert fold.fold_mode() == "off"
    monkeypatch.setenv(fold.FLAG_ENV, "1")
    assert fold.fold_mode() == "on"
    monkeypatch.setenv(fold.FLAG_ENV, "shadow")
    assert fold.fold_mode() == "shadow"
    monkeypatch.setenv(fold.FLAG_ENV, "maybe")
    assert fold.fold_mode() == "off"


def test_config_env_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv(fold.AGE_TURNS_ENV, "nope")
    assert fold.age_turns() == fold.DEFAULT_AGE_TURNS
    monkeypatch.setenv(fold.AGE_TURNS_ENV, "3")
    assert fold.age_turns() == 3
    monkeypatch.setenv(fold.MIN_CHARS_ENV, "0")  # below minimum -> default
    assert fold.min_chars() == fold.DEFAULT_MIN_CHARS


def test_flag_off_run_is_identity():
    msgs = _conversation()
    out = fold.run_fold(msgs, mode="off", cold=True, session_id="s", store=_store())
    assert out.messages is msgs
    assert not out.changed


# ---------------------------------------------------------------- cold signal


@pytest.mark.parametrize(
    "ttl, last_seen, has_history, idle, prior, expected",
    [
        # caching off: nothing to bust
        (None, 1, True, 1, True, (True, "cache_off")),
        # kept warm by a recent request / ping
        (300, 100, False, 0, True, (False, "warm")),
        # lapsed: last request long ago
        (300, 600, False, 0, True, (True, "idle")),
        # tracker idle past TTL + margin, no last-seen known
        (300, None, True, 400, True, (True, "idle")),
        # resumed session: no tracker, no last-seen, prior assistant turns
        (300, None, False, 0, True, (True, "resumed")),
        # brand-new session: nothing old to fold anyway
        (300, None, False, 0, False, (False, "new_session")),
        # conflicting evidence resolves to warm (a wrong cold busts a warm cache)
        (300, 900, True, 10, True, (False, "warm")),
    ],
)
def test_classify_cold(ttl, last_seen, has_history, idle, prior, expected):
    got = fold.classify_cold(
        cache_ttl_seconds=ttl,
        last_seen_age=last_seen,
        tracker_has_history=has_history,
        tracker_idle_seconds=idle,
        has_prior_turns=prior,
    )
    assert got == expected


def test_keep_alive_ping_keeps_session_warm():
    import time

    store = fold.get_fold_store()  # the ping callback writes to the process-wide store
    calls = []
    touch = fold.keep_alive_touch_for(lambda: calls.append(1), "s1")
    # Session last spoke 20 minutes ago (cache lapsed); a keep-alive ping then reads it.
    store.touch("s1", now=time.time() - 1200)
    assert store.last_seen_age("s1") > 1000
    touch()
    assert calls == [1]
    assert store.last_seen_age("s1") == pytest.approx(0.0, abs=1.0)
    cold, reason = fold.classify_cold(
        cache_ttl_seconds=300,
        last_seen_age=store.last_seen_age("s1"),
        tracker_has_history=False,
        tracker_idle_seconds=0,
        has_prior_turns=True,
    )
    assert (cold, reason) == (False, "warm")


# ---------------------------------------------------------------- eligibility


def test_cold_folds_old_eligible_results_and_spares_recent_and_last_user():
    msgs = _conversation(old=3, recent_turns=2)
    store = _store()
    out = _run(msgs, store=store, cold=True)
    assert out.changed
    folded = {d.tool_use_id for d in out.new}
    assert folded == {"toolu_old_0", "toolu_old_1", "toolu_old_2"}
    # recent (within 10 assistant turns) results are untouched
    assert "toolu_recent_0" not in folded


def test_age_threshold_is_configurable():
    msgs = _conversation(old=3, recent_turns=2)
    # The oldest result has 14 assistant turns after it, the newest old one has 12.
    assert _run(msgs, store=_store(), cold=True, age=15).new == []
    assert len(_run(msgs, store=_store(), cold=True, age=12).new) == 3
    assert len(_run(msgs, store=_store(), cold=True, age=13).new) == 2


def test_warm_request_never_folds_new_results():
    msgs = _conversation(old=3)
    store = _store()
    out = _run(msgs, store=store, cold=False)
    assert out.messages is msgs
    assert out.new == []
    assert store.decisions("s1") == {}


def test_last_user_message_and_errors_and_small_results_untouched():
    # Old enough to fold, but these must each be spared for their own reason.
    msgs = [{"role": "user", "content": "start"}]
    msgs += _tool_turn("Bash", "toolu_old_0")
    msgs += _tool_turn("Bash", "toolu_err", text=BIG)
    msgs[-1]["content"][0]["is_error"] = True
    msgs += _tool_turn("Bash", "toolu_small", text="tiny")
    msgs += [{"role": "assistant", "content": "x"}, {"role": "user", "content": "ok"}] * 12
    # The final user message carries a big result: it is the last user message.
    msgs += _tool_turn("Bash", "toolu_last", text=BIG)
    out = _run(msgs, store=_store(), cold=True)
    ids = {d.tool_use_id for d in out.new}
    assert "toolu_old_0" in ids
    assert "toolu_err" not in ids
    assert "toolu_small" not in ids
    assert "toolu_last" not in ids
    assert out.skipped.get("error", 0) >= 1
    assert out.skipped.get("small", 0) >= 1
    assert out.skipped.get("last_user_message", 0) >= 1


def test_read_folds_only_when_superseded():
    msgs = [{"role": "user", "content": "go"}]
    msgs += _tool_turn("Read", "toolu_read_live", inp={"file_path": "/a.py"})
    msgs += _tool_turn("Read", "toolu_read_old", inp={"file_path": "/b.py"})
    msgs += _tool_turn("Edit", "toolu_edit_b", text="ok", inp={"file_path": "/b.py"})
    msgs += [{"role": "assistant", "content": "x"}] * 12
    msgs.append({"role": "user", "content": "end"})
    out = _run(msgs, store=_store(), cold=True)
    ids = {d.tool_use_id for d in out.new}
    assert "toolu_read_old" in ids  # /b.py was edited later
    assert "toolu_read_live" not in ids  # /a.py is still the live version


def test_retrieval_results_and_disallowed_tools_never_fold():
    msgs = [{"role": "user", "content": "go"}]
    msgs += _tool_turn("mcp__horizon__horizon_retrieve", "toolu_ret")
    msgs += _tool_turn("Task", "toolu_task")
    msgs += _tool_turn("mcp__github__list", "toolu_mcp")
    msgs += [{"role": "assistant", "content": "x"}] * 12
    msgs.append({"role": "user", "content": "end"})
    out = _run(msgs, store=_store(), cold=True)
    ids = {d.tool_use_id for d in out.new}
    assert "toolu_ret" not in ids
    assert "toolu_task" not in ids
    assert "toolu_mcp" in ids


def test_tool_pairing_and_structure_preserved():
    msgs = _conversation(old=2)
    out = _run(msgs, store=_store(), cold=True)
    assert len(out.messages) == len(msgs)
    uses = {
        b["id"]
        for m in out.messages
        if m["role"] == "assistant" and isinstance(m["content"], list)
        for b in m["content"]
        if b["type"] == "tool_use"
    }
    results = [
        b["tool_use_id"]
        for m in out.messages
        if m["role"] == "user" and isinstance(m["content"], list)
        for b in m["content"]
        if b["type"] == "tool_result"
    ]
    assert set(results) <= uses
    for old_m, new_m in zip(msgs, out.messages):
        assert old_m["role"] == new_m["role"]


# ---------------------------------------------------------------- stub + byte stability


def test_stub_names_tool_size_exit_code_and_retrieval_key():
    text = "\n".join(f"step {i}" for i in range(300)) + "\nexit code 2\n"
    key = fold.ccr_key(text)
    stub = fold.build_stub("Bash", text, key)
    assert stub.startswith(fold.STUB_PREFIX)
    assert "Bash output" in stub
    assert "exit code 2" in stub
    assert f"hash={key}]" in stub
    assert "Last lines:" in stub
    assert len(stub) < len(text) * fold.MIN_GAIN_RATIO


def test_cold_fold_then_warm_resend_is_byte_identical():
    """A later request re-sends the raw transcript; it must carry the identical stub."""
    raw = _conversation(old=3, recent_turns=2)
    store = _store()
    cold = _run(raw, store=store, cold=True)
    assert cold.changed
    cold_bytes = json.dumps(cold.messages, sort_keys=True)

    # Next turn: client re-sends the raw history plus one more exchange (warm).
    nxt = raw + _tool_turn("Bash", "toolu_next", text="short")
    warm = _run(nxt, store=store, cold=False)
    assert warm.changed  # the re-sent raw results get the recorded stubs
    for stub_msg_cold, stub_msg_warm in zip(cold.messages[: len(raw)], warm.messages[: len(raw)]):
        assert json.dumps(stub_msg_cold, sort_keys=True) == json.dumps(
            stub_msg_warm, sort_keys=True
        )
    # And the cold output itself is unchanged by a re-run (deterministic).
    again = _run(raw, store=store, cold=False)
    assert json.dumps(again.messages, sort_keys=True) == cold_bytes


def test_warm_reapply_tolerates_already_stubbed_input():
    raw = _conversation(old=2)
    store = _store()
    cold = _run(raw, store=store, cold=True)
    warm = _run(cold.messages, store=store, cold=False)
    assert warm.messages is cold.messages or warm.changed is False


def test_store_failure_leaves_result_unfolded_and_never_raises():
    def boom(*a, **k):
        raise RuntimeError("store down")

    msgs = _conversation(old=2)
    out = _run(msgs, store=_store(), cold=True, writer=boom)
    assert out.new == []
    assert out.skipped.get("store_error", 0) == 2
    assert out.messages == msgs or not out.changed


# ---------------------------------------------------------------- CCR round trip


def test_folded_stub_is_detected_as_retrievable_and_redeems_original(monkeypatch):
    raw = _conversation(old=1, recent_turns=0)
    # The stub must be recognised by the same detector that injects horizon_retrieve.
    store = _store()
    out = _run(raw, store=store, cold=True)
    assert out.changed
    stubs = list(out.new)
    assert stubs
    entry = ccr_lookup_store().retrieve(stubs[0].key)
    assert entry is not None
    assert entry.original_content == BIG
    # The original lives in the fold store, not the shared CCR store.
    assert get_compression_store_or_none() is None or not (
        get_compression_store_or_none().exists(stubs[0].key)
    )
    injector = CCRToolInjector(
        provider="anthropic", inject_tool=False, inject_system_instructions=False
    )
    injector.scan_for_markers(out.messages)
    assert stubs[0].key in injector.detected_hashes
    assert injector.verify_ownership() == injector.detected_hashes


def test_original_prefers_client_raw_text_over_compressed_forwarded_text():
    compressed = "compressed " * 300  # forwarded text (already shortened by the pipeline)
    raw_msgs = _conversation(old=1, recent_turns=0)
    # Forwarded copy is the compressed text; the client's raw result is BIG.
    fwd = _conversation(old=1, recent_turns=0)
    fwd[2]["content"][0]["content"] = compressed
    recorded: list[str] = []

    def capture(session_id, original, key):
        recorded.append(original)
        return True

    out = _run(fwd, store=_store(), cold=True, original=raw_msgs, writer=capture)
    assert out.new
    assert BIG in recorded


# ---------------------------------------------------------------- shadow / accounting


def test_shadow_mode_changes_no_bytes_and_stores_nothing():
    msgs = _conversation(old=3)
    store = _store()
    before = json.dumps(msgs, sort_keys=True)
    calls = []
    out = _run(msgs, store=store, cold=True, mode="shadow", writer=lambda *a: calls.append(a))
    assert out.messages is msgs
    assert json.dumps(out.messages, sort_keys=True) == before
    assert out.would_fold == 3
    assert out.token_delta > 0
    assert calls == []
    assert store.decisions("s1") == {}


def test_accounting_equals_token_delta_priced_by_tokenizer():
    msgs = _conversation(old=2)
    out = _run(msgs, store=_store(), cold=True)
    expected = 0
    for m_old, m_new in zip(msgs, out.messages):
        if m_old is m_new:
            continue
        for b_old, b_new in zip(m_old["content"], m_new["content"]):
            if b_old is not b_new:
                expected += _count(b_old["content"]) - _count(b_new["content"])
    assert out.token_delta == expected > 0


def test_record_outcome_adds_label_and_tag_but_no_attribution_row():
    msgs = _conversation(old=2)
    out = _run(msgs, store=_store(), cold=True)
    tags: dict = {}
    transforms: list = []
    fold.record_outcome(out, reason="resumed", tags=tags, transforms_applied=transforms)
    assert transforms and transforms[0].startswith("fold_at_rewrite:resumed:2new")
    assert tags["fold_at_rewrite"]["token_delta"] == out.token_delta > 0
    # No attribution row: the saving is already inside tokens_saved (no double count).
    assert SAVINGS_ATTRIBUTION_TAG not in tags


def test_record_outcome_shadow_writes_only_tags():
    msgs = _conversation(old=2)
    out = _run(msgs, store=_store(), cold=True, mode="shadow")
    tags: dict = {}
    transforms: list = []
    fold.record_outcome(out, reason="resumed", tags=tags, transforms_applied=transforms)
    assert transforms == []
    assert tags["fold_shadow"]["would_fold"] == 2


def test_session_capacity_caps_new_folds_without_dropping_recorded_ones():
    store = fold.FoldSessionStore(max_decisions=1, clock=_Clock())
    msgs = _conversation(old=3)
    out = _run(msgs, store=store, cold=True)
    assert len(out.new) == 1
    assert out.skipped.get("session_cap", 0) == 2
    assert len(store.decisions("s1")) == 1


def test_decisions_survive_later_cold_turns():
    raw = _conversation(old=2)
    store = _store()
    first = _run(raw, store=store, cold=True)
    recorded = store.decisions("s1")
    second = _run(raw + _tool_turn("Bash", "toolu_more", text="x"), store=store, cold=True)
    assert set(recorded) <= set(store.decisions("s1"))
    # json.dumps escapes newlines, so compare the escaped form of each stub.
    blob = json.dumps(second.messages)
    for _tid, decision in recorded.items():
        assert json.dumps(decision.stub)[1:-1] in blob
    assert first.changed


# ---------------------------------------------------------------- own original store


def test_originals_evict_only_idle_sessions_and_refuse_when_all_are_live():
    from horizon.cache.fold_originals import STATUS_AVAILABLE, STATUS_EVICTED

    fo = FoldOriginalStore(max_bytes=100, ttl_seconds=86400, idle_seconds=60)
    fo.put("sess-idle", "k1", "x" * 40, now=1000)  # never marked active: idle
    fo.mark_active("sess-live", now=1000)
    assert fo.put("sess-live", "k2", "y" * 40, now=1000)
    assert fo.put("sess-live", "k3", "z" * 40, now=1000)  # evicts idle k1, keeps live k2
    assert fo.status("k1", now=1000) == STATUS_EVICTED
    assert fo.status("k2", now=1000) == STATUS_AVAILABLE
    assert fo.status("k3", now=1000) == STATUS_AVAILABLE
    # Every resident entry belongs to a live session: nothing evictable, so refuse.
    assert fo.put("sess-live", "k4", "w" * 40, now=1000) is False
    assert fo.status("k4", now=1000) == "missing"
    # Once the session goes idle its entries become evictable.
    assert fo.put("sess-other", "k4", "w" * 40, now=1061)
    assert fo.status("k2", now=1061) == STATUS_EVICTED
    assert fo.status("k3", now=1061) == STATUS_AVAILABLE


def test_originals_expire_after_ttl_with_a_clear_status():
    fo = FoldOriginalStore(max_bytes=10_000, ttl_seconds=100, idle_seconds=60)
    fo.put("s", "k", "text", now=0)
    assert fo.get("k", now=99) == "text"
    assert fo.get("k", now=101) is None
    assert fo.status("k", now=101) == "expired"
    assert "24 hours" in detail_for("expired")


def test_refused_original_leaves_the_result_unfolded_so_no_stub_dangles():
    fo = FoldOriginalStore(max_bytes=100, ttl_seconds=86400, idle_seconds=60)
    store = fold.FoldSessionStore(clock=_Clock(), originals=fo)
    out = _run(_conversation(old=2), store=store, cold=True)
    # Each original is larger than the cap, so the store refuses it and no stub is written.
    assert out.new == []
    assert not out.changed
    assert out.skipped.get("store_full", 0) == 2


def test_retrieval_of_an_evicted_fold_key_says_so_clearly():
    fo = FoldOriginalStore(max_bytes=10, ttl_seconds=86400, idle_seconds=60)
    fo.put("idle", "kk", "x" * 8, now=0)
    fo.put("other", "kk2", "y" * 8, now=0)  # both idle: the first is evicted to fit

    class _Primary:
        def retrieve(self, hash_key, query=None):
            return None

        def exists(self, hash_key, clean_expired=False):
            return False

        def get_entry_status(self, hash_key, clean_expired=False):
            return {
                "hash": hash_key,
                "status": "missing",
                "default_ttl_seconds": 1800,
                "ttl_seconds": 1800,
            }

    store = CcrLookupStore(_Primary(), fo)
    assert store.exists("kk") is True  # still ours, so the marker keeps its tool
    assert store.retrieve("kk") is None
    status = store.get_entry_status("kk", clean_expired=True)
    assert status["status"] == "evicted"
    assert format_retrieval_miss_detail(status) == detail_for("evicted")
    assert "no longer available" in format_retrieval_miss_detail(status)


def test_response_handler_answers_an_evicted_original_without_erroring(monkeypatch):
    from horizon.ccr.response_handler import CCRResponseHandler
    from horizon.ccr.tool_calls import CCRToolCall

    class _Primary:
        def retrieve(self, hash_key, query=None):
            return None

        def exists(self, hash_key, clean_expired=False):
            return False

        def get_entry_status(self, hash_key, clean_expired=False):
            return {
                "hash": hash_key,
                "status": "missing",
                "default_ttl_seconds": 1800,
                "ttl_seconds": 1800,
            }

    monkeypatch.setattr("horizon.ccr.response_handler.get_compression_store", lambda: _Primary())
    fo = get_fold_originals()
    key = "a" * 24
    assert fo.put("sess", key, "x" * 10)
    fo.evict_all()
    result = CCRResponseHandler()._execute_retrieval(CCRToolCall(tool_call_id="t1", hash_key=key))
    assert result.success is False
    assert "no longer available" in result.content


def test_warm_resend_keeps_the_stub_when_its_original_is_gone():
    raw = _conversation(old=2)
    store = _store()
    cold = _run(raw, store=store, cold=True)
    assert cold.changed
    get_fold_originals().evict_all()
    warm = _run(raw + [{"role": "assistant", "content": "x"}], store=store, cold=False)
    assert warm.changed
    for old_m, new_m in zip(cold.messages, warm.messages):
        assert json.dumps(old_m, sort_keys=True) == json.dumps(new_m, sort_keys=True)


# ---------------------------------------------------------------- flash coexistence

BIG_GREP = "grep hit line of output\n" * 130  # ~3.2 KB: under flash's 8 KB, over fold's 2 KB


def _flash_on_messages(first_input=None, first_name="Bash", second_name="Grep", second_input=None):
    msgs: list = [{"role": "user", "content": "start"}]
    msgs += _tool_turn(first_name, "toolu_flash", text=BIG, inp=first_input)
    msgs += _tool_turn(second_name, "toolu_grep", text=BIG_GREP, inp=second_input)
    for _ in range(12):
        msgs += [{"role": "assistant", "content": "x"}, {"role": "user", "content": "ok"}]
    msgs.append({"role": "user", "content": "end"})
    return msgs


def test_fold_skips_what_flash_owns_and_folds_the_rest():
    from horizon.transforms import flash_observations as flash

    msgs = _flash_on_messages()
    excl_ids, excl_sigs = flash.fold_exclusions(msgs, horizon=0, policy=flash.FlashPolicy())
    assert excl_ids == {"toolu_flash"}
    out = _run(msgs, store=_store(), cold=True, exclude_ids=excl_ids, exclude_signatures=excl_sigs)
    folded = {d.tool_use_id for d in out.new}
    assert folded == {"toolu_grep"}
    assert out.skipped.get("flash_owned", 0) == 1


def test_fold_skips_a_call_that_partners_a_flash_stubbed_rerun():
    from horizon.transforms import flash_observations as flash

    msgs = _flash_on_messages(
        first_input={"pattern": "x"}, first_name="Grep", second_input={"pattern": "x"}
    )
    excl_ids, excl_sigs = flash.fold_exclusions(msgs, horizon=0, policy=flash.FlashPolicy())
    out = _run(msgs, store=_store(), cold=True, exclude_ids=excl_ids, exclude_signatures=excl_sigs)
    assert "toolu_grep" not in {d.tool_use_id for d in out.new}
    assert out.skipped.get("flash_rerun_partner", 0) == 1


def test_flash_never_restubs_a_fold_stub_and_fold_never_refolds_a_flash_stub():
    from horizon.transforms import flash_observations as flash

    fold_stub = fold.build_stub("Grep", BIG_GREP, "b" * 24)
    msgs = [
        {"role": "user", "content": "start"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": fold_stub}],
        },
    ]
    result = flash.apply_flash(msgs, horizon=0, policy=flash.FlashPolicy(min_chars=10))
    assert result.stubbed == 0

    flash_stub = flash.build_stub("Bash", BIG)
    msgs2 = _tool_turn("Bash", "toolu_fs", text=flash_stub) + [
        {"role": "assistant", "content": "x"},
        {"role": "user", "content": "end"},
    ]
    msgs2 = (
        [{"role": "user", "content": "start"}]
        + msgs2
        + [{"role": "assistant", "content": "x"}] * 12
        + [{"role": "user", "content": "end"}]
    )
    out = _run(msgs2, store=_store(), cold=True)
    assert "toolu_fs" not in {d.tool_use_id for d in out.new}
    assert out.skipped.get("already_stub", 0) >= 1
