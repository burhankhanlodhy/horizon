"""Signed-in CLI use of the hosted ContextShrink proxy (``horizon login``).

After ``horizon login`` the CLI works like the desktop app: a per-device
``hz_...`` key in the OS credential store, and every ``horizon wrap <tool>``
runs through a loopback relay (``horizon forward``) to the hosted proxy
instead of a local proxy, so the account dashboard sees its usage and
savings. ``horizon wrap --local`` (or ``HORIZON_HOSTED=0``) opts out.

The CLI keeps its own credential entries and relay ports, apart from the
desktop app's (``remote-api-key``, 18788-18792), so signing in or out of one
never breaks the other on the same machine. The control-plane API is the one
the desktop app and the web dashboard use.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

API_URL_ENV = "CONTEXTSHRINK_API_URL"
PROXY_URL_ENV = "CONTEXTSHRINK_PROXY_URL"
APP_URL_ENV = "CONTEXTSHRINK_APP_URL"
HOSTED_ENV = "HORIZON_HOSTED"

#: Credential-store entries of the signed-in CLI (the desktop app uses ``remote-api-key``).
KEY_ENTRY = "cli-api-key"
SESSION_ENTRY = "cli-session"

#: The CLI's relay ports, clear of a local proxy (8787) and the desktop app (18788-18792).
RELAY_PORT = 18688
#: Tools on a provider the hosted proxy is not configured for get their own
#: relay pinned to it (as in the desktop app): wrap command -> (port, upstream).
TOOL_RELAYS: dict[str, tuple[int, str]] = {
    "kimi": (18689, "https://api.kimi.com/coding/v1"),
    "vibe": (18690, "https://api.mistral.ai"),
    "grok": (18691, "https://api.x.ai"),
    "bob": (18692, "https://api.us-east.bob.ibm.com"),
}
#: Hosted use leaves local-only extras off, as the desktop app does: the hosted
#: gateway serves no retrieve/MCP routes and code memory needs a local Python.
HOSTED_DEFAULTS = {"no_mcp": True, "code_memory": "none", "no_serena": True}

KEY_SCOPES = ["proxy:messages", "proxy:responses"]


def api_url(path: str = "") -> str:
    base = os.environ.get(API_URL_ENV) or "https://api.contextshrink.com"
    return base.rstrip("/") + path


def proxy_url() -> str:
    return (os.environ.get(PROXY_URL_ENV) or "https://proxy.contextshrink.com").rstrip("/")


def app_url() -> str:
    return (os.environ.get(APP_URL_ENV) or "https://app.contextshrink.com").rstrip("/")


def device_name() -> str:
    return f"CLI: {socket.gethostname() or 'unknown'}"


# -- sign-in state (no secrets; those live in the credential store) ----------


def state_path() -> Path:
    from horizon import paths

    return paths.config_dir() / "cli-login.json"


def load_state() -> dict[str, Any] | None:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("key_id") else None


def save_state(state: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def clear_state() -> None:
    try:
        state_path().unlink()
    except OSError:
        pass


def hosted_active() -> bool:
    """True when wraps should use the hosted proxy: signed in (or a CI key), not opted out."""
    if os.environ.get(HOSTED_ENV, "").strip().lower() in ("0", "false", "no", "off"):
        return False
    from horizon.vault import KEY_ENV

    return load_state() is not None or bool(os.environ.get(KEY_ENV, "").strip())


def wrap_defaults(command: Any) -> dict[str, Any]:
    """Hosted defaults for one ``wrap`` subcommand: its relay port and local extras off."""
    names = {getattr(p, "name", None) for p in getattr(command, "params", [])}
    defaults: dict[str, Any] = {}
    if "port" in names:
        relay = TOOL_RELAYS.get(command.name)
        defaults["port"] = relay[0] if relay else RELAY_PORT
    defaults.update({k: v for k, v in HOSTED_DEFAULTS.items() if k in names})
    return defaults


# -- control-plane API ----------------------------------------------------------


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


#: Tests inject an ``httpx.MockTransport`` here.
_transport: httpx.BaseTransport | None = None


def _request(method: str, path: str, *, token: str | None = None, json_body: Any = None) -> Any:
    from horizon._version import __version__

    headers = {"user-agent": f"ContextShrinkCLI/{__version__}"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=20, transport=_transport) as client:
            resp = client.request(method, api_url(path), headers=headers, json=json_body)
    except httpx.HTTPError as exc:
        raise ApiError("Cannot reach ContextShrink. Check your internet connection.") from exc
    if resp.status_code == 401:
        raise ApiError("Your session has expired. Run: horizon login --force", 401)
    try:
        body = resp.json()
    except ValueError:
        body = None
    if resp.status_code >= 400:
        detail = body.get("detail") if isinstance(body, dict) else None
        raise ApiError(str(detail or f"Request failed ({resp.status_code})"), resp.status_code)
    return body


def login(email: str, password: str) -> tuple[str, dict[str, Any]]:
    try:
        body = _request("POST", "/auth/login", json_body={"email": email, "password": password})
    except ApiError as exc:
        if exc.status == 401:
            raise ApiError("Incorrect email or password.", 401) from exc
        raise
    if not isinstance(body, dict) or not body.get("token"):
        raise ApiError("Unexpected login response")
    return str(body["token"]), dict(body.get("user") or {})


def create_device_key(token: str, name: str) -> dict[str, Any]:
    body = _request("POST", "/keys", token=token, json_body={"name": name, "scopes": KEY_SCOPES})
    if not isinstance(body, dict) or not body.get("key") or not body.get("id"):
        raise ApiError("Unexpected key response")
    return body


def key_is_active(token: str, key_id: str) -> bool:
    keys = _request("GET", "/keys", token=token)
    return isinstance(keys, list) and any(
        k.get("id") == key_id and k.get("revoked_at") is None for k in keys if isinstance(k, dict)
    )


def revoke_key(token: str, key_id: str) -> None:
    _request("DELETE", f"/keys/{key_id}", token=token)


def end_session(token: str) -> None:
    try:
        _request("POST", "/auth/logout", token=token)
    except ApiError:
        pass


def me(token: str) -> dict[str, Any]:
    return dict(_request("GET", "/auth/me", token=token) or {})


def billing_estimate(token: str) -> dict[str, Any]:
    return dict(_request("GET", "/billing/estimate", token=token) or {})


def usage_summary(token: str, days: int = 30) -> dict[str, Any]:
    return dict(_request("GET", f"/usage/summary?days={days}", token=token) or {})


# -- relays -------------------------------------------------------------------------


def relay_upstream(port: int) -> str | None:
    return next((up for p, up in TOOL_RELAYS.values() if p == port), None)


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def relay_command(port: int) -> list[str]:
    """The ``horizon forward start`` command for a hosted relay on ``port``."""
    if getattr(sys, "frozen", False):  # the bundled client is its own executable
        cmd = [sys.executable]
    else:
        cmd = [sys.executable, "-m", "horizon.cli"]
    cmd += ["forward", "start", "--remote", proxy_url(), "--port", str(port)]
    upstream = relay_upstream(port)
    if upstream:
        cmd += ["--upstream", upstream]
    return cmd


def ensure_relay(port: int, *, timeout: float = 15.0) -> subprocess.Popen | None:
    """Start a hosted relay on ``port``; reuse one already listening (returns None).

    The relay reads the CLI's own device key (``HORIZON_VAULT_CREDENTIAL``)
    from the credential store, or ``CONTEXTSHRINK_API_KEY``.
    """
    if _listening(port):
        return None
    from horizon import paths
    from horizon.vault import CREDENTIAL_ENV, VaultError, get_credential

    env = dict(os.environ)
    env[CREDENTIAL_ENV] = KEY_ENTRY
    previous = os.environ.get(CREDENTIAL_ENV)
    os.environ[CREDENTIAL_ENV] = KEY_ENTRY
    try:
        get_credential()  # fail before spawning, with the actionable message
    except VaultError as exc:
        raise RuntimeError(f"{exc}. Run: horizon login") from exc
    finally:
        if previous is None:
            os.environ.pop(CREDENTIAL_ENV, None)
        else:
            os.environ[CREDENTIAL_ENV] = previous
    log_dir = paths.log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"relay-{port}.log"
    log = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 - the child owns it
    proc = subprocess.Popen(
        relay_command(port),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        if _listening(port):
            return proc
        time.sleep(0.2)
    if proc.poll() is None:
        proc.terminate()
    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-600:]
    except OSError:
        tail = ""
    raise RuntimeError(f"the ContextShrink relay did not start on port {port}. {tail}".strip())
