"""Cache routing ids for providers that route their prompt cache by one (xAI Grok).

xAI keeps a prompt cache per server with no guaranteed lifetime, and routes a
request to the server holding its cache only when the request names its
conversation: the ``x-grok-conv-id`` header on Chat Completions, and
``prompt_cache_key`` on the Responses API. Without one, a request can land on
a server with no cache for it (xAI prompt-caching best practices, 2026-10).
Many clients send neither, so their Grok sessions miss the cache by chance.

When the client set no id of its own, this adds a stable one per conversation:
a hash of the verified account and the conversation's opening (system prompt
and first user message), so two accounts never share an id. A client's own
id always wins. Content is never changed, so the prompt cache is not touched.

``HORIZON_CACHE_ROUTING=1`` (``HORIZON_SAVINGS=auto`` sets it). Never raises.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping, MutableMapping
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

ENABLE_ENV = "HORIZON_CACHE_ROUTING"
GROK_HEADER = "x-grok-conv-id"
XAI_HOSTS = ("x.ai",)


def enabled() -> bool:
    from horizon.proxy import runtime_env

    value = runtime_env.getenv(ENABLE_ENV, "") or ""
    return value.strip().lower() in ("1", "true", "yes", "on")


def is_grok(model: str, url: str = "") -> bool:
    """A Grok model, by name (``grok-4.6``, ``x-ai/grok-4.6``) or by an xAI host."""
    name = (model or "").lower()
    if name.startswith("grok-") or "/grok-" in name:
        return True
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in XAI_HOSTS)


def routing_id(messages: Any, *, system: Any = None) -> str | None:
    """Stable per conversation and per account, or ``None`` without a first user message."""
    from horizon.proxy.account_analytics import tenant_key
    from horizon.proxy.conversation_savings import transcript_savings_key

    key = transcript_savings_key(messages, system=system)
    if not key:
        return None
    scoped = tenant_key(key) or key
    return hashlib.sha256(("cache-routing\x00" + scoped).encode()).hexdigest()[:32]


def apply_chat(
    headers: MutableMapping[str, str], *, model: str, url: str, messages: Any
) -> str | None:
    """Add ``x-grok-conv-id`` to a Grok Chat Completions request without one."""
    try:
        if not enabled() or not is_grok(model, url):
            return None
        if any(str(k).lower() == GROK_HEADER for k in headers):
            return None
        rid = routing_id(messages)
        if rid:
            headers[GROK_HEADER] = rid
        return rid
    except Exception:  # pragma: no cover - a routing hint must never fail a request
        logger.debug("cache routing (chat) skipped", exc_info=True)
        return None


def apply_responses(
    body: MutableMapping[str, Any],
    *,
    model: str,
    url: str,
    headers: Mapping[str, str] | None = None,
) -> str | None:
    """Add ``prompt_cache_key`` to a Grok Responses request without one."""
    try:
        if not enabled() or not is_grok(model, url):
            return None
        if body.get("prompt_cache_key"):
            return None
        if headers is not None and any(str(k).lower() == GROK_HEADER for k in headers):
            return None
        if body.get("previous_response_id") or body.get("conversation"):
            # The server holds the history; it routes by the response chain.
            return None
        items = body.get("input")
        if isinstance(items, str):
            items = [{"role": "user", "content": items}]
        rid = routing_id(items, system=body.get("instructions"))
        if rid:
            body["prompt_cache_key"] = rid
        return rid
    except Exception:  # pragma: no cover - a routing hint must never fail a request
        logger.debug("cache routing (responses) skipped", exc_info=True)
        return None
