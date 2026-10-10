"""Request-scoped learning namespaces for hosted and local Horizon proxies.

Hosted learning follows the verified account, never a client-selected tenant
header. Local operators may select a namespace over loopback. Other callers
are partitioned by authenticated principal/provider credentials using the
same HMAC policy as the response cache. Unresolved identity is isolated to
one request instead of joining another customer's learning pool.
"""
from __future__ import annotations

import logging
import os
import secrets
from contextvars import ContextVar
from typing import Any, Final

logger = logging.getLogger(__name__)
TENANT_KEY_HEADER_ENV_VAR: Final[str] = "HORIZON_TENANT_KEY_HEADER"
DEFAULT_TENANT_KEY_HEADER: Final[str] = "X-Horizon-Tenant-ID"
GLOBAL_TENANT_KEY: Final[str] = "global"
SOURCE_HEADER: Final[str] = "header"
SOURCE_GLOBAL: Final[str] = "global"
SOURCE_ACCOUNT: Final[str] = "account"
SOURCE_CREDENTIAL: Final[str] = "credential"
_MAX_TENANT_KEY_LEN: Final[int] = 64
_request_tenant_key: ContextVar[str | None] = ContextVar("horizon_request_tenant_key", default=None)


def set_request_tenant_key(tenant_key: str | None) -> None:
    """Bind a resolved namespace to this request and its compression workers."""
    _request_tenant_key.set(tenant_key)


def _verified_account_key() -> str | None:
    # This ContextVar is populated only after the control plane authenticates
    # the API key. It also covers Gemini and WebSocket worker paths.
    from horizon.proxy.account_analytics import account_id

    uid = account_id()
    return "account_" + uid if uid else None


def get_current_tenant_key() -> str:
    """Read the authenticated account first, then the local request binding."""
    return _verified_account_key() or _request_tenant_key.get() or GLOBAL_TENANT_KEY


def resolve_tenant_key(request: Any) -> tuple[str, str]:
    """Choose a namespace without accepting remote client identity assertions."""
    from horizon.proxy.loopback_guard import is_loopback_host
    from horizon.proxy.semantic_cache_key_policy import (
        ANONYMOUS_PARTITION,
        compute_request_cache_partition,
    )

    account_key = _verified_account_key()
    if account_key:
        result = (account_key, SOURCE_ACCOUNT)
    else:
        header_name = os.environ.get(TENANT_KEY_HEADER_ENV_VAR, DEFAULT_TENANT_KEY_HEADER)
        peer = getattr(getattr(request, "client", None), "host", None)
        header_key = _sanitize_tenant_key(_get_header(request, header_name))
        partition = compute_request_cache_partition(request)
        if partition is None:
            # An installed resolver failed. Never degrade to a shared pool.
            result = ("unresolved_" + secrets.token_hex(16), "unresolved")
        elif header_key and is_loopback_host(peer):
            result = ("local_" + header_key, SOURCE_HEADER)
        elif partition != ANONYMOUS_PARTITION:
            result = (partition, SOURCE_CREDENTIAL)
        else:
            result = (GLOBAL_TENANT_KEY, SOURCE_GLOBAL)
    _emit_resolution_log(*result)
    return result


def _get_header(request: Any, name: str) -> str:
    headers = getattr(request, "headers", None)
    if headers is None:
        return ""
    value = headers.get(name)
    if value is None:
        value = next((v for k, v in headers.items() if str(k).lower() == name.lower()), "")
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value or "")


def _sanitize_tenant_key(raw: str) -> str:
    return "".join(ch for ch in raw if ch.isascii() and (ch.isalnum() or ch in "-_"))[:_MAX_TENANT_KEY_LEN]


def _emit_resolution_log(tenant_key: str, source: str) -> None:
    logger.info("tenant_key_resolved", extra={"event": "tenant_key_resolved", "tenant_key": tenant_key, "source": source})


__all__ = [
    "DEFAULT_TENANT_KEY_HEADER", "GLOBAL_TENANT_KEY", "SOURCE_GLOBAL",
    "SOURCE_HEADER", "SOURCE_ACCOUNT", "SOURCE_CREDENTIAL", "TENANT_KEY_HEADER_ENV_VAR",
    "get_current_tenant_key", "resolve_tenant_key", "set_request_tenant_key",
]
