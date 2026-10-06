"""OpenCode config file helpers for wrap and persistent install."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import click

from horizon import fsutil
from horizon.install.paths import opencode_config_path

# Horizon-managed JSON marker comments for idempotent block injection.
_PROVIDER_MARKER_START = "// --- Horizon proxy provider ---"
_PROVIDER_MARKER_END = "// --- end Horizon proxy provider ---"
_MCP_MARKER_START = "// --- Horizon MCP server ---"
_MCP_MARKER_END = "// --- end Horizon MCP server ---"

# Regex to strip horizon blocks (including the marker comments).
_PROVIDER_BLOCK_RE = re.compile(
    re.escape(_PROVIDER_MARKER_START) + r".*?" + re.escape(_PROVIDER_MARKER_END),
    re.DOTALL,
)
_MCP_BLOCK_RE = re.compile(
    re.escape(_MCP_MARKER_START) + r".*?" + re.escape(_MCP_MARKER_END),
    re.DOTALL,
)
HORIZON_OPENCODE_PLUGIN = "horizon-opencode"

# Models exposed by the injected `horizon` provider. This provider uses
# ``@ai-sdk/openai-compatible`` and the proxy's ``/v1/chat/completions`` path,
# which is routed to the configured OpenAI upstream. Do not advertise Claude
# models here: OpenCode would send them through the OpenAI endpoint and report
# an ``invalid_api_key`` error instead of reaching Anthropic. Claude models are
# available through OpenCode's native ``anthropic`` provider, whose base URL is
# also redirected to Horizon by ``build_opencode_config_content``.
#
# OpenCode only resolves ``horizon/<id>`` for ids listed in this map, so an
# empty map means every documented ``horizon/*`` model fails with "Model not
# found".
HORIZON_OPENCODE_MODELS: dict[str, Any] = {
    "gpt-4o": {
        "name": "GPT-4o",
        "limit": {"context": 128000, "output": 16384},
    },
    "gpt-4.1": {
        "name": "GPT-4.1",
        "limit": {"context": 1048576, "output": 32768},
    },
}


def horizon_provider_entry(port: int) -> dict[str, Any]:
    """Return the `horizon` provider block pointed at the local proxy."""
    return {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Horizon Proxy",
        "options": {"baseURL": f"http://127.0.0.1:{port}/v1"},
        "models": HORIZON_OPENCODE_MODELS,
    }


def _opencode_home_dir() -> Path:
    """Return the OpenCode home/config directory."""
    env_path = os.environ.get("OPENCODE_HOME", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    return Path.home() / ".config" / "opencode"


def opencode_config_paths() -> tuple[Path, Path]:
    """Return ``(config_file, backup_file)`` for OpenCode."""
    config_file = opencode_config_path()
    backup_file = config_file.with_name(config_file.name + ".horizon-backup")
    return config_file, backup_file


def opencode_created_marker(config_file: Path) -> Path:
    """Marker left when the wrap created ``config_file`` (there was none to back up)."""
    return config_file.with_name(config_file.name + ".horizon-created")


def is_generated_horizon_provider(entry: Any) -> bool:
    """True for the ``horizon`` provider exactly as :func:`horizon_provider_entry` writes it.

    Lets unwrap remove a block the wrap wrote without markers, including ones
    left by releases that did not record creating the file.
    """
    if not isinstance(entry, dict):
        return False
    base_url = str((entry.get("options") or {}).get("baseURL", ""))
    return (
        entry.get("name") == "Horizon Proxy"
        and entry.get("npm") == "@ai-sdk/openai-compatible"
        and re.fullmatch(r"http://127\.0\.0\.1:\d+/v1", base_url) is not None
    )


def remove_generated_horizon_provider(data: dict[str, Any]) -> bool:
    """Drop a generated ``horizon`` provider (and an emptied ``provider`` map)."""
    providers = data.get("provider")
    if not isinstance(providers, dict) or not is_generated_horizon_provider(providers.get("horizon")):
        return False
    del providers["horizon"]
    if not providers:
        del data["provider"]
    return True


def snapshot_opencode_config_if_unwrapped(config_file: Path, backup_file: Path) -> None:
    """Snapshot ``opencode.json`` to ``backup_file`` before the first injection.

    Guarantees that ``horizon unwrap opencode`` can restore the user's
    original file byte-for-byte.
    """
    if backup_file.exists():
        return
    if not config_file.exists():
        return
    try:
        content = fsutil.read_text(config_file)
    except OSError:
        return
    if _PROVIDER_MARKER_START in content or _MCP_MARKER_START in content:
        return
    backup_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(config_file, backup_file)


def strip_opencode_horizon_blocks(content: str, *, remove_mcp: bool = True) -> str:
    """Remove all Horizon-managed blocks from opencode JSON text.

    Preserves user content. Returns the cleaned string.
    """
    content = _PROVIDER_BLOCK_RE.sub("", content)
    if remove_mcp:
        content = _MCP_BLOCK_RE.sub("", content)
    # Collapse multiple blank lines left behind by block removal.
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content.strip()


def _render_provider_block(port: int) -> str:
    """Render a Horizon provider block as a JSON comment-wrapped snippet."""
    provider = {"horizon": horizon_provider_entry(port)}
    lines = [
        _PROVIDER_MARKER_START,
        f'"provider": {json.dumps(provider, indent=2)},',
        _PROVIDER_MARKER_END,
    ]
    return "\n".join(lines)


def _parse_json_loose(text: str) -> dict[str, Any]:
    """Parse JSON text, stripping line comments (// ...) when needed.

    Tries standard JSON first to avoid corrupting URLs that contain ``//``.
    Falls back to stripping ``//`` comments when standard parsing fails.
    Two-pass: (1) remove comment-only lines, (2) strip inline trailing
    comments that follow a comma.
    """
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass
    # Pass 1: remove lines that are ONLY a comment.
    cleaned = re.sub(r"^\s*//[^\n]*\n", "", text, flags=re.MULTILINE)
    # Pass 2: remove inline trailing comments (", // comment").
    cleaned = re.sub(r",\s*//[^\n]*", ",", cleaned)
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _inject_key_into_json(data: dict[str, Any], key: str, value: Any) -> dict[str, Any]:
    """Merge ``value`` into ``data[key]`` idempotently."""
    existing = data.get(key)
    if isinstance(existing, dict) and isinstance(value, dict):
        merged = {**existing, **value}
        data[key] = merged
    else:
        data[key] = value
    return data


def append_horizon_plugin(config: dict[str, object]) -> bool:
    """Append the optional OpenCode plugin entry if it is not already present."""
    plugin = config.get("plugin")
    if plugin is None:
        config["plugin"] = [HORIZON_OPENCODE_PLUGIN]
        return True

    if not isinstance(plugin, list):
        return False

    for entry in plugin:
        if entry == HORIZON_OPENCODE_PLUGIN:
            return False
        if isinstance(entry, list) and entry and entry[0] == HORIZON_OPENCODE_PLUGIN:
            return False

    plugin.append(HORIZON_OPENCODE_PLUGIN)
    return True


def inject_opencode_provider_config(port: int) -> None:
    """Inject a Horizon model provider into OpenCode's config file.

    Safe to call multiple times — the injected block is fully replaced on
    each call, so re-running with a different ``port`` updates the config.
    Before the first injection, the pre-wrap file is snapshotted to
    ``opencode.json.horizon-backup`` so ``horizon unwrap opencode``
    can restore it byte-for-byte.
    """
    config_file, backup_file = opencode_config_paths()
    config_dir = config_file.parent

    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        snapshot_opencode_config_if_unwrapped(config_file, backup_file)

        if config_file.exists():
            content = fsutil.read_text(config_file)
            data = _parse_json_loose(content)
        else:
            content = ""
            data = {}
            # Nothing to back up: record that this file is ours so unwrap
            # can remove it again.
            opencode_created_marker(config_file).touch()

        # Strip any prior Horizon-managed blocks before re-injecting.
        if _PROVIDER_MARKER_START in content or _MCP_MARKER_START in content:
            content = strip_opencode_horizon_blocks(content)
            data = _parse_json_loose(content)

        # Merge provider into the JSON data structure.
        provider = {"horizon": horizon_provider_entry(port)}
        data = _inject_key_into_json(data, "provider", provider)

        # Write back as formatted JSON (opencode uses standard JSON with comments).
        output = json.dumps(data, indent=2) + "\n"
        config_file.write_text(output, encoding="utf-8")
    except OSError as exc:
        raise click.ClickException(
            f"could not write OpenCode config at {config_file}: {exc}"
        ) from exc
