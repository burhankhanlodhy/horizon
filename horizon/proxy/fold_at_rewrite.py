"""Fold stale tool output at cache rewrites (Phase 3, step 2).

A Claude Code session re-sends its whole transcript every turn, so a 40 KB
``Bash`` output read twenty turns ago is paid for again on every request until
the session ends. Folding that output to a short stub is only worth doing when
the prompt cache is being rewritten anyway: a rewrite of a *warm* prefix throws
away a cache read (0.1x) and pays the write premium (1.25x) for the new prefix,
which costs more than the tokens the fold saves. Measured on the owner's real
sessions (count-only replay): folding at cold moments saves about 9% of the bill;
folding in batches while the cache is warm loses money.

So the contract is:

* **Warm requests never fold new results.** They only re-apply the decisions
  already recorded for the session, byte for byte, so the cached prefix stays
  identical to what the cold request forwarded.
* **Cold requests fold old, eligible results** and record each decision (tool
  use id -> stub text and retrieval key). The original text goes into the fold
  originals store (horizon.cache.fold_originals: its own byte-capped store, 24 h
  TTL, evicting only idle sessions), under the key the existing
  ``Retrieve more: hash=`` marker detector already recognises, so the retrieve
  tool is injected and the model can get the full output back.
* **Shadow mode** (``HORIZON_FOLD_AT_REWRITE=shadow``) computes what a cold
  request would fold and the token delta, and changes no bytes and stores
  nothing.

Cold detection is a classifier over three independent signals, because the
prefix tracker alone is not reliable: a lapsed tracker is usually discarded and
recreated (idle reads 0 on the new one), so a cold 5-minute cache can look like a
brand-new session. The signals are the tracker's idle gap, a per-session
last-seen clock that keep-alive pings also refresh, and "a resumed session with
prior assistant turns but no tracker". Conflicting evidence resolves to warm,
because a wrong "cold" busts a warm cache and a missed "cold" only costs the
fold for one turn.

Stub format::

    [Horizon folded: earlier Bash output, 412 lines, 18340 chars (~4585 tokens), exit code 1. First lines:
    <first 8 lines, each clipped to 200 chars>
    Last lines:
    <last 8 lines>
    Full text kept. Retrieve more: hash=<24 hex>]

Eligibility (all must hold): tool in the allow-list (Bash, Grep, Glob, LS,
WebFetch, WebSearch, mcp__* except retrieval tools) or a ``Read`` whose file was
later re-read, edited or written; the result is at least ``age_turns`` assistant
turns old; it is not the last user message; it is not an error; its text is at
least ``min_chars`` characters and entirely text blocks; the stub is at least 20%
smaller than the original.

Never raises from :func:`run_fold`: on any error the messages are returned
unchanged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from bisect import bisect_right
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from horizon.cache.fold_originals import (
    FoldOriginalStore,
    get_fold_originals,
    reset_fold_originals,
)
from horizon.transforms.flash_observations import FLASH_MARKER, call_signature

logger = logging.getLogger(__name__)

FLAG_ENV = "HORIZON_FOLD_AT_REWRITE"
AGE_TURNS_ENV = "HORIZON_FOLD_AGE_TURNS"
MIN_CHARS_ENV = "HORIZON_FOLD_MIN_CHARS"
DEFAULT_AGE_TURNS = 10
DEFAULT_MIN_CHARS = 2_000
COLD_MARGIN_SECONDS = 60.0

STUB_PREFIX = "[Horizon folded:"
STUB_LINES = 8
STUB_LINE_CHARS = 200
MIN_GAIN_RATIO = 0.8  # the stub must be at most 80% of the original
MAX_SESSIONS = 512
MAX_DECISIONS_PER_SESSION = 8192

FOLD_TOOLS = frozenset({"Bash", "Grep", "Glob", "LS", "WebFetch", "WebSearch"})
READ_TOOL = "Read"
# Tools whose calls touch a file: a later one supersedes an earlier Read of it.
TOUCH_TOOLS = frozenset({"Read", "Edit", "Write", "MultiEdit", "NotebookEdit"})
_EXIT_RE = re.compile(r"(?i)exit\s*code\s*[:=]?\s*(-?\d+)")
_TRUTHY = ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def fold_mode() -> str:
    """``"off"`` (default), ``"on"`` or ``"shadow"`` from HORIZON_FOLD_AT_REWRITE."""
    raw = os.environ.get(FLAG_ENV, "").strip().lower()
    if raw == "shadow":
        return "shadow"
    if raw in _TRUTHY:
        return "on"
    return "off"


def _env_int(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


def age_turns() -> int:
    return _env_int(AGE_TURNS_ENV, DEFAULT_AGE_TURNS, 1)


def min_chars() -> int:
    return _env_int(MIN_CHARS_ENV, DEFAULT_MIN_CHARS, 1)


# ---------------------------------------------------------------------------
# Cold classification
# ---------------------------------------------------------------------------


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out and out not in (float("inf"), float("-inf")) else default


def classify_cold(
    *,
    cache_ttl_seconds: int | None,
    last_seen_age: float | None,
    tracker_has_history: bool,
    tracker_idle_seconds: Any,
    has_prior_turns: bool,
    margin: float = COLD_MARGIN_SECONDS,
) -> tuple[bool, str]:
    """Whether this request's prompt-cache prefix is already dead (so a rewrite is free).

    ``cache_ttl_seconds`` is the request's own provider TTL (``None`` = caching
    off, so nothing can be busted). ``last_seen_age`` is seconds since any request
    or keep-alive ping for this session (``None`` if unknown). The tracker fields
    describe the conversation lineage. Returns ``(cold, reason)``.
    """
    if cache_ttl_seconds is None:
        return True, "cache_off"
    limit = float(cache_ttl_seconds) + float(margin)
    idle = _as_float(tracker_idle_seconds)
    if last_seen_age is not None and last_seen_age <= limit:
        return False, "warm"
    if tracker_has_history and idle <= limit:
        return False, "warm"
    if last_seen_age is not None:
        return True, "idle"
    if tracker_has_history:
        return True, "idle"
    if has_prior_turns:
        return True, "resumed"
    return False, "new_session"


# ---------------------------------------------------------------------------
# Per-session state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FoldDecision:
    """One folded tool result: the stub the session carries from now on, forever."""

    tool_use_id: str
    tool: str
    stub: str
    key: str
    original_chars: int


class _SessionState:
    __slots__ = ("decisions", "last_seen")

    def __init__(self) -> None:
        self.decisions: dict[str, FoldDecision] = {}
        self.last_seen: float | None = None


class FoldSessionStore:
    """Bounded, thread-safe per-session fold decisions and last-seen clock.

    Decisions are never silently dropped while their session is alive: dropping
    one changes the bytes of a prefix the provider may still have cached. Their
    only bounds are the per-session cap (new folds stop) and the LRU session cap.
    """

    def __init__(
        self,
        *,
        max_sessions: int = MAX_SESSIONS,
        max_decisions: int = MAX_DECISIONS_PER_SESSION,
        clock: Callable[[], float] = time.time,
        originals: FoldOriginalStore | None = None,
    ) -> None:
        self.originals = originals if originals is not None else get_fold_originals()
        self._max_sessions = max(1, int(max_sessions))
        self._max_decisions = max(1, int(max_decisions))
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: OrderedDict[str, _SessionState] = OrderedDict()

    def _get(self, session_id: str, create: bool) -> _SessionState | None:
        state = self._sessions.get(session_id)
        if state is None:
            if not create:
                return None
            state = _SessionState()
            self._sessions[session_id] = state
            while len(self._sessions) > self._max_sessions:
                self._sessions.popitem(last=False)
        else:
            self._sessions.move_to_end(session_id)
        return state

    def touch(self, session_id: str, now: float | None = None) -> None:
        """A request or keep-alive ping read this session's cache now."""
        if not session_id:
            return
        current = self._clock() if now is None else float(now)
        self.originals.mark_active(session_id, current)
        with self._lock:
            state = self._get(session_id, create=True)
            if state is not None:
                state.last_seen = current

    def last_seen_age(self, session_id: str, now: float | None = None) -> float | None:
        with self._lock:
            state = self._get(session_id, create=False)
            if state is None or state.last_seen is None:
                return None
            current = self._clock() if now is None else float(now)
            return max(0.0, current - state.last_seen)

    def decisions(self, session_id: str) -> dict[str, FoldDecision]:
        with self._lock:
            state = self._get(session_id, create=False)
            return dict(state.decisions) if state is not None else {}

    def remaining_capacity(self, session_id: str) -> int:
        with self._lock:
            state = self._get(session_id, create=False)
            used = len(state.decisions) if state is not None else 0
            return max(0, self._max_decisions - used)

    def record(self, session_id: str, decisions: Iterable[FoldDecision]) -> None:
        with self._lock:
            state = self._get(session_id, create=True)
            assert state is not None
            for decision in decisions:
                if len(state.decisions) >= self._max_decisions:
                    break
                state.decisions.setdefault(decision.tool_use_id, decision)

    def reset(self) -> None:
        with self._lock:
            self._sessions.clear()


