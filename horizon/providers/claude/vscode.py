"""Persistent configuration for Claude Code's user-level settings.

Two surfaces write the same ``~/.claude/settings.json`` env block through the
forwarder: the VS Code extension (``vscode-claude``) and the Claude Code the
ContextShrink desktop app connects (``claude-code``: the terminal and the
Desktop Code tab). They share one state file, so they cannot fight over it:

* ``owners`` lists the connectors that hold the shared route. A connector
  leaves on disconnect; the env block is restored only when the last one goes.
* ``managed`` holds the values Horizon wrote. A user edit to any of them makes
  both connectors refuse to overwrite it.
* ``claude_code`` records what the Claude Code connector added beyond env (the
  status line, the plugin marketplace) so disconnect removes only those.

A state file written before ``owners`` existed belongs to ``vscode-claude``.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import click

from horizon import fsutil
from horizon.providers.claude.runtime import resolve_1m_model
from horizon.proxy.project_context import with_project_prefix

_BASE_URL_KEY = "ANTHROPIC_BASE_URL"
_TOOL_SEARCH_KEY = "ENABLE_TOOL_SEARCH"
_MANAGED_KEYS = (_BASE_URL_KEY, _TOOL_SEARCH_KEY)
_STATE_FILENAME = ".horizon-vscode-claude.json"
_STATE_VERSION = 1

VSCODE_CLAUDE = "vscode-claude"
CLAUDE_CODE = "claude-code"
_OWNERS_KEY = "owners"
_CLAUDE_CODE_KEY = "claude_code"
_LOOPBACK = "127.0.0.1"
#: The user-level tool-search value both connectors share. VS Code's webview
#: cannot render deferred tool-search blocks (anthropics/claude-code#2028), and
#: the settings file is read by the VS Code extension too, so this stays
#: ``false``. The Desktop Code tab gets ``true`` from its launcher environment.
TOOL_SEARCH_USER_VALUE = "false"
MARKETPLACE_NAME = "contextshrink"
PLUGIN_KEY = "contextshrink@contextshrink"


def claude_user_settings_path(
    environ: Mapping[str, str] | None = None, *, platform: str | None = None
) -> Path:
    """Return the Claude Code user settings path, respecting CLAUDE_CONFIG_DIR."""
    env = environ if environ is not None else os.environ
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir).expanduser() / "settings.json"
    current_platform = platform or sys.platform
    home_var = "USERPROFILE" if current_platform == "win32" else "HOME"
    home = Path(env.get(home_var) or Path.home())
    return home / ".claude" / "settings.json"


def vscode_claude_proxy_url(port: int, project: str | None = None) -> str:
    """Return the project-scoped Anthropic endpoint for Claude Code in VS Code."""
    return str(with_project_prefix(f"http://127.0.0.1:{port}", project))


def claude_code_base_url(port: int, keepalive_id: str) -> str:
    """The Claude Code connector's endpoint: the forwarder's keep-alive route."""
    return f"http://{_LOOPBACK}:{port}/k/{keepalive_id}"


def new_keepalive_id() -> str:
    return secrets.token_hex(8)


def route_port(url: str | None) -> int | None:
    """The forwarder port a Horizon route points at, or ``None`` for any other URL."""
    match = re.fullmatch(rf"http://{re.escape(_LOOPBACK)}:(\d+)(?:/k/[^/]+)?", (url or "").strip())
    return int(match.group(1)) if match else None


def _state_path(settings_path: Path) -> Path:
    return settings_path.with_name(_STATE_FILENAME)


def _read_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        raw = fsutil.read_text(path)
    except OSError as exc:
        raise click.ClickException(f"Could not read {label} {path}: {exc}") from exc
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.ClickException(
            f"{label.capitalize()} {path} is not valid JSON ({exc}); refusing to overwrite it."
        ) from exc
    if not isinstance(payload, dict):
        raise click.ClickException(
            f"{label.capitalize()} {path} must contain a JSON object; refusing to overwrite it."
        )
    return payload


def _read_settings(path: Path) -> dict[str, Any]:
    return _read_object(path, label="Claude settings") if path.exists() else {}


def _env_map(payload: dict[str, Any], path: Path) -> dict[str, Any]:
    env = payload.get("env")
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise click.ClickException(
            f"Claude settings {path} has a non-object 'env' value; refusing to overwrite it."
        )
    return dict(env)


def _model_value(payload: dict[str, Any], path: Path) -> str | None:
    if "model" not in payload:
        return None
    model = payload["model"]
    if not isinstance(model, str):
        raise click.ClickException(
            f"Claude settings {path} has a non-string top-level 'model' value; "
            "refusing to overwrite it."
        )
    return model


def resolve_vscode_claude_model(path: Path) -> str:
    """Resolve the 1M model from settings without writing settings or state."""
    payload = _read_settings(path)
    return resolve_1m_model(_model_value(payload, path))


def resolve_vscode_claude_model_for_instructions(path: Path) -> str:
    """Resolve a model for manual setup instructions, tolerating bad settings."""
    try:
        return resolve_vscode_claude_model(path)
    except click.ClickException:
        return resolve_1m_model(None)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fsutil.write_text(path, json.dumps(payload, indent=2) + "\n")


def _validate_previous(previous: dict[str, Any], state_path: Path) -> None:
    for key in _MANAGED_KEYS:
        saved = previous.get(key)
        if (
            not isinstance(saved, dict)
            or not isinstance(saved.get("present"), bool)
            or "value" not in saved
        ):
            raise click.ClickException(
                f"Horizon state {state_path} is incomplete; refusing to edit."
            )


def _validate_model_record(state: dict[str, Any], state_path: Path) -> dict[str, Any] | None:
    if "model" not in state:
        return None
    model_state = state["model"]
    if not isinstance(model_state, dict):
        raise click.ClickException(
            f"Horizon state {state_path} has an incomplete model record; refusing to edit."
        )
    previous = model_state.get("previous")
    managed = model_state.get("managed")
    if (
        not isinstance(previous, dict)
        or not isinstance(previous.get("present"), bool)
        or "value" not in previous
        or not isinstance(managed, str)
        or (not previous["present"] and previous.get("value") is not None)
        or (previous["present"] and not isinstance(previous.get("value"), str))
    ):
        raise click.ClickException(
            f"Horizon state {state_path} has an incomplete model record; refusing to edit."
        )
    return model_state


def _validate_state(state: dict[str, Any], state_path: Path) -> dict[str, Any] | None:
    if state.get("version") != _STATE_VERSION or not isinstance(state.get("previous"), dict):
        raise click.ClickException(
            f"Horizon state {state_path} is unsupported or incomplete; refusing to edit."
        )
    previous = state["previous"]
    _validate_previous(previous, state_path)
    managed = state.get("managed")
    if not isinstance(managed, dict):
        raise click.ClickException(f"Horizon state {state_path} has no managed values.")
    if any(key not in managed or not isinstance(managed[key], str) for key in _MANAGED_KEYS):
        raise click.ClickException(f"Horizon state {state_path} is incomplete; refusing to edit.")
    return _validate_model_record(state, state_path)


def _owners_of(state: dict[str, Any]) -> list[str]:
    """Connectors holding the route. A state without the field is ``vscode-claude``'s."""
    owners = state.get(_OWNERS_KEY)
    if isinstance(owners, list) and all(isinstance(o, str) for o in owners):
        return list(owners)
    return [VSCODE_CLAUDE]


