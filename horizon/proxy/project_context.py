"""Per-request project attribution for the proxy.

``horizon wrap`` launches agents with an ``X-Horizon-Project`` header
(via ``ANTHROPIC_CUSTOM_HEADERS`` for Claude Code and ``env_http_headers``
for Codex) naming the project directory the agent is working in. The proxy
captures that header once per request — in the HTTP middleware for regular
requests and at the WebSocket accept for Codex responses-WS sessions —
into a :mod:`contextvars` variable, so the outcome funnel can attribute
savings to a project without threading a parameter through every handler.

The value is sanitized (printable characters only, length-capped) before it
is stored; an absent or unusable header simply leaves attribution off for
that request, matching pre-feature behavior.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from contextvars import ContextVar
from typing import Any

from horizon.proxy.project_policy import (
    KEEPALIVE_HEADER,
    PROJECT_HEADER,
    PROJECT_PATH_PREFIX,
    classify_project,
    split_keepalive_path,
    split_project_path,
    with_project_prefix,
)
from horizon.proxy.request_scope import normalize_scope_path
from horizon.proxy.savings_tracker import sanitize_project_name

_current_project: ContextVar[str | None] = ContextVar("horizon_current_project", default=None)


def set_current_project(project: str | None) -> None:
    """Bind the active request's project for downstream outcome recording."""
    _current_project.set(sanitize_project_name(project))


def get_current_project() -> str | None:
    """Project bound to the current request context, or ``None``."""
    return _current_project.get()


def strip_project_path_prefix(scope: MutableMapping[str, Any]) -> str | None:
    """Strip a ``/p/<name>`` prefix from an ASGI scope, returning the name.

    A ``/k/<id>`` keep-alive segment after it is stripped too and becomes the
    ``X-Horizon-Keepalive-Id`` header (unless the client sent one).

    Mutates ``scope["path"]`` (and ``raw_path``) so routing sees the
    canonical path. Must run before anything caches the request URL.
    """
    path = scope.get("path", "")
    project, path = split_project_path(path)
    keepalive_id, path = split_keepalive_path(path)
    if project is not None or keepalive_id is not None:
        normalize_scope_path(scope, path)
    if keepalive_id is not None:
        headers = list(scope.get("headers") or [])
        name = KEEPALIVE_HEADER.encode("latin-1")
        if not any(key.lower() == name for key, _ in headers):
            headers.append((name, keepalive_id.encode("latin-1")))
            scope["headers"] = headers
    return project


__all__ = [
    "PROJECT_HEADER",
    "PROJECT_PATH_PREFIX",
    "classify_project",
    "get_current_project",
    "set_current_project",
    "split_project_path",
    "strip_project_path_prefix",
    "with_project_prefix",
]
