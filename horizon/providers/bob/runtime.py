"""Route IBM Bob CLI traffic through the Horizon proxy (port of headroom #3801).

Bob resolves its gateway as ``--gateway-url ?? BOB_GATEWAY_URL ?? default``,
so ``BOB_GATEWAY_URL`` reroutes it without touching ``~/.bob/settings``; Bob
keeps its own ``Authorization: apikey ...`` credential. Bob appends full paths
to that bare origin itself, so the proxy needs three seams:

* Bob posts chat to ``/inference/v1/chat/completions``: a real route
  (:data:`CHAT_ROUTE`) so it is compressed, not passed through the catch-all.
* Bob builds its other gateway paths (``/inference/``, ``/admin/``, ``/rag/``,
  ``/metrics-forwarder/``) against the bare origin. Joining them onto the
  ``/inference/v1`` upstream base doubles or misroots them, which IBM's edge
  rejects with 403, so they go to the origin verbatim.
* Bob 2.0.1-2.0.5 rewrites its gateway host from ``region_domain`` in the
  ``/admin/v1/profile`` response while keeping the proxy's port, so every later
  request goes to an unreachable ``api.<region>:<proxy-port>``. The proxy
  removes that key.

Bob bills flat per token (no prompt-cache discount to protect), so its wrap
defaults the proxy to token mode.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from horizon.proxy.project_context import with_project_prefix

GATEWAY_ENV = "BOB_GATEWAY_URL"
# Carries /inference/v1 so the proxy's URL normalization (strips /v1) and the
# chat handler (re-appends /v1/chat/completions) compose the path IBM serves.
DEFAULT_API_URL = "https://api.us-east.bob.ibm.com/inference/v1"
CHAT_ROUTE = "/inference/v1/chat/completions"
DEFAULT_MODE = "token"

_ORIGIN_PASSTHROUGH_PREFIXES = ("/inference/", "/admin/", "/rag/", "/metrics-forwarder/")
# IBM runs one gateway per region (api.<region>.bob.ibm.com); --openai-api-url
# can select any of them.
_ORIGIN_HOST_SUFFIX = ".bob.ibm.com"
# (path, key): removed at every nesting depth (Bob reads region_domain under
# instances[].teams[], not at the top level).
_STRIP_JSON_KEYS = (("/admin/v1/profile", "region_domain"),)


def build_launch_env(
    port: int, environ: Mapping[str, str] | None = None, project: str | None = None
) -> tuple[dict[str, str], list[str]]:
    """Bob's launch environment: the bare proxy origin (Bob adds its own paths)."""
    env = dict(environ if environ is not None else os.environ)
    url = with_project_prefix(f"http://127.0.0.1:{port}", project)
    env[GATEWAY_ENV] = url
    return env, [f"{GATEWAY_ENV}={url}"]


def _origin_key(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    return parts.scheme, "127.0.0.1" if host == "localhost" else host, parts.port


def preflight(env: Mapping[str, str], settings_path: Path | None = None) -> str | None:
    """Refuse to launch Bob when its saved gatewayUrl would bypass the proxy.

    bobshell (verified 2.0.5) re-resolves its gateway at startup as
    ``settings.gatewayUrl ?? policy.GatewayUrl ?? <env/flag>``, so a saved
    gatewayUrl silently overrides BOB_GATEWAY_URL and Bob runs uncompressed
    while the wrap banner claims otherwise. A policy-enforced URL cannot be
    read locally; the message names it so the user knows where else to look.
    """
    path = settings_path or Path.home() / ".bob" / "settings" / "settings.json"
    try:
        saved = json.loads(path.read_text(encoding="utf-8")).get("gatewayUrl")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(saved, str) or not saved.strip():
        return None
    # Same proxy if scheme, host and port agree; a different /p/<project>
    # prefix only changes attribution.
    if _origin_key(saved) == _origin_key(env.get(GATEWAY_ENV, "")):
        return None
    return (
        f"Bob's saved gatewayUrl ({saved.strip()}) overrides {GATEWAY_ENV}, so Bob "
        f"would bypass the Horizon proxy. Remove the gatewayUrl entry from {path} "
        "(or set it to the proxy URL shown by this wrap) and retry. If your "
        "organisation enforces a GatewayUrl policy, Bob cannot be wrapped."
    )


def _bob_origin(base_url: str | None) -> str | None:
    if not base_url:
        return None
    base = urlsplit(base_url)
    host = (base.hostname or "").lower()
    if base.scheme == "https" and host.endswith(_ORIGIN_HOST_SUFFIX):
        return f"{base.scheme}://{base.netloc}"
    return None


def resolve_origin_passthrough_url(base_url: str | None, path: str) -> str | None:
    """Origin-rooted upstream URL for a Bob gateway path, or None to keep base+path."""
    origin = _bob_origin(base_url)
    if origin and path.startswith(_ORIGIN_PASSTHROUGH_PREFIXES):
        return origin + path
    return None


def _strip_json_key(obj: object, key: str) -> bool:
    """Remove ``key`` from every dict in ``obj`` in place; True when removed."""
    removed = False
    if isinstance(obj, dict):
        if key in obj:
            del obj[key]
            removed = True
        for value in obj.values():
            removed = _strip_json_key(value, key) or removed
    elif isinstance(obj, list):
        for value in obj:
            removed = _strip_json_key(value, key) or removed
    return removed


def filters_response(base_url: str | None, path: str) -> bool:
    """True when a reply on ``path`` from ``base_url`` may need keys removed."""
    return _bob_origin(base_url) is not None and any(
        path == declared or path.startswith(declared.rstrip("/") + "/")
        for declared, _ in _STRIP_JSON_KEYS
    )


def strip_origin_passthrough_response_keys(
    base_url: str | None, path: str, body: bytes
) -> bytes | None:
    """Filtered JSON body for a Bob gateway response, or None when nothing changed."""
    if _bob_origin(base_url) is None:
        return None
    keys = [
        key
        for declared, key in _STRIP_JSON_KEYS
        if path == declared or path.startswith(declared.rstrip("/") + "/")
    ]
    if not keys:
        return None
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    changed = False
    for key in keys:
        changed = _strip_json_key(payload, key) or changed
    if not changed:
        return None
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