def _model_is_managed(payload: dict[str, Any], managed: str) -> bool:
    return "model" in payload and payload["model"] == managed


def _restore_model(payload: dict[str, Any], model_state: dict[str, Any]) -> None:
    previous = model_state["previous"]
    if previous["present"]:
        payload["model"] = previous["value"]
    else:
        payload.pop("model", None)


def _check_managed(
    payload: dict[str, Any], env: dict[str, Any], state: dict[str, Any], path: Path
) -> None:
    """Refuse to overwrite a value the user changed since Horizon wrote it."""
    managed = state["managed"]
    model_state = _validate_model_record(state, _state_path(path))
    if any(env.get(key) != managed[key] for key in _MANAGED_KEYS):
        raise click.ClickException(
            "Claude settings changed one of Horizon's managed values; refusing to overwrite "
            f"the user's change. Resolve the conflict in {path}, then retry."
        )
    if model_state is not None and not _model_is_managed(payload, model_state["managed"]):
        raise click.ClickException(
            "Claude settings changed Horizon's managed model; refusing to overwrite the user's "
            f"change. Resolve the conflict in {path}, then retry."
        )


def configure_vscode_claude_settings(
    path: Path, proxy_url: str, *, context_1m: bool = False
) -> str:
    """Route Claude Code's VS Code process through Horizon, reversibly."""
    payload = _read_settings(path)
    env = _env_map(payload, path)
    state_path = _state_path(path)
    # Claude Code's VS Code webview cannot render the server_tool_use /
    # tool_search_tool_result blocks emitted by deferred tool search (#2028).
    # Keep it disabled for this surface; the standalone CLI retains its own
    # configurable/default-on policy.
    managed = {_BASE_URL_KEY: proxy_url, _TOOL_SEARCH_KEY: "false"}

    if state_path.exists():
        state = _read_object(state_path, label="Horizon state")
        model_state = _validate_state(state, state_path)
        old_managed = state["managed"]
        if any(env.get(key) != old_managed[key] for key in _MANAGED_KEYS):
            raise click.ClickException(
                "Claude settings changed one of Horizon's managed values. Run "
                "`horizon unwrap vscode-claude` or resolve the conflict before retrying."
            )
        if model_state is not None and not _model_is_managed(payload, model_state["managed"]):
            raise click.ClickException(
                "Claude settings changed Horizon's managed model. Run "
                "`horizon unwrap vscode-claude` or resolve the conflict before retrying."
            )
        owners = _owners_of(state)
        if CLAUDE_CODE in owners:
            # The Claude Code connector holds the shared route. Join it rather
            # than rewrite the base URL out from under it.
            if route_port(old_managed[_BASE_URL_KEY]) != route_port(proxy_url):
                raise click.ClickException(
                    "Claude Code is connected through the ContextShrink desktop app on another "
                    "port. Disconnect it first."
                )
            if VSCODE_CLAUDE not in owners:
                state[_OWNERS_KEY] = owners + [VSCODE_CLAUDE]
                _write_json(state_path, state)
            return "joined"
        action = "updated"
    else:
        model_state = None
        state = {
            "version": _STATE_VERSION,
            "settings_existed": path.exists(),
            "previous": {
                key: {"present": key in env, "value": env.get(key)} for key in _MANAGED_KEYS
            },
        }
        action = "added"

    if context_1m:
        current_model = _model_value(payload, path)
        if model_state is None:
            resolved_model = resolve_1m_model(current_model)
            state["model"] = {
                "previous": {
                    "present": "model" in payload,
                    "value": current_model,
                },
                "managed": resolved_model,
            }
        else:
            previous_model = model_state["previous"]
            prior_value = previous_model["value"] if previous_model["present"] else None
            resolved_model = resolve_1m_model(prior_value)
            model_state["managed"] = resolved_model
            state["model"] = model_state
        payload["model"] = resolved_model
    elif model_state is not None:
        _restore_model(payload, model_state)
        state.pop("model", None)

    env.update(managed)
    payload["env"] = env
    state["managed"] = managed
    state[_OWNERS_KEY] = [VSCODE_CLAUDE]
    _write_json(path, payload)
    _write_json(state_path, state)
    return action


