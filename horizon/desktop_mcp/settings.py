"""Validated, explicit configuration for the local desktop MCP server."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class ConfigurationError(Exception):
    """Sanitized configuration failure safe to report to an MCP client."""

    def __init__(self, code: str, fields: tuple[str, ...] = ()) -> None:
        self.code = code
        self.fields = fields
        self.message = f"Configuration error ({code})"
        super().__init__(self.message)


def _comparable(path: Path) -> Path:
    """Lexically normalized, case-folded on Windows, for overlap checks only."""
    normalized = Path(os.path.normpath(path))
    return Path(os.path.normcase(str(normalized)))


def _overlaps_approved_roots(path: Path, roots: tuple[Path, ...]) -> bool:
    """True when ``path`` is, contains, or lies inside any approved root."""
    candidate = _comparable(path)
    for root in roots:
        approved = _comparable(root)
        if candidate == approved or approved in candidate.parents or candidate in approved.parents:
            return True
    return False


class DesktopSettings(BaseModel):
    """Strict settings for diagnostic fixtures or approved local sources."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    profile: Literal["diagnostic", "local"] = "diagnostic"
    approved_roots: tuple[Path, ...] = ()
    broker_id: str = "desktop"
    result_token_budget: int = Field(default=2000, ge=128, le=8000)
    max_result_bytes: int = Field(default=65536, ge=1024, le=1048576)
    max_input_bytes: int = Field(default=32768, ge=1024, le=1048576)
    max_source_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)
    max_snapshot_bytes: int = Field(default=16 * 1024 * 1024, ge=1024, le=256 * 1024 * 1024)
    max_store_bytes: int = Field(default=128 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024)
    max_snapshots: int = Field(default=64, ge=1, le=4096)
    max_broker_clients: int = Field(default=8, ge=1, le=64)
    broker_timeout_seconds: int = Field(default=10, ge=1, le=60)
    # 0 keeps the broker running until stopped; otherwise it stops after this many
    # idle seconds (validated below).
    broker_idle_seconds: int = Field(default=0)
    snapshot_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    counting_model: str | None = None
    eval_arm: Literal["raw", "optimized"] = "optimized"
    min_compaction_gain_tokens: int = Field(default=64, ge=0, le=8000)
    min_compaction_gain_percent: float = Field(default=5.0, ge=0.0, le=100.0)
    # "compute" adds the analyze, render and retrieve_batch tools; the tool
    # list is fixed for a given config and never changes per chat.
    tool_profile: Literal["source", "compute"] = "source"
    # Generated artifacts live here, never inside an approved root, so they
    # cannot be cataloged or opened as sources. None disables rendering.
    artifact_root: Path | None = None
    # Cumulative per-call usage counters persist here across broker restarts.
    # None keeps usage in worker memory only. Must not overlap an approved root.
    usage_stats_path: Path | None = None

    @field_validator("approved_roots", mode="before")
    @classmethod
    def roots_must_be_absolute(cls, value: Any) -> tuple[Path, ...]:
        if not isinstance(value, (tuple, list)):
            raise ValueError("must be a sequence")
        roots: list[Path] = []
        for root in value:
            if isinstance(root, Path):
                path = root
            elif isinstance(root, str):
                path = Path(root)
            else:
                raise ValueError("each root must be a path")
            if not path.is_absolute():
                raise ValueError("each root must be absolute")
            roots.append(path)
        return tuple(roots)

    @field_validator("artifact_root", mode="before")
    @classmethod
    def artifact_root_must_be_absolute(cls, value: Any) -> Path | None:
        if value is None:
            return None
        if isinstance(value, Path):
            path = value
        elif isinstance(value, str):
            path = Path(value)
        else:
            raise ValueError("must be a path")
        if not path.is_absolute():
            raise ValueError("must be absolute")
        return path

    @field_validator("usage_stats_path", mode="before")
    @classmethod
    def usage_stats_path_must_be_absolute(cls, value: Any) -> Path | None:
        if value is None:
            return None
        if isinstance(value, Path):
            path = value
        elif isinstance(value, str):
            path = Path(value)
        else:
            raise ValueError("must be a path")
        if not path.is_absolute():
            raise ValueError("must be absolute")
        return path

    @field_validator("broker_id")
    @classmethod
    def broker_id_is_safe(cls, value: str) -> str:
        if (
            not value
            or len(value) > 32
            or any(
                char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                for char in value
            )
        ):
            raise ValueError("must be 1 to 32 ASCII letters, digits, underscores, or hyphens")
        return value

    @field_validator("broker_idle_seconds")
    @classmethod
    def broker_idle_seconds_is_bounded(cls, value: int) -> int:
        if value != 0 and not 60 <= value <= 86400:
            raise ValueError("must be 0 or between 60 and 86400 seconds")
        return value

    @model_validator(mode="after")
    def source_limits_are_consistent(self) -> DesktopSettings:
        if len(self.approved_roots) > 32:
            raise ValueError("at most 32 approved roots are supported")
        if self.max_source_bytes > 25 * 1024 * 1024:
            raise ValueError("max_source_bytes exceeds the source-reader limit")
        if self.max_snapshot_bytes < self.max_source_bytes:
            raise ValueError("max_snapshot_bytes must be at least max_source_bytes")
        if self.max_store_bytes < self.max_snapshot_bytes:
            raise ValueError("max_store_bytes must be at least max_snapshot_bytes")
        if self.tool_profile == "compute" and self.profile != "local":
            raise ValueError("tool_profile compute requires the local profile")
        if self.artifact_root is not None and _overlaps_approved_roots(
            self.artifact_root, self.approved_roots
        ):
            raise ValueError("artifact_root must not overlap an approved root")
        if self.usage_stats_path is not None and _overlaps_approved_roots(
            self.usage_stats_path, self.approved_roots
        ):
            raise ValueError("usage_stats_path must not overlap an approved root")
        return self

    @field_validator("counting_model")
    @classmethod
    def counting_model_is_bounded(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or len(value) > 128):
            raise ValueError("must be 1 to 128 nonblank characters")
        return value


