"""OpenAI prompt-cache writes: reported (GPT-5.6 and later) or inferred.

Before GPT-5.6, OpenAI reported only cache reads (``cached_tokens``) and billed
no write premium, so Horizon inferred a write volume from the uncached tokens
for dashboards and flagged it ``cache_inferred`` (priced as plain input).
GPT-5.6 and later report writes in ``input_tokens_details.cache_write_tokens``
(Chat Completions: ``prompt_tokens_details``) and bill them at 1.25x input;
those are real writes, priced as such, and not part of the uncached tokens.
"""

from __future__ import annotations

from typing import Any


def reported_writes(usage: Any) -> int | None:
    """Cache-write tokens the usage block reports, or ``None`` when it reports none."""
    if not isinstance(usage, dict):
        return None
    for key in ("input_tokens_details", "prompt_tokens_details"):
        details = usage.get(key)
        if isinstance(details, dict) and details.get("cache_write_tokens") is not None:
            try:
                return max(int(details["cache_write_tokens"]), 0)
            except (TypeError, ValueError):
                return None
    return None


def split(input_tokens: int, cache_read: int, writes: int | None) -> tuple[int, int, bool]:
    """``(cache_write, uncached, inferred)`` for one request.

    ``writes`` is :func:`reported_writes`. Reported writes are real and leave
    the rest of the uncached input as fresh input; without them the write
    volume is inferred from the uncached tokens (``inferred=True``).
    """
    if writes is not None:
        return writes, max(int(input_tokens) - int(cache_read) - writes, 0), False
    inferred = max(int(input_tokens) - int(cache_read), 0)
    return inferred, inferred, True