def _restore_all(
    path: Path,
    state: dict[str, Any],
    state_path: Path,
    model_state: dict[str, Any] | None,
    payload: dict[str, Any],
) -> None:
    """Put back every value Horizon saved, then drop the state file."""
    previous = state["previous"]
    env = _env_map(payload, path)
    _check_managed(payload, env, state, path)

    for key in _MANAGED_KEYS:
        saved = previous.get(key)
        if saved["present"]:
            env[key] = saved.get("value")
        else:
            env.pop(key, None)
    if env:
        payload["env"] = env
    else:
        payload.pop("env", None)

    if model_state is not None:
        _restore_model(payload, model_state)

    if payload or state.get("settings_existed"):
        _write_json(path, payload)
    elif path.exists():
        path.unlink()
    state_path.unlink()


def remove_vscode_claude_settings(path: Path) -> bool:
    """Restore the values saved before Horizon configured the VS Code extension.

    When the Claude Code connector still holds the route, only this connector's
    claim is dropped and the env block stays.
    """
    state_path = _state_path(path)
    if not state_path.exists():
        return False
    state = _read_object(state_path, label="Horizon state")
    model_state = _validate_state(state, state_path)
    owners = _owners_of(state)
    if VSCODE_CLAUDE not in owners:
        return False
    others = [owner for owner in owners if owner != VSCODE_CLAUDE]
    if others:
        state[_OWNERS_KEY] = others
        _write_json(state_path, state)
        return True
    payload = _read_settings(path)
    _restore_all(path, state, state_path, model_state, payload)
    return True


# --------------------------------------------------------------------------- Claude Code


def status_exe_default() -> Path:
    """Where the desktop app installs the status line helper."""
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "ContextShrink" / "bin" / "contextshrink-status.exe"


def plugin_dir_default() -> Path:
    """The local marketplace directory (holds ``.claude-plugin/marketplace.json``)."""
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "ContextShrink" / "claude-code" / "plugin-marketplace"


def _quoted_command(exe: Path) -> str:
    """The status line command: the absolute path, quoted so spaces survive the shell."""
    return f'"{exe}"'


