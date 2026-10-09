"""Novel-vs-repeat attribution for per-request compression savings.

Providers disagree about what a request's ``tokens_saved`` means, and the
disagreement only shows up once something sums it.

Anthropic's cached prefix is frozen -- the router leaves it alone so the
provider's prefix cache keeps hitting -- but the frozen prefix is replayed in
its compressed form, so a turn's ``tokens_saved`` still includes every earlier
removal. Chat Completions behaves the same way. Those handlers key their
running total with :func:`transcript_savings_key` (measured 2026-10-09).

OpenAI's ``/v1/responses`` carries the whole transcript in every request and
the router recompresses all of it, so a turn's ``tokens_saved`` is the running
total of everything removed from that conversation so far. Summing across
turns counts the same removed token once per remaining turn: roughly twenty
times on a twenty-turn conversation.

Measured 2026-09-08 across one machine's retained proxy logs: gpt/codex models
reported 8.10M tokens saved against 3.90M tokens of new input (2.1x, which no
share of new content can be), where Claude reported 4.18M against 43.3M. Two
requests from the same machine and the same minute::

    gpt-6-astra   tok_before=156260 tok_after=80240 tok_saved=76020 cache_read=66304
    claude-opus-5 tok_before=114024 tok_after=113566 tok_saved=458   cache_read=98503

Every cumulative consumer inherits the inflation: lifetime savings totals, the
per-model savings percent, the dollar figure (repeat removals priced at the
full uncached input rate when the alternative was a cache read at ~0.1x), and
any rate dividing savings by new input, which then has no 100% ceiling.

This module converts the cumulative series into the incremental one. Per
request descriptions keep the wire truth -- ``tok_before``/``tok_after``, the
request log and the transformations feed all still say what left this process
-- because those tokens really were removed from this request's payload. Only
the running totals switch to the novel figure, so a removed token is counted
once per conversation instead of once per turn.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from typing import Any

from horizon.proxy.output_savings_policy import _unwrap_response_create_body

__all__ = [
    "ConversationSavings",
    "ResponseChainSavings",
    "gemini_savings_key",
    "is_transcript_running_total",
    "transcript_savings_key",
    "get_conversation_savings",
    "get_response_chain_savings",
    "reset_conversation_savings",
    "savings_conversation_key",
]

_ID_KEYS = ("id", "conversation_id", "session_id", "thread_id")


def _explicit_id(value: Any) -> str:
    if isinstance(value, str):
        return value if value and value.lower() != "auto" else ""
    if isinstance(value, dict):
        for key in _ID_KEYS:
            nested = value.get(key)
            if isinstance(nested, str) and nested:
                return nested
    return ""


def savings_conversation_key(body: Any, *, session_id: str | None = None) -> str | None:
    """Identity under which a Responses request's ``tokens_saved`` is a running total.

    Returns ``None`` unless BOTH premises of the cumulative accounting hold, in
    which case the funnel keeps ordinary per-request accounting:

    1. The request names one conversation. Only an explicit id counts: a
       top-level ``conversation_id``/``session_id``/``thread_id``, one of
       those inside ``metadata``/``client_metadata``, or a transport-scoped
       ``session_id`` the caller vouches for (a WebSocket, a session header).
       Two things are deliberately NOT accepted. The holdout key's fallbacks
       (the instructions prefix, the literal ``responses``): two independent
       conversations with the same instructions would share a running total
       and suppress each other's savings. And ``prompt_cache_key``: it groups
       prompt-cache routing, and OpenAI documents one key shared across a
       user's sessions and forks, or across independent single-turn judging
       requests, so it aliases conversations the same way. An adapter that
       establishes a client sets it uniquely per conversation may pass it as
       ``session_id``; nothing here does.
    2. The request carries the whole transcript. With ``previous_response_id``
       or a server-side ``conversation`` the provider holds prior context and
       the payload is this turn's increment, so ``tokens_saved`` is already
       per-request and must not be differenced.

    Chat-completions bodies (no ``input``) return ``None``; the Chat handler
    keys its own running total with :func:`transcript_savings_key`.
    """
    if not isinstance(body, dict):
        return None
    body = _unwrap_response_create_body(body)
    if "input" not in body:
        return None
    if body.get("previous_response_id") or body.get("conversation"):
        return None

    identity = ""
    for key in ("conversation_id", "session_id", "thread_id"):
        value = _explicit_id(body.get(key))
        if value:
            identity = f"{key}:{value}"
            break
    if not identity:
        for container_key in ("client_metadata", "metadata"):
            container = body.get(container_key)
            if not isinstance(container, dict):
                continue
            for key in (
                "conversation_id",
                "conversation_key",
                "session_id",
                "thread_id",
                "codex_session_id",
            ):
                value = _explicit_id(container.get(key))
                if value:
                    identity = f"{container_key}.{key}:{value}"
                    break
            if identity:
                break
    if not identity and session_id:
        identity = f"session:{session_id}"
    if not identity:
        return None
    return hashlib.sha256(("savings\x00" + identity).encode("utf-8", "ignore")).hexdigest()


TRANSCRIPT_KEY_PREFIX = "transcript:"


def _without_cache_control(value: Any) -> Any:
    """``value`` with every ``cache_control`` dropped: clients move the markers each turn."""
    if isinstance(value, dict):
        return {k: _without_cache_control(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [_without_cache_control(v) for v in value]
    return value


def transcript_savings_key(messages: Any, *, system: Any = None) -> str | None:
    """Identity under which a whole-transcript request's ``tokens_saved`` is a running total.

    Chat Completions and Claude Messages requests carry the whole transcript,
    and every earlier tool output reaches the upstream compressed again
    (recompressed in token mode, replayed from the prefix tracker in cache
    mode), so ``tokens_saved`` repeats each earlier removal on every turn.
    Measured 2026-10-09 through the handlers, five turns: Chat booked 134,745
    tokens for 44,915 ever removed (both modes); Claude booked 44,915 for
    8,983 in cache mode and 191,008 for about 67,800 in token mode, all priced
    as fresh input. Keyed, the funnel books each removal once at the live-zone
    price and its repeats as retained, priced as the cache reads they replace.

    These bodies name no conversation, so the identity is the client's system
    prompt (``system``, or Chat's system and developer messages) and its first
    user message, ignoring ``cache_control``, which clients move every turn.
    Conversations within one account that share both share a running total and
    shift savings between each other; the key is namespaced per account before
    use (``tenant_key``).
    """
    if not isinstance(messages, list):
        return None
    preamble: list[Any] = [system] if system is not None else []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role in ("system", "developer"):
            preamble.append(msg.get("content"))
        elif role == "user":
            import json

            seed = json.dumps(
                _without_cache_control([preamble, msg.get("content")]),
                sort_keys=True,
                default=str,
            )
            digest = hashlib.sha256(("transcript-savings:" + seed).encode("utf-8", "ignore"))
            return TRANSCRIPT_KEY_PREFIX + digest.hexdigest()
    return None


def gemini_savings_key(contents: Any, system_instruction: Any = None) -> str | None:
    """:func:`transcript_savings_key` for Gemini ``contents`` (``parts``, ``user``/``model``)."""
    if not isinstance(contents, list):
        return None
    messages = [
        {"role": c.get("role"), "content": c.get("parts")} for c in contents if isinstance(c, dict)
    ]
    return transcript_savings_key(messages, system=system_instruction)


def is_transcript_running_total(conversation_key: str | None) -> bool:
    """True for a key from :func:`transcript_savings_key`: repeats are not in ``tokens_saved``."""
    return bool(conversation_key) and str(conversation_key).startswith(TRANSCRIPT_KEY_PREFIX)


# Conversations tracked before the oldest is forgotten. A forgotten
# conversation that is still live restarts from zero and re-counts its
# transcript once, so the cap trades a bounded, one-off overcount for bounded
# memory. 512 is far above the number of conversations one client keeps warm.
DEFAULT_MAX_CONVERSATIONS = 512


class ConversationSavings:
    """Cumulative-per-turn savings to novel-this-turn savings, bounded."""

    def __init__(self, max_conversations: int = DEFAULT_MAX_CONVERSATIONS) -> None:
        self._max = max(1, int(max_conversations))
        self._seen: OrderedDict[str, int] = OrderedDict()
        self._lock = threading.Lock()

    def novel(self, conversation_key: str | None, cumulative: int | None) -> int | None:
        """Tokens this request removed for the first time in its conversation.

        ``cumulative`` is the conversation's running removed-token total as of
        this request. Returns ``None`` when the caller cannot supply both a
        conversation key and a total, which means "this path does not
        distinguish" -- the funnel then falls back to ``tokens_saved``, which
        is already novel-only on providers that freeze their cached prefix.
        """
        split = self.split(conversation_key, cumulative)
        return None if split is None else split[0]

    def split(self, conversation_key: str | None, cumulative: int | None) -> tuple[int, int] | None:
        """``(novel, retained)`` for this request's running removed-token total.

        ``retained`` is the part of the total an earlier turn already removed
        and counted, which stays out of this request too. It is a saving again
        on every later turn, at whatever rate the provider would have charged
        for re-sending it (usually a cache read). ``None`` as for :meth:`novel`.
        """
        if not conversation_key or cumulative is None:
            return None

        total = max(0, int(cumulative))
        with self._lock:
            previous = self._seen.get(conversation_key, 0)
            self._seen[conversation_key] = total
            self._seen.move_to_end(conversation_key)
            while len(self._seen) > self._max:
                self._seen.popitem(last=False)

        # A total that went DOWN means the transcript shrank under it: a
        # compaction, or a client that dropped history. Nothing was removed
        # for the first time, and the next turn counts from the lower base
        # rather than waiting for the old high-water mark to be re-reached.
        novel = max(0, total - previous)
        return novel, total - novel


_default = ConversationSavings()


def get_conversation_savings() -> ConversationSavings:
    """Process-wide ledger. One conversation spans many requests."""
    return _default


# Responses kept before the oldest is forgotten. A turn that continues a
# forgotten response carries nothing, which undercounts; it never overcounts.
DEFAULT_MAX_RESPONSES = 4096


class ResponseChainSavings:
    """Removed tokens that stay out of server-held Responses context.

    A request with ``previous_response_id`` sends only new items; the provider
    rebuilds the earlier context from what it stored, which is what Horizon
    forwarded -- compressed. Every token removed earlier in the chain is
    therefore kept out of this request as well, without appearing in its
    ``tokens_saved``. This maps each completed response id to that running
    total so the next turn can claim it. An unknown id (another process, a
    fork the proxy never saw, an evicted entry) carries zero.
    """

    def __init__(self, max_responses: int = DEFAULT_MAX_RESPONSES) -> None:
        self._max = max(1, int(max_responses))
        self._kept: OrderedDict[str, int] = OrderedDict()
        self._lock = threading.Lock()

    def carried(self, previous_response_key: str | None) -> int:
        """Tokens already kept out of the context ``previous_response_key`` names."""
        if not previous_response_key:
            return 0
        with self._lock:
            return self._kept.get(previous_response_key, 0)

    def record(self, response_key: str | None, kept: int) -> None:
        """Remember how many removed tokens the context of ``response_key`` excludes."""
        if not response_key:
            return
        with self._lock:
            self._kept[response_key] = max(0, int(kept))
            self._kept.move_to_end(response_key)
            while len(self._kept) > self._max:
                self._kept.popitem(last=False)


_default_chain = ResponseChainSavings()


def get_response_chain_savings() -> ResponseChainSavings:
    """Process-wide ledger. A chain can outlive the connection that began it."""
    return _default_chain


def reset_conversation_savings() -> None:
    """Forget every conversation and response chain. Test helper only."""
    global _default, _default_chain
    _default = ConversationSavings()
    _default_chain = ResponseChainSavings()
