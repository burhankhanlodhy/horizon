"""Flash Observations for OpenAI-format requests: stub on the next turn.

The Claude version (:mod:`horizon.transforms.flash_observations`) shows a large
tool output once through a turn-scoped system message. OpenAI's APIs have no
such message, so this version gets the same economics by editing the history
itself:

* **Turn T.** The newest large output goes out in full, with a one-line notice
  that it is shown for this turn only.
* **Turn T+1 on.** The client re-sends it, and Horizon forwards a short stub
  (first and last lines) in its place.

The edit always lands just after the newest model turn, so it costs no cache:
the provider's automatic prefix cache still matches everything before it, and
everything after it is new input on that turn anyway. Each output is then
billed once in full instead of being re-read on every later turn, and long
sessions stay under whole-request price tiers (272k on GPT-6.x) for longer.

Formats:
    * Responses API ``input`` items: ``function_call_output``,
      ``custom_tool_call_output`` and ``local_shell_call_output``, matched to
      their call by ``call_id``. Requests that carry ``previous_response_id``
      or ``conversation`` keep history on the server and are left alone.
    * Chat Completions ``messages``: ``role: "tool"`` messages, matched by
      ``tool_call_id`` to the assistant's ``tool_calls``.

CONTRACT
    * Deterministic. A stub is a pure function of the client's original output
      (looked up by call id, never taken from text another stage compressed or
      replayed), so every later request forwards the same bytes.
    * Only outputs the model has already answered are stubbed, and only at or
      after the conversation's horizon: the start of the newest tool-output
      tail when Horizon first saw the conversation. Nothing the model saw
      before the feature was on is rewritten.
    * Stubs carry no retrieval hash. In the live Claude runs an offered
      retrieval was sometimes taken every turn, which put each output back in
      the cached prefix at about twice the cost of not flashing.

Never raises: on any error the input is returned unchanged.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable
from typing import Any

from horizon.transforms.flash_observations import (
    FlashPolicy,
    FlashResult,
    build_stub,
    call_signature,
    ccr_key,
    first_rerun,
)

logger = logging.getLogger(__name__)

#: Shell, search and listing tools of Codex, opencode, Copilot CLI and similar.
#: ``read_file`` / ``read`` are opt-in, as ``Read`` is for Claude.
DEFAULT_OPENAI_TOOLS = (
    "shell",
    "shell_command",
    "exec_command",
    "local_shell",
    "container.exec",
    "grep_files",
    "list_dir",
    "bash",
    "grep",
    "glob",
    "list",
    "ls",
)
OUTPUT_ITEM_TYPES = frozenset(
    {"function_call_output", "custom_tool_call_output", "local_shell_call_output"}
)
CALL_ITEM_TYPES = frozenset({"function_call", "custom_tool_call", "local_shell_call"})
#: Items the model produced: an output followed by one of these has been answered.
MODEL_ITEM_TYPES = frozenset(
    {
        "reasoning",
        "function_call",
        "custom_tool_call",
        "local_shell_call",
        "apply_patch_call",
        "web_search_call",
        "file_search_call",
        "computer_call",
        "code_interpreter_call",
        "image_generation_call",
        "mcp_call",
    }
)
# Asks for exact details in the visible reply: in a live DeepSeek run the model
# kept them only in its reasoning and later invented the error values.
NOTICE = (
    "\n\n[Horizon flash: this output is shown in full for this turn only. From the "
    "next turn only its first and last lines remain, so write the exact details you "
    "will need later (names, numbers, paths, error messages) in your reply text, "
    "not only in your reasoning.]"
)


def _strip_notice(text: str) -> str:
    return text[: -len(NOTICE)] if text.endswith(NOTICE) else text


def _parts_text(value: Any) -> str | None:
    """Plain text of an output (string or text parts), ``None`` for anything else."""
    if isinstance(value, str):
        return value
    if not isinstance(value, list) or not value:
        return None
    parts: list[str] = []
    for part in value:
        if isinstance(part, str):
            parts.append(part)
            continue
        if not isinstance(part, dict) or part.get("type") not in (
            "input_text",
            "output_text",
            "text",
        ):
            return None
        text = part.get("text")
        if not isinstance(text, str):
            return None
        parts.append(text)
    return "\n".join(parts)


def _with_notice(value: Any) -> Any:
    if isinstance(value, str):
        return value if value.endswith(NOTICE) else value + NOTICE
    if isinstance(value, list):
        if value and isinstance(value[-1], dict) and value[-1].get("text") == NOTICE.strip():
            return value
        part_type = "input_text"
        for part in value:
            if isinstance(part, dict) and isinstance(part.get("type"), str):
                part_type = part["type"]
                break
        return [*value, {"type": part_type, "text": NOTICE.strip()}]
    return value


def _call_id(item: dict[str, Any]) -> str | None:
    value = item.get("call_id") or (
        item.get("id") if item.get("type") == "local_shell_call_output" else None
    )
    return value if isinstance(value, str) else None


# -- Responses API ----------------------------------------------------------------


def responses_tool_names(items: Iterable[Any]) -> dict[str, str]:
    names: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("type") not in CALL_ITEM_TYPES:
            continue
        call_id = item.get("call_id") or item.get("id")
        if not isinstance(call_id, str):
            continue
        name = "local_shell" if item["type"] == "local_shell_call" else item.get("name")
        if isinstance(name, str):
            names[call_id] = name
    return names


def _is_model_item(item: Any) -> bool:
    return isinstance(item, dict) and (
        item.get("type") in MODEL_ITEM_TYPES or item.get("role") == "assistant"
    )


def _last_index(items: list[Any], predicate: Any) -> int:
    for index in range(len(items) - 1, -1, -1):
        if predicate(items[index]):
            return index
    return -1


def responses_tail_start(items: list[Any]) -> int:
    """Index just after the newest model item: where the newest outputs begin."""
    return _last_index(items, _is_model_item) + 1


def responses_originals(items: Iterable[Any]) -> dict[str, str]:
    """``call_id`` -> the client's original output text, notice removed."""
    out: dict[str, str] = {}
    for item in items:
        if isinstance(item, dict) and item.get("type") in OUTPUT_ITEM_TYPES:
            call_id = _call_id(item)
            text = _parts_text(item.get("output"))
            if call_id and text is not None:
                out[call_id] = _strip_notice(text)
    return out