_store_lock = threading.Lock()
_store: FoldSessionStore | None = None


def get_fold_store() -> FoldSessionStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = FoldSessionStore()
        return _store


def reset_fold_store() -> None:
    """Forget every session's decisions and every fold original (tests, operator reset)."""
    global _store
    with _store_lock:
        _store = None
    reset_fold_originals()


# ---------------------------------------------------------------------------
# Stub and eligibility
# ---------------------------------------------------------------------------


def _clip(line: str) -> str:
    return line if len(line) <= STUB_LINE_CHARS else line[:STUB_LINE_CHARS] + " …"


def _exit_code(text: str) -> str | None:
    matches = list(_EXIT_RE.finditer(text[-400:]))
    return matches[-1].group(1) if matches else None


def build_stub(tool: str, text: str, key: str) -> str:
    """The deterministic stub for one folded result. Pure function of its inputs."""
    lines = text.splitlines()
    code = _exit_code(text) if tool == "Bash" else None
    exit_part = f", exit code {code}" if code is not None else ""
    parts = [
        f"{STUB_PREFIX} earlier {tool} output, {len(lines)} lines, {len(text)} chars "
        f"(~{len(text) // 4} tokens){exit_part}. First lines:",
    ]
    parts += [_clip(x) for x in lines[:STUB_LINES]]
    if len(lines) > 2 * STUB_LINES:
        parts.append("Last lines:")
        parts += [_clip(x) for x in lines[-STUB_LINES:]]
    parts.append(f"Full text kept. Retrieve more: hash={key}]")
    return "\n".join(parts)


