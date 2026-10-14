"""Stable and bounded tool contracts exposed by the desktop MCP server."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class ToolInputError(Exception):
    """Sanitized invalid-tool-input error; input values are never echoed."""

    def __init__(self, code: str, fields: tuple[str, ...] = ()) -> None:
        self.code = code
        self.fields = fields
        self.message = f"Invalid tool input ({code})"
        super().__init__(self.message)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _bounded_text(value: str) -> str:
    if len(value) > 4096:
        raise ValueError("text too long")
    return value


class SearchArguments(_StrictModel):
    query: str = ""
    limit: int = Field(default=5, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def query_length(cls, value: str) -> str:
        return _bounded_text(value)


class OpenArguments(_StrictModel):
    source: str = Field(min_length=1, max_length=4096)
    mode: Literal["exact", "compact", "focused", "overview"] = "exact"
    result_token_budget: int | None = Field(default=None, ge=128, le=8000)
    query: str | None = None
    cursor: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("query")
    @classmethod
    def query_length(cls, value: str | None) -> str | None:
        return _bounded_text(value) if value is not None else None


class LinesSelector(_StrictModel):
    type: Literal["lines"]
    start: int = Field(ge=1)
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def end_not_before_start(self) -> LinesSelector:
        if self.end < self.start or self.end - self.start > 2000:
            raise ValueError("line range must be ordered and bounded")
        return self


class MarkdownSectionSelector(_StrictModel):
    type: Literal["markdown_section"]
    heading: str = Field(min_length=1, max_length=256)
    occurrence: int = Field(default=1, ge=1, le=100)

    @field_validator("heading")
    @classmethod
    def heading_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("heading cannot be blank")
        return value


class JsonPointerSelector(_StrictModel):
    type: Literal["json_pointer"]
    pointer: str = Field(max_length=1024)

    @field_validator("pointer")
    @classmethod
    def pointer_syntax(cls, value: str) -> str:
        if value and (
            not value.startswith("/") or "~" in value.replace("~0", "").replace("~1", "")
        ):
            raise ValueError("pointer must be an RFC 6901 path")
        return value


class RowsSelector(_StrictModel):
    type: Literal["rows"]
    start: int = Field(ge=1)
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def ordered_bounded_rows(self) -> RowsSelector:
        if self.end < self.start or self.end - self.start > 2000:
            raise ValueError("row range must be ordered and bounded")
        return self


Selector = Annotated[
    LinesSelector | MarkdownSectionSelector | JsonPointerSelector | RowsSelector,
    Field(discriminator="type"),
]


class RetrieveArguments(_StrictModel):
    source_id: str = Field(min_length=1, max_length=4096)
    selector: Selector
    result_token_budget: int | None = Field(default=None, ge=128, le=8000)
    cursor: str | None = Field(default=None, min_length=1, max_length=256)


class StatsArguments(_StrictModel):
    """No-argument tool input; extra keys are rejected."""


_TOOL_SPECS: tuple[dict[str, Any], ...] = (
    {
        "name": "contextshrink_search",
        "description": "Search available ContextShrink sources.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "default": "", "maxLength": 4096},
                "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
            },
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "contextshrink_open",
        "description": "Open a source using a bounded presentation mode.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "minLength": 1, "maxLength": 4096},
                "mode": {
                    "type": "string",
                    "enum": ["exact", "compact", "focused", "overview"],
                    "default": "exact",
                },
                "result_token_budget": {"type": "integer", "minimum": 128, "maximum": 8000},
                "query": {"type": "string", "maxLength": 4096},
                "cursor": {"type": "string", "minLength": 1, "maxLength": 256},
            },
            "required": ["source"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "contextshrink_retrieve",
        "description": "Retrieve an exact bounded selector from an available source version.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_id": {"type": "string", "minLength": 1, "maxLength": 4096},
                "selector": {
                    "type": "object",
                    "description": (
                        "Use logical CSV/TSV record numbers for rows; record 1 is the first "
                        "record, including a header when present."
                    ),
                    "oneOf": [
                        {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "const": "lines"},
                                "start": {"type": "integer", "minimum": 1},
                                "end": {"type": "integer", "minimum": 1},
                            },
                            "required": ["type", "start", "end"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "const": "markdown_section"},
                                "heading": {"type": "string", "minLength": 1, "maxLength": 256},
                                "occurrence": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 100,
                                    "default": 1,
                                },
                            },
                            "required": ["type", "heading"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "const": "json_pointer"},
                                "pointer": {"type": "string", "maxLength": 1024},
                            },
                            "required": ["type", "pointer"],
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "const": "rows"},
                                "start": {"type": "integer", "minimum": 1},
                                "end": {"type": "integer", "minimum": 1},
                            },
                            "required": ["type", "start", "end"],
                            "additionalProperties": False,
                        },
                    ],
                    "discriminator": {"propertyName": "type"},
                },
                "result_token_budget": {"type": "integer", "minimum": 128, "maximum": 8000},
                "cursor": {"type": "string", "minLength": 1, "maxLength": 256},
            },
            "required": ["source_id", "selector"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "contextshrink_stats",
        "description": "Return local ContextShrink diagnostic statistics.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
)


def tool_specs() -> list[dict[str, Any]]:
    """Return fresh MCP Tool constructor dictionaries in stable order."""
    # Round-trip to avoid callers mutating our module-level schema templates.
    return json.loads(json.dumps(_TOOL_SPECS))


_ARGUMENT_MODELS: dict[str, type[BaseModel]] = {
    "contextshrink_search": SearchArguments,
    "contextshrink_open": OpenArguments,
    "contextshrink_retrieve": RetrieveArguments,
    "contextshrink_stats": StatsArguments,
}
_TOOL_FIELDS = {
    "query",
    "limit",
    "source",
    "mode",
    "result_token_budget",
    "source_id",
    "selector",
    "type",
    "start",
    "end",
    "cursor",
}


def _safe_validation_fields(error: ValidationError) -> tuple[str, ...]:
    fields = {
        str(item["loc"][0]) if str(item["loc"][0]) in _TOOL_FIELDS else "unknown_field"
        for item in error.errors()
        if item.get("loc")
    }
    return tuple(sorted(fields))


def validate_tool_arguments(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate tool arguments strictly and return a plain JSON-compatible dict."""
    model = _ARGUMENT_MODELS.get(name)
    if model is None:
        raise ToolInputError("unknown_tool")
    if not isinstance(args, dict):
        raise ToolInputError("invalid_arguments")
    try:
        validated = model.model_validate(args)
    except ValidationError as error:
        raise ToolInputError("invalid_arguments", _safe_validation_fields(error)) from None
    return validated.model_dump(mode="json", exclude_unset=False)


def result_error(code: str, message: str) -> dict[str, Any]:
    """Create a small consistent error payload for tool results."""
    return {"error": {"code": code, "message": message}}


def json_text(payload: Any) -> str:
    """Serialize payloads as compact deterministic JSON text."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