def apply_responses(
    items: list[Any],
    *,
    horizon: int,
    policy: FlashPolicy,
    originals: dict[str, str] | None = None,
) -> FlashResult:
    """Stub answered outputs at or after ``horizon``; mark the unanswered ones.

    ``originals`` maps ``call_id`` to the client's own output text (see
    :func:`responses_originals`); without it the text in ``items`` is used.
    """
    try:
        return _apply(items, horizon, policy, originals, chat=False)
    except Exception:
        logger.debug("flash (responses) skipped", exc_info=True)
        return FlashResult(messages=list(items))


# -- Chat Completions --------------------------------------------------------------


def chat_tool_names(messages: Iterable[Any]) -> dict[str, str]:
    names: dict[str, str] = {}
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for call in msg.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            name = function.get("name") if isinstance(function, dict) else None
            if isinstance(call.get("id"), str) and isinstance(name, str):
                names[call["id"]] = name
    return names


def chat_tail_start(messages: list[Any]) -> int:
    return _last_index(messages, lambda m: isinstance(m, dict) and m.get("role") == "assistant") + 1


def chat_originals(messages: Iterable[Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "tool":
            call_id = msg.get("tool_call_id")
            text = _parts_text(msg.get("content"))
            if isinstance(call_id, str) and text is not None:
                out[call_id] = _strip_notice(text)
    return out


def apply_chat(
    messages: list[Any],
    *,
    horizon: int,
    policy: FlashPolicy,
    originals: dict[str, str] | None = None,
) -> FlashResult:
    try:
        return _apply(messages, horizon, policy, originals, chat=True)
    except Exception:
        logger.debug("flash (chat) skipped", exc_info=True)
        return FlashResult(messages=list(messages))


# -- shared -----------------------------------------------------------------------


def _apply(
    items: list[Any],
    horizon: int,
    policy: FlashPolicy,
    originals: dict[str, str] | None,
    *,
    chat: bool,
) -> FlashResult:
    if chat:
        names = chat_tool_names(items)
        answered_before = chat_tail_start(items)
        field_name = "content"
    else:
        names = responses_tool_names(items)
        answered_before = responses_tail_start(items)
        field_name = "output"
    # First pass: the outputs this policy covers, by index.
    eligible: dict[int, tuple[str, str]] = {}
    for index, item in enumerate(items):
        is_output = isinstance(item, dict) and (
            item.get("role") == "tool" if chat else item.get("type") in OUTPUT_ITEM_TYPES
        )
        if index < horizon or not is_output:
            continue
        call_id = item.get("tool_call_id") if chat else _call_id(item)
        if not isinstance(call_id, str) or not policy.allows(names.get(call_id, "")):
            continue
        current = _parts_text(item.get(field_name))
        original = (originals or {}).get(call_id)
        if original is None and current is not None:
            original = _strip_notice(current)
        if original is None or len(original) < policy.min_chars:
            continue
        eligible[index] = (call_id, original)

    result = FlashResult(messages=[])
    outputs = chat_originals(items) if chat else responses_originals(items)
    outputs.update(originals or {})
    result.paused_at = first_rerun(
        _call_signatures(items, chat=chat),
        outputs,
        {call_id: index for index, (call_id, _) in eligible.items()},
    )
    out = result.messages
    for index, item in enumerate(items):
        if index not in eligible or (result.paused_at is not None and index > result.paused_at):
            out.append(item)
            continue
        call_id, original = eligible[index]
        if index < answered_before:
            stub = build_stub(names[call_id], original)
            out.append({**item, field_name: stub})
            result.stubbed += 1
            result.chars_kept_out += len(original)
            result.keys.append((ccr_key(original), original))
        else:
            out.append({**item, field_name: _with_notice(item.get(field_name))})
            result.flashed += 1
    return result


def _call_signatures(items: list[Any], *, chat: bool) -> dict[str, tuple[int, str]]:
    """Call id -> (index of the call, signature) for every tool call in ``items``."""
    calls: dict[str, tuple[int, str]] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        if chat:
            if item.get("role") != "assistant":
                continue
            for call in item.get("tool_calls") or []:
                function = call.get("function") if isinstance(call, dict) else None
                if isinstance(function, dict) and isinstance(call.get("id"), str):
                    calls[call["id"]] = (
                        index,
                        call_signature(str(function.get("name")), function.get("arguments")),
                    )
            continue
        kind = item.get("type")
        if kind not in CALL_ITEM_TYPES:
            continue
        call_id = item.get("call_id") or item.get("id")
        if not isinstance(call_id, str):
            continue
        if kind == "local_shell_call":
            sig = call_signature("local_shell", item.get("action"))
        elif kind == "custom_tool_call":
            sig = call_signature(str(item.get("name")), item.get("input"))
        else:
            sig = call_signature(str(item.get("name")), item.get("arguments"))
        calls[call_id] = (index, sig)
    return calls


def conversation_key(items: Iterable[Any]) -> str:
    """The first user message's content (Responses items or Chat messages)."""
    for item in items:
        if isinstance(item, dict) and item.get("role") == "user":
            raw = json.dumps(item.get("content"), sort_keys=True, ensure_ascii=False, default=str)
            return hashlib.blake2b(raw.encode("utf-8", "ignore"), digest_size=12).hexdigest()
    return ""


DEFAULT_MIN_READ_RATIO = 0.04
_DECISIONS: dict[str, tuple[bool, str]] = {}


def flash_pays_for(model: str) -> tuple[bool, str]:
    """Whether next-turn stubbing is worth doing for ``model``, and why.

    Stubbing saves the re-reads of an old output on every later turn. Where a
    cache read costs almost nothing and there is no cache-write premium, that
    saving is close to zero while the risk (the model needing an output it no
    longer sees) stays. Live runs, 2026-10-09: GPT-6.1 Sol (reads 0.05x input)
    saved 53%; DeepSeek V4 Pro (0.008x-0.033x) saved nothing and one session
    invented values from a stubbed log.

    Skips the model when its list cache-read price is below
    ``HORIZON_FLASH_MIN_READ_RATIO`` x its input price (default 0.04) and it
    has no write premium. A model Horizon cannot price is flashed: the feature
    is opt-in and gated by host, and an upstream that does not cache at all is
    where stubbing pays most. ``HORIZON_FLASH_MIN_READ_RATIO=0`` turns the
    check off.
    """
    from horizon.proxy import runtime_env

    raw = runtime_env.getenv("HORIZON_FLASH_MIN_READ_RATIO", "") or ""
    try:
        threshold = float(raw) if raw.strip() else DEFAULT_MIN_READ_RATIO
    except ValueError:
        threshold = DEFAULT_MIN_READ_RATIO
    if threshold <= 0:
        return True, "price check off"
    key = f"{model.lower()}|{threshold}"
    if key in _DECISIONS:
        return _DECISIONS[key]
    decision = _decide(model, threshold)
    _DECISIONS[key] = decision
    return decision


def _decide(model: str, threshold: float) -> tuple[bool, str]:
    try:
        from horizon.pricing.counterfactual import resolve_rates

        rates = resolve_rates(model)
    except Exception:
        rates = None
    if rates is None or not rates.uncached:
        return True, "price unknown"
    ratio = rates.read / rates.uncached
    write_premium = rates.write_5m > rates.uncached * 1.05
    if ratio < threshold and not write_premium:
        return False, (
            f"cache reads cost {ratio:.3f}x input (below {threshold:g}x) and there is no "
            "write premium: re-reading old outputs is nearly free"
        )
    return True, f"cache reads cost {ratio:.3f}x input"


def flash_openai_enabled() -> bool:
    from horizon.proxy import runtime_env

    value = runtime_env.getenv("HORIZON_FLASH_OPENAI", "") or ""
    return value.strip().lower() in ("1", "true", "yes", "on")


#: OpenAI API and the ChatGPT (Codex subscription) backend.
OPENAI_HOSTS = frozenset({"api.openai.com", "chatgpt.com"})


def upstream_supports_flash_openai(url: str) -> bool:
    """OpenAI's own hosts, plus hosts listed in ``HORIZON_FLASH_OPENAI_UPSTREAMS``
    (``*`` for any host).

    Another OpenAI-compatible provider may cache differently or reject an
    edited history next to its reasoning state, so add a host (comma list, a
    host also matches its subdomains) only after ``preflight_openai.py`` has
    passed on it. The list applies to this feature only: a gateway can pass
    this preflight and fail the Claude one (ModelFlare did, 2026-10-09).
    ``HORIZON_FLASH_ANY_UPSTREAM=1`` still allows every host, for both.
    """
    from urllib.parse import urlparse

    from horizon.proxy import runtime_env

    override = (runtime_env.getenv("HORIZON_FLASH_ANY_UPSTREAM", "") or "").strip().lower()
    if override in ("1", "true", "yes", "on"):
        return True
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if host in OPENAI_HOSTS:
        return True
    extra = runtime_env.getenv("HORIZON_FLASH_OPENAI_UPSTREAMS", "") or ""
    allowed = {h.strip().lower().lstrip(".") for h in extra.split(",") if h.strip()}
    if "*" in allowed:
        # Any host (HORIZON_SAVINGS=auto): a stub only shrinks what is billed, and
        # a host that rejects a flashed request is retried unflashed and switched
        # off (horizon.proxy.flash_guard).
        return bool(host)
    return any(host == h or host.endswith("." + h) for h in allowed)
