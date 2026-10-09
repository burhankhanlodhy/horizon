"""Complete-JSON Claude Code and Codex compact/native tool adapters.

No streaming deltas, WebSocket events, built-in apply_patch_call operations or
shell/JavaScript wrappers are accepted here. They have different contracts.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .compiler import CompactEditError, CompiledEdit

VIRTUAL_TOOL = "horizon_compact_edit_v1"
KINDS = ("claude_edit", "codex_custom_patch", "codex_function_patch")


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def fingerprint(definition: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(definition).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NativeContract:
    kind: str
    definition_sha256: str
    # Custom patch uses a raw string; function patch uses this exact parameter.
    patch_parameter: str = "patch"
    emit_replace_all: bool = True

    def validate(self, tools: Any) -> None:
        if (
            self.kind not in KINDS
            or not isinstance(tools, list)
            or any(not isinstance(tool, dict) for tool in tools)
        ):
            raise CompactEditError("unsupported native tool contract")
        name = "Edit" if self.kind == "claude_edit" else "apply_patch"
        matches = [tool for tool in tools if isinstance(tool, dict) and tool.get("name") == name]
        if len(matches) != 1 or fingerprint(matches[0]) != self.definition_sha256:
            raise CompactEditError("native tool definition does not match certification")
        tool = matches[0]
        if self.kind == "claude_edit":
            schema = tool.get("input_schema", {})
            expected = {"file_path", "old_string", "new_string"}
        elif self.kind == "codex_function_patch":
            if tool.get("type") != "function":
                raise CompactEditError("expected Responses function tool")
            schema = tool.get("parameters", {})
            expected = {self.patch_parameter}
        else:
            if (
                tool.get("type") != "custom"
                or not isinstance(tool.get("format"), dict)
                or tool["format"].get("type") != "grammar"
            ):
                raise CompactEditError("expected certified Responses custom grammar tool")
            return
        if not isinstance(schema, dict):
            raise CompactEditError("unsupported native schema")
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if (
            not isinstance(properties, dict)
            or not isinstance(required, list)
            or any(not isinstance(key, str) for key in required)
        ):
            raise CompactEditError("unsupported native schema")
        if schema.get("type") != "object" or not expected.issubset(required):
            raise CompactEditError("native argument requirements are unsupported")
        if set(required) - (expected | ({"replace_all"} if self.kind == "claude_edit" else set())):
            raise CompactEditError("native tool has additional required arguments")
        if any(
            not isinstance(properties.get(key), dict) or properties[key].get("type") != "string"
            for key in expected
        ):
            raise CompactEditError("native tool arguments must be strings")
        if self.kind == "claude_edit":
            if self.emit_replace_all and (
                not isinstance(properties.get("replace_all"), dict)
                or properties["replace_all"].get("type") != "boolean"
            ):
                raise CompactEditError("unsupported Edit replace_all schema")
            if not self.emit_replace_all and "replace_all" in required:
                raise CompactEditError("required replace_all must be emitted")


def virtual_definition(
    kind: str,
    receipt: str,
    table: str,
    template: str,
    keys: tuple[str, ...],
    *,
    path: str,
    source_sha256: str,
) -> dict[str, Any]:
    schema = {
        "type": "object",
        "properties": {
            "receipt": {"type": "string", "enum": [receipt]},
            "table": {"type": "string", "enum": [table]},
            "template": {"type": "string", "enum": [template]},
            "keys": {
                "type": "array",
                "items": {"type": "string", "enum": list(keys)},
                "minItems": len(keys),
                "maxItems": len(keys),
            },
        },
        "required": ["receipt", "table", "template", "keys"],
        "additionalProperties": False,
    }
    description = (
        "Clone the supplied literal ModelPricing catalog template into exactly the requested keys, "
        "changing only each dictionary key and model field. This expands to the native edit tool; "
        "normal permissions apply. Use native tools for every other change."
    )
    description += " Bound source: " + canonical({"path": path, "sha256": source_sha256})
    if kind == "claude_edit":
        return {"name": VIRTUAL_TOOL, "description": description, "input_schema": schema}
    return {
        "type": "function",
        "name": VIRTUAL_TOOL,
        "description": description,
        "parameters": schema,
        "strict": True,
    }


def response_items(body: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    field = "content" if kind == "claude_edit" else "output"
    items = body.get(field)
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise CompactEditError("expected a complete provider response")
    if kind == "claude_edit":
        if (
            body.get("type") != "message"
            or body.get("role") != "assistant"
            or body.get("stop_reason") is None
        ):
            raise CompactEditError("Claude response is not complete")
    elif body.get("object") != "response" or body.get("status") != "completed":
        raise CompactEditError("Responses response is not complete")
    return items


def is_virtual(item: dict[str, Any], kind: str) -> bool:
    call_type = "tool_use" if kind == "claude_edit" else "function_call"
    return item.get("type") == call_type and item.get("name") == VIRTUAL_TOOL


def call_id(item: dict[str, Any], kind: str) -> str:
    value = item.get("id" if kind == "claude_edit" else "call_id")
    if not isinstance(value, str) or not value or len(value) > 256:
        raise CompactEditError("missing or invalid call ID")
    return value


def virtual_arguments(item: dict[str, Any], kind: str) -> dict[str, Any]:
    value = item.get("input") if kind == "claude_edit" else item.get("arguments")
    if kind != "claude_edit":
        if not isinstance(value, str) or len(value) > 16_384:
            raise CompactEditError("invalid compact arguments")
        try:

            def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                result = {}
                for key, entry in pairs:
                    if key in result:
                        raise CompactEditError("duplicate argument key")
                    result[key] = entry
                return result

            value = json.loads(value, object_pairs_hook=unique_pairs)
        except (ValueError, RecursionError) as exc:
            raise CompactEditError("invalid compact JSON") from exc
    if not isinstance(value, dict) or set(value) != {"receipt", "table", "template", "keys"}:
        raise CompactEditError("unexpected compact arguments")
    return value


def expand_call(
    item: dict[str, Any], contract: NativeContract, edit: CompiledEdit
) -> dict[str, Any]:
    native = copy.deepcopy(item)
    if contract.kind == "claude_edit":
        native["name"] = "Edit"
        native["input"] = {
            "file_path": edit.snapshot.native_path,
            "old_string": edit.snapshot.source,
            "new_string": edit.after,
        }
        if contract.emit_replace_all:
            native["input"]["replace_all"] = False
    elif contract.kind == "codex_custom_patch":
        native.pop("arguments", None)
        native.update(type="custom_tool_call", name="apply_patch", input=edit.patch)
    else:
        native["name"] = "apply_patch"
        native["arguments"] = canonical({contract.patch_parameter: edit.patch})
    return native


def signature(item: dict[str, Any], kind: str) -> str:
    """Bind replay to the exact native operation, ignoring envelope-only fields."""
    if kind == "claude_edit":
        value = {key: item.get(key) for key in ("type", "id", "name", "input")}
    elif item.get("type") == "custom_tool_call":
        value = {key: item.get(key) for key in ("type", "call_id", "name", "input")}
    else:
        value = {key: item.get(key) for key in ("type", "call_id", "name")}
        try:
            value["arguments"] = json.loads(item["arguments"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CompactEditError("invalid replayed function arguments") from exc
    return fingerprint(value)


def restore_call(current: dict[str, Any], provider: dict[str, Any], kind: str) -> dict[str, Any]:
    restored = copy.deepcopy(current)
    for key in ("type", "name", "input", "arguments"):
        restored.pop(key, None)
    for key in ("type", "name", "input", "arguments"):
        if key in provider:
            restored[key] = copy.deepcopy(provider[key])
    if kind != "claude_edit" and "id" in provider:
        restored["id"] = provider["id"]
    return restored
