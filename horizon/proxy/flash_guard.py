"""Safety nets and counters for Flash Observations.

**Reject-and-retry.** When a handler flashes a request it *arms* the guard
with the request's unflashed form. If the upstream answers 400, the shared
forwarders (``HorizonProxy._retry_request`` and the streaming path) send the
unflashed form once instead. If that retry succeeds, the 400 was the flash's
fault: flash is switched off for that host and model for the life of the
process, and the client never sees the error. If the retry fails too, the
error was not the flash's and nothing is switched off. The state lives in a
``ContextVar``, so it never leaks across requests (the same mechanism as the
Flex 429 fallback in :mod:`horizon.proxy.flex_policy`).

**Counters** for ``/stats``: outputs shown once and stubbed, tokens kept out of
the permanent context, models skipped by the price check, re-need pauses, and
automatic switch-offs with their reasons.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# (field, unflashed value, (host, model)) while armed;
# ("retried", key, field, unflashed value) after a retry.
_armed: contextvars.ContextVar[tuple[Any, ...] | None] = contextvars.ContextVar(
    "horizon_flash_armed", default=None
)
# (field, unflashed value) once an unflashed retry was accepted: what was really sent.
_sent: contextvars.ContextVar[tuple[str, Any] | None] = contextvars.ContextVar(
    "horizon_flash_sent", default=None
)
_lock = threading.Lock()
_disabled: dict[tuple[str, str], str] = {}
_counters: dict[str, int] = {
    "requests_flashed": 0,
    "outputs_shown_once": 0,
    "outputs_stubbed": 0,
    "tokens_kept_out": 0,
    "rerun_pauses": 0,
    "rejected_then_retried": 0,
}
_price_skips: dict[str, str] = {}
_paused_conversations: set[str] = set()
_events: list[dict[str, Any]] = []
_MAX_EVENTS = 50


def _key(host: str, model: str) -> tuple[str, str]:
    return (host or "").lower(), (model or "").lower()


def is_disabled(host: str, model: str) -> bool:
    with _lock:
        return _key(host, model) in _disabled


def arm(field: str, unflashed: Any, *, host: str, model: str) -> None:
    """Record the unflashed ``field`` value of the request about to be forwarded."""
    _armed.set((field, unflashed, _key(host, model)))
    _sent.set(None)


def disarm() -> None:
    _armed.set(None)


def sent_value(field: str, forwarded: Any) -> Any:
    """What the upstream actually accepted for ``field`` on this request.

    ``forwarded`` unless an unflashed retry was accepted, in which case the
    unflashed value. Prefix trackers must record this: replaying the rejected
    flashed bytes on the next turn would send the stubs again.
    """
    sent = _sent.get()
    return sent[1] if sent is not None and sent[0] == field else forwarded


def fallback_body(body: dict[str, Any], status_code: int) -> dict[str, Any] | None:
    """The unflashed retry body after a 400 on a flashed request, else ``None``.

    One retry per request: the guard moves to "retried" when this returns a body.
    """
    state = _armed.get()
    if status_code != 400 or state is None or state[0] == "retried":
        return None
    field, unflashed, key = state
    if field not in body:
        return None
    _armed.set(("retried", key, field, unflashed))
    logger.warning("upstream rejected a flashed request (400) for %s/%s; retrying unflashed", *key)
    return {**body, field: unflashed}


def record_retry(status_code: int) -> None:
    """Called with the retry's status: a success switches flash off for that host and model."""
    state = _armed.get()
    _armed.set(None)
    if state is None or state[0] != "retried":
        return
    key = state[1]
    if status_code >= 400:
        logger.info("unflashed retry failed too (%s); flash left on for %s/%s", status_code, *key)
        return
    _sent.set((state[2], state[3]))
    reason = "upstream rejected flashed requests (400) and accepted them unflashed"
    with _lock:
        _disabled[key] = reason
        _counters["rejected_then_retried"] += 1
        _event("flash_off", host=key[0], model=key[1], reason=reason)
    logger.warning("flash observations switched off for %s/%s: %s", key[0], key[1], reason)


def record_flash(*, shown_once: int, stubbed: int, chars_kept_out: int) -> None:
    with _lock:
        _counters["requests_flashed"] += 1
        _counters["outputs_shown_once"] += shown_once
        _counters["outputs_stubbed"] += stubbed
        _counters["tokens_kept_out"] += chars_kept_out // 4


def record_price_skip(model: str, reason: str) -> None:
    with _lock:
        if model not in _price_skips:
            _price_skips[model] = reason
            _event("price_skip", model=model, reason=reason)


def record_rerun_pause(conversation: str) -> None:
    """Once per conversation: the model re-ran a command whose output was stubbed."""
    with _lock:
        if conversation in _paused_conversations:
            return
        _paused_conversations.add(conversation)
        _counters["rerun_pauses"] += 1
        _event(
            "rerun_pause",
            conversation=conversation,
            reason="the model re-ran a command whose output had been stubbed",
        )


def _event(kind: str, **fields: Any) -> None:
    _events.append({"at": round(time.time()), "event": kind, **fields})
    del _events[:-_MAX_EVENTS]


def stats() -> dict[str, Any]:
    with _lock:
        return {
            **_counters,
            "switched_off": [
                {"host": h, "model": m, "reason": r} for (h, m), r in sorted(_disabled.items())
            ],
            "price_skips": dict(sorted(_price_skips.items())),
            "recent_events": list(_events),
        }


def reset_for_tests() -> None:
    with _lock:
        _disabled.clear()
        _price_skips.clear()
        _paused_conversations.clear()
        _events.clear()
        for k in _counters:
            _counters[k] = 0
    _armed.set(None)
    _sent.set(None)
