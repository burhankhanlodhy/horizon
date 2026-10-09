"""Size of the request body this request last sent upstream, for the account ledger.

The shared forwarders (``HorizonProxy._retry_request`` and the streaming path)
record it; the outcome funnel reads it in the same task. Paired with the
provider's billed input tokens it gives the provider's own tokens per byte for
a model (:mod:`horizon.proxy.policy_savings`), which converts the bytes a flash
stub removed into the tokens the provider would have billed. No local tokenizer
matches every provider: on one pytest log, characters / 4 gave 8,452 tokens and
o200k 11,313, while Gemini billed about 16,000, GPT-6.1 Sol about 15,000 and
Claude Opus 5.5 about 19,000 (2026-10-09).
"""

from __future__ import annotations

import contextvars

_bytes: contextvars.ContextVar[int] = contextvars.ContextVar("horizon_forwarded_bytes", default=0)


def note(content: bytes | bytearray | str | None) -> None:
    """Record the size of the body about to be sent."""
    if content is None:
        return
    size = len(content.encode("utf-8")) if isinstance(content, str) else len(content)
    _bytes.set(size)


def get() -> int:
    """Bytes of the last body this request sent (0 when nothing was recorded)."""
    return _bytes.get()