def _claude_code_record(state: dict[str, Any]) -> dict[str, Any]:
    record = state.get(_CLAUDE_CODE_KEY)
    return dict(record) if isinstance(record, dict) else {}


def _install_status_line(payload: dict[str, Any], command: str, record: dict[str, Any]) -> str:
    """Install our statusLine only when the user has none. Returns what happened."""
    ours = {"type": "command", "command": command}
    owned = isinstance(record.get("status_line"), dict) and record["status_line"].get("owned")
    if "statusLine" in payload:
        if owned and payload["statusLine"] == ours:
            return "already"
        record["status_line"] = {"owned": False}
        return "kept-user"
    payload["statusLine"] = ours
    record["status_line"] = {"owned": True, "command": command}
    return "installed"


def _install_plugin(payload: dict[str, Any], plugin_dir: str, record: dict[str, Any]) -> str:
    """Add the local marketplace and enable the plugin, only where absent.

    Returns ``installed``, ``already``, or ``kept-user`` when the user already
    has a different marketplace under the same name.
    """
    ours = {"source": {"source": "directory", "path": plugin_dir}}
    previous = record.get("plugin") if isinstance(record.get("plugin"), dict) else {}
    marketplaces = payload.get("extraKnownMarketplaces")
    marketplaces = dict(marketplaces) if isinstance(marketplaces, dict) else {}
    enabled = payload.get("enabledPlugins")
    enabled = dict(enabled) if isinstance(enabled, dict) else {}

    existing = marketplaces.get(MARKETPLACE_NAME)
    if existing is not None and not (previous.get("marketplace_added") and existing == ours):
        record["plugin"] = {
            "marketplace_added": False,
            "plugin_enabled_added": False,
            "plugin_dir": plugin_dir,
        }
        return "kept-user"

    added_marketplace = existing is None
    if added_marketplace:
        marketplaces[MARKETPLACE_NAME] = ours
        payload["extraKnownMarketplaces"] = marketplaces
    added_enabled = PLUGIN_KEY not in enabled
    if added_enabled:
        enabled[PLUGIN_KEY] = True
        payload["enabledPlugins"] = enabled

    record["plugin"] = {
        "marketplace_added": bool(added_marketplace or previous.get("marketplace_added")),
        "plugin_enabled_added": bool(added_enabled or previous.get("plugin_enabled_added")),
        "plugin_dir": plugin_dir,
    }
    return "installed" if (added_marketplace or added_enabled) else "already"


def _restore_extras(payload: dict[str, Any], record: dict[str, Any]) -> None:
    """Remove what the Claude Code connector added, and only while it is unchanged."""
    status = record.get("status_line")
    if isinstance(status, dict) and status.get("owned"):
        if payload.get("statusLine") == {"type": "command", "command": status.get("command")}:
            payload.pop("statusLine", None)
    plugin = record.get("plugin")
    if isinstance(plugin, dict):
        if plugin.get("marketplace_added"):
            marketplaces = payload.get("extraKnownMarketplaces")
            if isinstance(marketplaces, dict):
                ours = {"source": {"source": "directory", "path": plugin.get("plugin_dir")}}
                if marketplaces.get(MARKETPLACE_NAME) == ours:
                    marketplaces.pop(MARKETPLACE_NAME, None)
                if not marketplaces:
                    payload.pop("extraKnownMarketplaces", None)
        if plugin.get("plugin_enabled_added"):
            enabled = payload.get("enabledPlugins")
            if isinstance(enabled, dict):
                if enabled.get(PLUGIN_KEY) is True:
                    enabled.pop(PLUGIN_KEY, None)
                if not enabled:
                    payload.pop("enabledPlugins", None)


