"""Pure project attribution policy helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from horizon.proxy.savings_tracker import sanitize_project_name

PROJECT_HEADER = "x-horizon-project"
PROJECT_PATH_PREFIX = "/p/"
# Cache keep-alive liveness id for clients that cannot send a header (Codex's
# built-in provider): ``/k/<id>`` after any project prefix, read as the
# ``X-Horizon-Keepalive-Id`` header.
KEEPALIVE_HEADER = "x-horizon-keepalive-id"
KEEPALIVE_PATH_PREFIX = "/k/"
_KEEPALIVE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def classify_project(headers: Mapping[str, Any] | Any) -> str | None:
    """Extract a sanitized project name from request headers, if present."""
    get = getattr(headers, "get", None)
    if get is None:
        return None
    value = get(PROJECT_HEADER) or get("X-Horizon-Project")
    return sanitize_project_name(value)


def split_project_path(path: str) -> tuple[str | None, str]:
    """Split ``/p/<name>/rest`` into ``(name, /rest)``."""
    if not path.startswith(PROJECT_PATH_PREFIX):
        return None, path
    remainder = path[len(PROJECT_PATH_PREFIX) :]
    segment, sep, rest = remainder.partition("/")
    project = sanitize_project_name(unquote(segment)) if segment else None
    if project is None:
        return None, path
    return project, ("/" + rest) if sep else "/"


def split_keepalive_path(path: str) -> tuple[str | None, str]:
    """Split ``/k/<id>/rest`` into ``(id, /rest)``."""
    if not path.startswith(KEEPALIVE_PATH_PREFIX):
        return None, path
    segment, sep, rest = path[len(KEEPALIVE_PATH_PREFIX) :].partition("/")
    if not _KEEPALIVE_ID_RE.fullmatch(segment):
        return None, path
    return segment, ("/" + rest) if sep else "/"


def with_keepalive_prefix(base_url: str, keepalive_id: str | None) -> str:
    """Insert ``/k/<id>`` ahead of the path of a local proxy base URL.

    Apply before :func:`with_project_prefix`, which then goes in front.
    """
    if not keepalive_id or not _KEEPALIVE_ID_RE.fullmatch(keepalive_id):
        return base_url
    parts = urlsplit(base_url)
    prefixed = f"{KEEPALIVE_PATH_PREFIX}{keepalive_id}{parts.path}"
    return urlunsplit(parts._replace(path=prefixed.rstrip("/")))


def with_project_prefix(base_url: str, project: str | None) -> str:
    """Insert ``/p/<name>`` ahead of the path of a local proxy base URL."""
    name = sanitize_project_name(project)
    if name is None:
        return base_url
    parts = urlsplit(base_url)
    prefixed = f"{PROJECT_PATH_PREFIX}{quote(name, safe='')}{parts.path}"
    return urlunsplit(parts._replace(path=prefixed.rstrip("/")))