_MAX_CONFIG_BYTES = 65536
_CONFIG_FIELDS = {
    "profile",
    "approved_roots",
    "broker_id",
    "result_token_budget",
    "max_result_bytes",
    "max_input_bytes",
    "max_source_bytes",
    "max_snapshot_bytes",
    "max_store_bytes",
    "max_snapshots",
    "max_broker_clients",
    "broker_timeout_seconds",
    "broker_idle_seconds",
    "snapshot_ttl_seconds",
    "counting_model",
    "eval_arm",
    "min_compaction_gain_tokens",
    "min_compaction_gain_percent",
    "tool_profile",
    "artifact_root",
    "usage_stats_path",
}


def _validation_fields(error: ValidationError) -> tuple[str, ...]:
    """Return allowlisted field names; never reflect arbitrary config keys."""
    fields = {
        str(item["loc"][0]) if str(item["loc"][0]) in _CONFIG_FIELDS else "unknown_field"
        for item in error.errors()
        if item.get("loc")
    }
    return tuple(sorted(fields))


def load_settings(config_path: Path | None = None) -> DesktopSettings:
    """Load an explicitly supplied JSON config, or return defaults.

    The loader never searches environment variables, creates directories, or
    writes configuration.  An explicit file is capped before JSON decoding.
    """
    if config_path is None:
        return DesktopSettings()
    try:
        path = Path(config_path)
        with path.open("rb") as handle:
            raw = handle.read(_MAX_CONFIG_BYTES + 1)
    except (OSError, TypeError, ValueError):
        raise ConfigurationError("config_unreadable") from None
    if len(raw) > _MAX_CONFIG_BYTES:
        raise ConfigurationError("config_too_large")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, ValueError):
        raise ConfigurationError("config_invalid_json") from None
    if not isinstance(data, dict):
        raise ConfigurationError("config_invalid_shape")
    try:
        return DesktopSettings.model_validate(data)
    except ValidationError as error:
        raise ConfigurationError("config_invalid", _validation_fields(error)) from None