def connect_claude_code_settings(
    path: Path,
    *,
    port: int,
    status_exe: Path | None = None,
    plugin_dir: Path | None = None,
) -> dict[str, str]:
    """Route the terminal and Desktop Code tab's Claude Code through the forwarder.

    Returns what happened per target: ``terminal`` (the env route), ``status_line``
    and ``plugin``. The status line and the plugin are installed only where the
    user has none, and only when their file exists (a dangling setting is worse
    than none); each is recorded so disconnect removes only what Horizon added.
    """
    payload = _read_settings(path)
    env = _env_map(payload, path)
    state_path = _state_path(path)

    if state_path.exists():
        state = _read_object(state_path, label="Horizon state")
        _validate_state(state, state_path)
        _check_managed(payload, env, state, path)
        managed = state["managed"]
        if route_port(managed[_BASE_URL_KEY]) != port:
            raise click.ClickException(
                f"Claude Code is already routed to {managed[_BASE_URL_KEY]} by Horizon. "
                "Disconnect that first."
            )
        owners = _owners_of(state)
        record = _claude_code_record(state)
        if CLAUDE_CODE in owners:
            terminal = "already"
        else:
            # VS Code holds the route; this connector joins it.
            terminal = "joined"
            owners = owners + [CLAUDE_CODE]
        keepalive_id = str(record.get("keepalive_id") or new_keepalive_id())
        base_url = managed[_BASE_URL_KEY]
        if not re.search(r"/k/[^/]+$", base_url):
            base_url = claude_code_base_url(port, keepalive_id)
            managed[_BASE_URL_KEY] = base_url
    else:
        keepalive_id = new_keepalive_id()
        record = {}
        base_url = claude_code_base_url(port, keepalive_id)
        state = {
            "version": _STATE_VERSION,
            "settings_existed": path.exists(),
            "previous": {
                key: {"present": key in env, "value": env.get(key)} for key in _MANAGED_KEYS
            },
            "managed": {_BASE_URL_KEY: base_url, _TOOL_SEARCH_KEY: TOOL_SEARCH_USER_VALUE},
        }
        owners = [CLAUDE_CODE]
        terminal = "connected"

    env[_BASE_URL_KEY] = base_url
    env[_TOOL_SEARCH_KEY] = state["managed"][_TOOL_SEARCH_KEY]
    record["keepalive_id"] = keepalive_id
    if status_exe is not None and status_exe.is_file():
        status_line = _install_status_line(payload, _quoted_command(status_exe), record)
    else:
        status_line = "skipped-missing"
    if plugin_dir is not None and (plugin_dir / ".claude-plugin" / "marketplace.json").is_file():
        plugin = _install_plugin(payload, str(plugin_dir), record)
    else:
        plugin = "skipped-missing"

    payload["env"] = env
    state["managed"][_BASE_URL_KEY] = base_url
    state[_OWNERS_KEY] = owners
    state[_CLAUDE_CODE_KEY] = record
    _write_json(path, payload)
    _write_json(state_path, state)
    return {
        "terminal": terminal,
        "status_line": status_line,
        "plugin": plugin,
        "base_url": base_url,
    }


def claude_code_connection_status(path: Path, port: int) -> str:
    """``connected`` / ``other`` / ``off`` for the Claude Code connector on ``port``."""
    state_path = _state_path(path)
    if not state_path.exists():
        return "off"
    state = _read_object(state_path, label="Horizon state")
    _validate_state(state, state_path)
    if route_port(state["managed"][_BASE_URL_KEY]) != port:
        return "other"
    return "connected" if CLAUDE_CODE in _owners_of(state) else "off"


def vscode_connection_status(path: Path, port: int) -> str:
    """``connected`` / ``other`` / ``off`` for the shared route, as VS Code sees it.

    The route is shared, so VS Code reads ``connected`` whenever the forwarder
    on ``port`` holds it, whichever connector wrote the state.
    """
    state_path = _state_path(path)
    if not state_path.exists():
        return "off"
    state = _read_object(state_path, label="Horizon state")
    _validate_state(state, state_path)
    base = state["managed"][_BASE_URL_KEY]
    if route_port(base) == port:
        return "connected"
    return "other"


def remove_claude_code_settings(path: Path, *, port: int) -> str:
    """Disconnect the Claude Code connector. Returns what happened.

    ``restored`` when this was the last connector and the env block went back to
    what it was. ``kept`` when another connector still holds the route.
    ``not-connected`` and ``left-alone`` mean nothing of this connector's was
    touched.
    """
    state_path = _state_path(path)
    if not state_path.exists():
        return "not-connected"
    state = _read_object(state_path, label="Horizon state")
    model_state = _validate_state(state, state_path)
    if route_port(state["managed"][_BASE_URL_KEY]) != port:
        return "left-alone"
    owners = _owners_of(state)
    if CLAUDE_CODE not in owners:
        return "not-connected"

    payload = _read_settings(path)
    record = _claude_code_record(state)
    others = [owner for owner in owners if owner != CLAUDE_CODE]
    if others:
        env = _env_map(payload, path)
        _check_managed(payload, env, state, path)
        _restore_extras(payload, record)
        state.pop(_CLAUDE_CODE_KEY, None)
        state[_OWNERS_KEY] = others
        _write_json(path, payload)
        _write_json(state_path, state)
        return "kept"

    _restore_extras(payload, record)
    state.pop(_CLAUDE_CODE_KEY, None)
    _restore_all(path, state, state_path, model_state, payload)
    return "restored"