def ccr_key(original: str) -> str:
    """The key ``original`` is filed under (SHA-256[:24], as the CCR store names it)."""
    return hashlib.sha256(original.encode("utf-8", "surrogatepass")).hexdigest()[:24]


def _result_text(block: Mapping[str, Any]) -> str | None:
    """Plain text of a tool_result, or None if it carries anything but text."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content:
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "text":
                return None
            text = item.get("text")
            if not isinstance(text, str):
                return None
            parts.append(text)
        return "\n".join(parts)
    return None


def _count_text_of_block(block: Mapping[str, Any]) -> str:
    text = _result_text(block)
    if text is not None:
        return text
    return json.dumps(block.get("content"), ensure_ascii=False, default=str)


def _allowed_tool(name: str) -> bool:
    if name in FOLD_TOOLS:
        return True
    lowered = name.lower()
    return lowered.startswith("mcp__") and "retrieve" not in lowered


@dataclass
class _Candidate:
    message_index: int
    tool_use_id: str
    tool: str
    text: str


def _eligible(
    messages: list[Any],
    existing: Mapping[str, FoldDecision],
    age: int,
    floor_chars: int,
    exclude_ids: frozenset[str] = frozenset(),
    exclude_signatures: frozenset[str] = frozenset(),
) -> tuple[list[_Candidate], dict[str, int]]:
    uses: dict[str, tuple[str, Mapping[str, Any], int]] = {}
    asst_pos: list[int] = []
    last_touch: dict[str, int] = {}
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        asst_pos.append(i)
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = str(block.get("name") or "")
            tid = block.get("id")
            inp = block.get("input") if isinstance(block.get("input"), dict) else {}
            if isinstance(tid, str):
                uses[tid] = (name, inp, i)
            if name in TOUCH_TOOLS:
                path = inp.get("file_path") or inp.get("notebook_path")
                if isinstance(path, str) and path:
                    last_touch[path] = max(last_touch.get(path, -1), i)

    last_user = -1
    for i, msg in enumerate(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user = i

    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    candidates: list[_Candidate] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tid = block.get("tool_use_id")
            if not isinstance(tid, str) or tid in existing:
                continue
            use = uses.get(tid)
            if use is None:
                skip("no_tool_use")
                continue
            name, inp, a = use
            if a >= i:
                skip("malformed")
                continue
            if tid in exclude_ids:
                skip("flash_owned")
                continue
            if call_signature(name, inp) in exclude_signatures:
                skip("flash_rerun_partner")
                continue
            if i == last_user:
                skip("last_user_message")
                continue
            turns_after = len(asst_pos) - bisect_right(asst_pos, i)
            if turns_after < age:
                skip("recent")
                continue
            if block.get("is_error"):
                skip("error")
                continue
            if name == READ_TOOL:
                path = inp.get("file_path")
                if not isinstance(path, str) or not path:
                    skip("read_no_path")
                    continue
                if last_touch.get(path, -1) <= a:
                    skip("read_live")
                    continue
            elif not _allowed_tool(name):
                skip("tool_not_allowed")
                continue
            text = _result_text(block)
            if text is None:
                skip("non_text")
                continue
            if text.startswith((STUB_PREFIX, FLASH_MARKER)):
                skip("already_stub")
                continue
            if len(text) < floor_chars:
                skip("small")
                continue
            candidates.append(_Candidate(i, tid, name, text))
    return candidates, skipped


def _raw_texts(original_messages: list[Any] | None) -> dict[str, str]:
    """Tool result texts as the client sent them (before any compression)."""
    out: dict[str, str] = {}
    for msg in original_messages or []:
        if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
            continue
        for block in msg["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tid = block.get("tool_use_id")
                text = _result_text(block)
                if isinstance(tid, str) and text is not None:
                    out[tid] = text
    return out


# ---------------------------------------------------------------------------
# The fold
# ---------------------------------------------------------------------------

#: ``(session_id, original, key) -> bool``: files ``original`` under ``key`` in the fold
#: store. False means it was refused (store full of live sessions): the result is not folded.
Writer = Callable[[str, str, str], bool]


@dataclass
class FoldOutcome:
    messages: list[Any]
    changed: bool = False
    new: list[FoldDecision] = field(default_factory=list)
    reapplied: int = 0
    token_delta: int = 0
    chars_before: int = 0
    chars_after: int = 0
    would_fold: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    mode: str = "off"


def run_fold(
    messages: list[Any],
    *,
    mode: str,
    cold: bool,
    session_id: str,
    store: FoldSessionStore,
    original_messages: list[Any] | None = None,
    count_text: Callable[[str], int] | None = None,
    age: int | None = None,
    floor_chars: int | None = None,
    writer: Writer | None = None,
    exclude_ids: frozenset[str] = frozenset(),
    exclude_signatures: frozenset[str] = frozenset(),
) -> FoldOutcome:
    """Re-apply recorded fold decisions, and on a cold request fold new results.

    ``mode`` is ``"on"`` or ``"shadow"`` (``"off"`` returns the messages untouched).
    On a warm request (``cold=False``) only the decisions already recorded for
    ``session_id`` are applied, so the forwarded prefix stays byte-identical to the
    cold request that created them. Shadow mode never changes or records anything.
    ``exclude_ids`` / ``exclude_signatures`` are what flash owns on this request (see
    :func:`horizon.transforms.flash_observations.fold_exclusions`). ``writer`` files
    originals (default: the fold originals store). Never raises.
    """
    outcome = FoldOutcome(messages=messages, mode=mode)
    if mode not in ("on", "shadow") or not isinstance(messages, list):
        return outcome
    try:
        return _run(
            messages,
            mode=mode,
            cold=cold,
            session_id=session_id,
            store=store,
            original_messages=original_messages,
            count_text=count_text or (lambda text: len(text) // 4),
            age=age_turns() if age is None else age,
            floor_chars=min_chars() if floor_chars is None else floor_chars,
            writer=writer or _default_writer(store),
            exclude_ids=exclude_ids,
            exclude_signatures=exclude_signatures,
        )
    except Exception:  # noqa: BLE001 - a fold must never break a request
        logger.warning("fold_at_rewrite failed; forwarding messages unchanged", exc_info=True)
        return FoldOutcome(messages=messages, mode=mode)


def _run(
    messages: list[Any],
    *,
    mode: str,
    cold: bool,
    session_id: str,
    store: FoldSessionStore,
    original_messages: list[Any] | None,
    count_text: Callable[[str], int],
    age: int,
    floor_chars: int,
    writer: Writer,
    exclude_ids: frozenset[str],
    exclude_signatures: frozenset[str],
) -> FoldOutcome:
    shadow = mode == "shadow"
    existing: dict[str, FoldDecision] = {} if shadow else store.decisions(session_id)

    new: list[FoldDecision] = []
    skipped: dict[str, int] = {}
    if cold:
        budget = MAX_DECISIONS_PER_SESSION if shadow else store.remaining_capacity(session_id)
        candidates, skipped = _eligible(
            messages, existing, age, floor_chars, exclude_ids, exclude_signatures
        )
        raw = _raw_texts(original_messages) if original_messages else {}
        for cand in candidates:
            if len(new) >= budget:
                skipped["session_cap"] = skipped.get("session_cap", 0) + 1
                continue
            original = raw.get(cand.tool_use_id, cand.text)
            key = ccr_key(original)
            stub = build_stub(cand.tool, cand.text, key)
            if len(stub) >= len(cand.text) * MIN_GAIN_RATIO:
                skipped["no_gain"] = skipped.get("no_gain", 0) + 1
                continue
            if not shadow:
                # The stub is written only once its original is filed: a refused original
                # must never leave a stub whose key cannot be redeemed.
                try:
                    filed = writer(session_id, original, key)
                except Exception:  # noqa: BLE001 - an unstored original must not be stubbed
                    logger.warning("fold_at_rewrite: original store failed; result left unfolded")
                    skipped["store_error"] = skipped.get("store_error", 0) + 1
                    continue
                if not filed:
                    skipped["store_full"] = skipped.get("store_full", 0) + 1
                    continue
            new.append(
                FoldDecision(
                    tool_use_id=cand.tool_use_id,
                    tool=cand.tool,
                    stub=stub,
                    key=key,
                    original_chars=len(cand.text),
                )
            )

    if shadow:
        # Count what a cold request would fold; nothing is stored or rewritten.
        replacements: dict[str, str] = {d.tool_use_id: d.stub for d in new}
        _, changed_pairs, _ = _replace(messages, replacements)
        return FoldOutcome(
            messages=messages,
            changed=False,
            new=[],
            would_fold=len(new),
            token_delta=sum(count_text(o) - count_text(s) for o, s in changed_pairs),
            chars_before=sum(len(o) for o, _ in changed_pairs),
            chars_after=sum(len(s) for _, s in changed_pairs),
            skipped=skipped,
            mode=mode,
        )

    if new:
        store.record(session_id, new)
    replacements = {tid: d.stub for tid, d in existing.items()}
    replacements.update({d.tool_use_id: d.stub for d in new})
    if not replacements:
        return FoldOutcome(messages=messages, skipped=skipped, mode=mode)

    out_messages, changed_pairs, changed = _replace(messages, replacements)
    reapplied = sum(1 for tid in replacements if tid in existing)
    if not changed:
        return FoldOutcome(
            messages=messages, new=new, reapplied=reapplied, skipped=skipped, mode=mode
        )
    return FoldOutcome(
        messages=out_messages,
        changed=True,
        new=new,
        reapplied=reapplied,
        token_delta=sum(count_text(o) - count_text(s) for o, s in changed_pairs),
        chars_before=sum(len(o) for o, _ in changed_pairs),
        chars_after=sum(len(s) for _, s in changed_pairs),
        skipped=skipped,
        mode=mode,
    )


def _replace(
    messages: list[Any], replacements: Mapping[str, str]
) -> tuple[list[Any], list[tuple[str, str]], bool]:
    """Swap the content of each tool_result whose id is in ``replacements``.

    Returns the new message list (the input list when nothing changed), the
    (old text, stub) pairs that changed, and whether anything changed. Blocks are
    replaced by copies that keep every other key, so pairing is intact.
    """
    changed_pairs: list[tuple[str, str]] = []
    out: list[Any] = []
    any_changed = False
    for msg in messages:
        if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
            out.append(msg)
            continue
        new_blocks: list[Any] = []
        msg_changed = False
        for block in msg["content"]:
            stub = (
                replacements.get(block.get("tool_use_id"))
                if isinstance(block, dict) and block.get("type") == "tool_result"
                else None
            )
            if stub is not None and block.get("content") != stub:
                changed_pairs.append((_count_text_of_block(block), stub))
                new_blocks.append({**block, "content": stub})
                msg_changed = True
            else:
                new_blocks.append(block)
        if msg_changed:
            out.append({**msg, "content": new_blocks})
            any_changed = True
        else:
            out.append(msg)
    if not any_changed:
        return messages, [], False
    return out, changed_pairs, True


def _default_writer(store: FoldSessionStore) -> Writer:
    def write(session_id: str, original: str, key: str) -> bool:
        return store.originals.put(session_id, key, original)

    return write


def record_outcome(
    outcome: FoldOutcome,
    *,
    reason: str,
    tags: Any,
    transforms_applied: list[Any],
) -> None:
    """Book one fold pass onto the request. Never raises.

    A live fold adds a ``fold_at_rewrite:<reason>:<new>new:<reapplied>reapplied``
    transform label and a ``tags["fold_at_rewrite"]`` summary. It deliberately adds
    NO savings-attribution row: the fold changes the forwarded messages, so the
    handler's token recount (``tokens_saved = original - optimized``) and the
    verified figure already carry the saving. An attribution row would be a second
    copy of those tokens (attribution rows are shown beside the headline, and only
    savings kept outside ``tokens_saved``, like tool-schema deferral, belong there).
    Shadow mode only writes ``tags["fold_shadow"]``.
    """
    try:
        if outcome.mode == "shadow":
            if outcome.would_fold or outcome.skipped:
                tags["fold_shadow"] = {
                    "reason": reason,
                    "would_fold": outcome.would_fold,
                    "token_delta": outcome.token_delta,
                    "skipped": dict(outcome.skipped),
                }
            return
        if not outcome.changed:
            return
        transforms_applied.append(
            f"fold_at_rewrite:{reason}:{len(outcome.new)}new:{outcome.reapplied}reapplied"
        )
        tags["fold_at_rewrite"] = {
            "reason": reason,
            "new": len(outcome.new),
            "reapplied": outcome.reapplied,
            "token_delta": int(outcome.token_delta),
        }
    except Exception:  # noqa: BLE001 - bookkeeping must never break a response
        logger.debug("fold_at_rewrite bookkeeping skipped", exc_info=True)


def keep_alive_touch_for(
    tracker_touch: Callable[[], None] | None, session_id: str
) -> Callable[[], None]:
    """The keep-alive ping callback: refresh the prefix tracker and the fold clock.

    A ping that reads the session's cache keeps it warm, so it must also count as
    activity for cold detection; otherwise a kept-warm session would look idle.
    """

    def touch() -> None:
        if tracker_touch is not None:
            tracker_touch()
        get_fold_store().touch(session_id)

    return touch
