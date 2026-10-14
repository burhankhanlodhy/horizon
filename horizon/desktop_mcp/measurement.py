"""Offline token estimates and metadata-only local evaluation accounting.

Claude's tokenizer is not public.  This module uses a verified local
``o200k_base`` cache when one is already present, and never asks tiktoken to
download a missing vocabulary.  Counts are estimates, not provider usage.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import tempfile
import threading
import time
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_O200K_URL = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
_O200K_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
_TOKENIZER_NAME = "o200k_base"
_POLICY_VERSION_FALLBACK = "phase-c-measurement-v1"
_ALLOWED_TOOLS = frozenset(
    {
        "contextshrink_search",
        "contextshrink_open",
        "contextshrink_retrieve",
        "contextshrink_stats",
        "contextshrink_analyze",
        "contextshrink_render",
        "contextshrink_retrieve_batch",
        "unknown",
    }
)
_ALLOWED_MODES = frozenset(
    {"exact", "compact", "focused", "overview", "computed", "artifact", "batch", "unknown"}
)
_ALLOWED_STATUS = frozenset({"success", "error", "fallback", "unknown"})
_SAFE_CODE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
# Counter and tokenizer labels carry versions such as "tiktoken-0.14.0".
_SAFE_LABEL = re.compile(r"^[a-zA-Z0-9_.:+-]{1,64}$")
_CACHE_LOAD_LOCK = threading.Lock()
_COUNT_CACHE_MAX_ENTRIES = 128


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class TokenEstimator:
    """Count text with a cached Claude stand-in, or a labeled byte heuristic.

    ``counting_model`` records the configured assumption only; it does not
    select or contact a remote model.  The o200k vocabulary is loaded only
    after its local cache file has passed a SHA-256 check.
    """

    def __init__(self, counting_model: str | None = None) -> None:
        self.counting_model = counting_model
        self._encoding: Any | None = None
        self._tiktoken_version: str | None = None
        self._load_error: str | None = None
        # Keep content digests and counts only. The source/result strings are
        # never retained, and the cache is bounded independently per worker.
        self._count_cache: OrderedDict[tuple[str, int, bytes], int] = OrderedDict()
        self._count_cache_lock = threading.RLock()
        self._load_cached_o200k()

    def _load_cached_o200k(self) -> None:
        try:
            cache_dir = Path(
                os.environ.get("TIKTOKEN_CACHE_DIR")
                or (Path(tempfile.gettempdir()) / "data-gym-cache")
            )
            cache_key = hashlib.sha1(_O200K_URL.encode("utf-8")).hexdigest()
            cache_path = cache_dir / cache_key
            if not cache_path.is_file():
                self._load_error = "verified_vocab_cache_missing"
                return
            digest = hashlib.sha256(cache_path.read_bytes()).hexdigest()
            if not hmac.compare_digest(digest, _O200K_SHA256):
                self._load_error = "verified_vocab_cache_invalid"
                return

            # tiktoken's public registry may download on a cache miss. Patch
            # its one o200k loader to consume the already verified local file
            # directly. This avoids a cache TOCTOU/network fallback if the file
            # disappears between our hash check and tokenizer construction.
            import importlib

            import tiktoken_ext.openai_public as openai_public
            from tiktoken import Encoding
            from tiktoken.load import load_tiktoken_bpe

            with _CACHE_LOAD_LOCK:
                original_loader = openai_public.load_tiktoken_bpe

                def local_loader(blobpath: str, expected_hash: str | None = None) -> Any:
                    if blobpath != _O200K_URL or expected_hash != _O200K_SHA256:
                        raise RuntimeError("unexpected_encoding_loader_request")
                    return load_tiktoken_bpe(str(cache_path), expected_hash=_O200K_SHA256)

                openai_public.load_tiktoken_bpe = local_loader
                try:
                    # Build directly from the versioned public specification
                    # and verified local ranks. This bypasses tiktoken's global
                    # registry, whose cached entry could have been constructed
                    # before this verification or whose miss path can fetch.
                    self._encoding = Encoding(**openai_public.o200k_base())
                finally:
                    openai_public.load_tiktoken_bpe = original_loader
            try:
                self._tiktoken_version = importlib.metadata.version("tiktoken")
            except importlib.metadata.PackageNotFoundError:
                self._tiktoken_version = "unknown"
        except Exception as error:  # Keep measurement available without a vocab.
            self._encoding = None
            self._load_error = f"offline_load_failed:{type(error).__name__}"

    def count_text(self, text: str) -> int:
        """Return an explicitly estimated count for one text value."""
        if not isinstance(text, str):
            raise TypeError("text must be str")
        if not text:
            return 0
        encoded = text.encode("utf-8")
        digest = hashlib.sha256(encoded).digest()
        if self._encoding is not None:
            basis = f"o200k:{self._tiktoken_version or 'unknown'}"
            cache_key = (basis, len(encoded), digest)
            cached = self._get_cached_count(cache_key)
            if cached is not None:
                return cached
            try:
                value = len(self._encoding.encode(text, disallowed_special=()))
                self._put_cached_count(cache_key, value)
                return value
            except Exception as error:
                self._encoding = None
                self._load_error = f"offline_count_failed:{type(error).__name__}"
        cache_key = ("utf8_bytes_div_3_heuristic:v1", len(encoded), digest)
        cached = self._get_cached_count(cache_key)
        if cached is not None:
            return cached
        # Deliberately conservative and portable. This is still a heuristic,
        # and the independent byte cap is the actual resource bound.
        value = max(1, math.ceil(len(encoded) / 3))
        self._put_cached_count(cache_key, value)
        return value

    def _get_cached_count(self, key: tuple[str, int, bytes]) -> int | None:
        with self._count_cache_lock:
            value = self._count_cache.get(key)
            if value is not None:
                self._count_cache.move_to_end(key)
            return value

    def _put_cached_count(self, key: tuple[str, int, bytes], value: int) -> None:
        with self._count_cache_lock:
            self._count_cache[key] = value
            self._count_cache.move_to_end(key)
            while len(self._count_cache) > _COUNT_CACHE_MAX_ENTRIES:
                self._count_cache.popitem(last=False)

    def provenance(self) -> dict[str, Any]:
        """Describe estimator provenance without exposing cache paths."""
        if self._encoding is not None:
            return {
                "count_status": "estimate_not_claude_count",
                "tokenizer_name": _TOKENIZER_NAME,
                "counter_version": f"tiktoken-{self._tiktoken_version or 'unknown'}",
                "counting_model_assumption": self.counting_model,
                "fallback": False,
            }
        return {
            "count_status": "heuristic_estimate_not_claude_count",
            "tokenizer_name": "utf8_bytes_div_3_heuristic",
            "counter_version": "desktop-mcp-byte-estimator-v1",
            "counting_model_assumption": self.counting_model,
            "fallback": True,
            "fallback_reason": self._load_error or "encoding_count_failed",
        }


def measure_payload(payload: Any, estimator: TokenEstimator | None = None) -> dict[str, Any]:
    """Measure a complete compact JSON result payload at the inner MCP stage.

    This does not include SDK JSON-RPC framing, registered tool schemas,
    Desktop-added context, or provider-hidden prompt material.
    """
    active = estimator or TokenEstimator()
    encoded = _compact_json(payload)
    provenance = active.provenance()
    return {
        "utf8_bytes": len(encoded.encode("utf-8")),
        "token_estimate": active.count_text(encoded),
        **provenance,
        "measured_stage": "inner_mcp_result_json_text",
        "sdk_wrapping": "excluded",
        "host_overhead": "unknown",
    }


def _payload_with_accounting(payload: dict[str, Any], accounting: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result["accounting"] = accounting
    return result


def _count_pair(
    payload: dict[str, Any], raw_payload: dict[str, Any], estimator: TokenEstimator
) -> tuple[dict[str, Any], dict[str, Any]]:
    delivered = measure_payload(payload, estimator)
    raw = measure_payload(raw_payload, estimator)
    return raw, delivered


def finalize_accounting(
    payload: dict[str, Any],
    raw_payload: dict[str, Any],
    raw_text: str,
    rendered_text: str,
    *,
    estimator: TokenEstimator | None = None,
    retrieval_call_count: int = 0,
    retrieval_bytes: int = 0,
    retrieval_token_estimate: int = 0,
) -> dict[str, Any]:
    """Attach bounded, self-inclusive accounting to a complete result.

    The raw baseline must be the same metadata/scope with ``raw_text`` in the
    same text-bearing field as ``rendered_text`` in ``payload``.  Counting is
    performed on the full inner JSON result, including its metadata and the
    final accounting object.  Three passes bound self-counting overhead.
    """
    if not isinstance(payload, dict) or not isinstance(raw_payload, dict):
        raise TypeError("payload and raw_payload must be dictionaries")
    if not isinstance(raw_text, str) or not isinstance(rendered_text, str):
        raise TypeError("raw_text and rendered_text must be strings")
    if (
        isinstance(retrieval_call_count, bool)
        or not isinstance(retrieval_call_count, int)
        or retrieval_call_count < 0
    ):
        raise ValueError("retrieval_call_count must be a nonnegative integer")
    if (
        isinstance(retrieval_bytes, bool)
        or not isinstance(retrieval_bytes, int)
        or retrieval_bytes < 0
    ):
        raise ValueError("retrieval_bytes must be a nonnegative integer")
    if (
        isinstance(retrieval_token_estimate, bool)
        or not isinstance(retrieval_token_estimate, int)
        or retrieval_token_estimate < 0
    ):
        raise ValueError("retrieval_token_estimate must be a nonnegative integer")

    active = estimator or TokenEstimator()
    clean_payload = {key: value for key, value in payload.items() if key != "accounting"}
    clean_raw = {key: value for key, value in raw_payload.items() if key != "accounting"}
    provenance = active.provenance()
    accounting: dict[str, Any] = {
        "count_basis": provenance["count_status"],
        "counter_version": provenance["counter_version"],
        "configured_model_assumption": provenance["counting_model_assumption"],
        "tokenizer_name": provenance["tokenizer_name"],
        "fallback": provenance["fallback"],
        "measured_stage": "inner_mcp_result_json_text",
        "sdk_wrapping": "excluded",
        "host_overhead": "unknown",
        "source": {},
        "result": {},
        "retrieval": {
            "call_count": retrieval_call_count,
            "delivered_utf8_bytes": retrieval_bytes,
            "delivered_token_estimate": retrieval_token_estimate,
        },
    }

    for _ in range(16):
        counted_raw, counted_delivered = _count_pair(
            _payload_with_accounting(clean_payload, accounting),
            _payload_with_accounting(clean_raw, accounting),
            active,
        )
        raw_source_bytes = len(raw_text.encode("utf-8"))
        delivered_source_bytes = len(rendered_text.encode("utf-8"))
        raw_source_tokens = active.count_text(raw_text)
        delivered_source_tokens = active.count_text(rendered_text)
        updated = {
            **accounting,
            "source": {
                "raw_utf8_bytes": raw_source_bytes,
                "rendered_utf8_bytes": delivered_source_bytes,
                "raw_token_estimate": raw_source_tokens,
                "rendered_token_estimate": delivered_source_tokens,
                "byte_delta": raw_source_bytes - delivered_source_bytes,
                "token_delta_estimate": raw_source_tokens - delivered_source_tokens,
            },
            "result": {
                "raw_equivalent_utf8_bytes": counted_raw["utf8_bytes"],
                "delivered_utf8_bytes": counted_delivered["utf8_bytes"],
                "raw_equivalent_token_estimate": counted_raw["token_estimate"],
                "delivered_token_estimate": counted_delivered["token_estimate"],
                "byte_delta": counted_raw["utf8_bytes"] - counted_delivered["utf8_bytes"],
                "token_delta_estimate": counted_raw["token_estimate"]
                - counted_delivered["token_estimate"],
            },
        }
        if updated == accounting:
            break
        accounting = updated

    # Ensure byte fields describe the exact payload returned. Byte counts
    # converge because only the decimal width of the recorded count feeds back
    # into the serialized payload size.
    for _ in range(8):
        final_payload = _payload_with_accounting(clean_payload, accounting)
        final_raw_payload = _payload_with_accounting(clean_raw, accounting)
        actual_delivered = measure_payload(final_payload, active)
        actual_raw = measure_payload(final_raw_payload, active)
        result_accounting = accounting["result"]
        if (
            result_accounting.get("delivered_utf8_bytes") == actual_delivered["utf8_bytes"]
            and result_accounting.get("raw_equivalent_utf8_bytes") == actual_raw["utf8_bytes"]
        ):
            break
        result_accounting["delivered_utf8_bytes"] = actual_delivered["utf8_bytes"]
        result_accounting["raw_equivalent_utf8_bytes"] = actual_raw["utf8_bytes"]
        result_accounting["byte_delta"] = actual_raw["utf8_bytes"] - actual_delivered["utf8_bytes"]

    return _payload_with_accounting(clean_payload, accounting)


class LocalUsageLedger:
    """Bounded in-memory HMAC event log for local measurement integrity.

    The signing key is created per instance and is never returned.  Events
    disappear with the worker; the MAC is not provider attestation or billing
    evidence.  Event fields are allowlisted metadata only.
    """

    def __init__(self, *, max_events: int = 2000, clock: Any = time.time) -> None:
        if isinstance(max_events, bool) or not isinstance(max_events, int) or max_events < 1:
            raise ValueError("max_events must be a positive integer")
        self.max_events = max_events
        self._clock = clock
        self._key = secrets.token_bytes(32)
        self._generation = secrets.token_hex(8)
        self._events: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    def record(
        self,
        *,
        tool: str,
        mode: str,
        source_version: str | None,
        policy_version: str = _POLICY_VERSION_FALLBACK,
        status: str = "success",
        accounting: dict[str, Any] | None = None,
        compression_applied: bool = False,
        latency_ms: int | float | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        """Append a sanitized event; raw argument values are never accepted."""
        safe_tool = tool if tool in _ALLOWED_TOOLS else "unknown"
        safe_mode = mode if mode in _ALLOWED_MODES else "unknown"
        safe_status = status if status in _ALLOWED_STATUS else "unknown"
        safe_policy = (
            policy_version
            if isinstance(policy_version, str) and _SAFE_CODE.fullmatch(policy_version)
            else _POLICY_VERSION_FALLBACK
        )
        version_hash = (
            hashlib.sha256(source_version.encode("utf-8", errors="replace")).hexdigest()
            if isinstance(source_version, str)
            else None
        )
        if (
            isinstance(latency_ms, bool)
            or not isinstance(latency_ms, (int, float))
            or not math.isfinite(latency_ms)
            or latency_ms < 0
        ):
            latency_value: float | None = None
        else:
            latency_value = round(float(latency_ms), 3)
        safe_error = (
            error_code if isinstance(error_code, str) and _SAFE_CODE.fullmatch(error_code) else None
        )
        acct = accounting if isinstance(accounting, dict) else {}
        result_acct = acct.get("result") if isinstance(acct.get("result"), dict) else {}
        source_acct = acct.get("source") if isinstance(acct.get("source"), dict) else {}
        event: dict[str, Any] = {
            "ledger_generation": self._generation,
            "sequence": 0,
            "timestamp_unix": round(float(self._clock()), 3),
            "tool": safe_tool,
            "mode": safe_mode,
            "status": safe_status,
            "policy_version": safe_policy,
            "source_version_sha256": version_hash,
            "latency_ms": latency_value,
            "error_code": safe_error,
            "compression_applied": compression_applied is True,
            "count_basis": _safe_text(acct.get("count_basis"), 64),
            "counter_version": _safe_text(acct.get("counter_version"), 64),
            "tokenizer_name": _safe_text(acct.get("tokenizer_name"), 64),
            "fallback": bool(acct.get("fallback", False)),
            "source_raw_utf8_bytes": _safe_nonnegative(source_acct.get("raw_utf8_bytes")),
            "source_rendered_utf8_bytes": _safe_nonnegative(source_acct.get("rendered_utf8_bytes")),
            "source_raw_token_estimate": _safe_nonnegative(source_acct.get("raw_token_estimate")),
            "source_rendered_token_estimate": _safe_nonnegative(
                source_acct.get("rendered_token_estimate")
            ),
            "result_raw_equivalent_utf8_bytes": _safe_nonnegative(
                result_acct.get("raw_equivalent_utf8_bytes")
            ),
            "result_delivered_utf8_bytes": _safe_nonnegative(
                result_acct.get("delivered_utf8_bytes")
            ),
            "result_raw_equivalent_token_estimate": _safe_nonnegative(
                result_acct.get("raw_equivalent_token_estimate")
            ),
            "result_delivered_token_estimate": _safe_nonnegative(
                result_acct.get("delivered_token_estimate")
            ),
        }
        with self._lock:
            event["sequence"] = (self._events[-1]["sequence"] + 1) if self._events else 1
            mac = hmac.new(
                self._key, _compact_json(event).encode("utf-8"), hashlib.sha256
            ).hexdigest()
            signed = {**event, "local_hmac_sha256": mac}
            self._events.append(signed)
            if len(self._events) > self.max_events:
                del self._events[: len(self._events) - self.max_events]
            return dict(signed)

    def verify(self, event: dict[str, Any]) -> bool:
        """Verify an event while this process still holds its ephemeral key."""
        if not isinstance(event, dict) or not isinstance(event.get("local_hmac_sha256"), str):
            return False
        body = {key: value for key, value in event.items() if key != "local_hmac_sha256"}
        expected = hmac.new(
            self._key, _compact_json(body).encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, event["local_hmac_sha256"])

    def stats(self) -> dict[str, Any]:
        """Return aggregate counts only; never return the key or event text."""
        with self._lock:
            events = tuple(self._events)
        by_mode: dict[str, int] = dict(Counter(event["mode"] for event in events))
        errors = sum(1 for event in events if event["status"] == "error")
        fallbacks = sum(1 for event in events if event["fallback"])
        successful_compact_results = sum(
            1
            for event in events
            if event["tool"] == "contextshrink_open"
            and event["mode"] == "compact"
            and event["status"] == "success"
            and event["compression_applied"] is True
        )
        return {
            "event_count": len(events),
            "max_events": self.max_events,
            "ledger_generation": self._generation,
            "error_count": errors,
            "fallback_count": fallbacks,
            "retained_successful_compact_result_count": successful_compact_results,
            "retained_successful_compact_result_scope": "retained successful compact responses in bounded worker ledger",
            "by_mode": by_mode,
            "counterfactual_per_call_raw_equivalent_inner_result_utf8_bytes_sum": _sum(
                events, "result_raw_equivalent_utf8_bytes"
            ),
            "per_call_delivered_inner_result_utf8_bytes_sum": _sum(
                events, "result_delivered_utf8_bytes"
            ),
            "counterfactual_per_call_raw_equivalent_token_estimate_sum": _sum(
                events, "result_raw_equivalent_token_estimate"
            ),
            "per_call_delivered_token_estimate_sum": _sum(
                events, "result_delivered_token_estimate"
            ),
            "aggregate_scope": "retained per-call observations; not complete task totals or measured savings",
            "billing_attribution": "not_measured",
            "signature_scope": "ephemeral_local_integrity_only",
            "persistence": "in_memory_worker_lifetime",
        }


def _safe_text(value: Any, maximum: int) -> str | None:
    if isinstance(value, str) and len(value) <= maximum and _SAFE_LABEL.fullmatch(value):
        return value
    return None


def _safe_nonnegative(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _sum(events: tuple[dict[str, Any], ...], field: str) -> int:
    return sum(event[field] for event in events if isinstance(event.get(field), int))


_CUMULATIVE_SCHEMA_VERSION = 1
_CUMULATIVE_MAX_FILE_BYTES = 64 * 1024
_CUMULATIVE_MAX_KEYS = 64
_CUMULATIVE_OTHER_KEY = "other"
_CUMULATIVE_BASIS_MAX = 128
_CUMULATIVE_SCOPE = (
    "cumulative per-call estimates across broker restarts on this machine; "
    "not task totals or measured savings"
)
_CUMULATIVE_INTEGRITY = "plain local file; editable and not evidence"
_CUMULATIVE_RESET = "delete the stats file to start over"
_CUMULATIVE_COUNTER_MAPS = ("by_tool", "by_mode", "by_status")
_CUMULATIVE_COUNT_FIELDS = ("calls", "error_count", "fallback_count", "compact_result_count")


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _empty_cumulative_totals() -> dict[str, Any]:
    return {
        "since": None,
        "last_updated": None,
        "calls": 0,
        "by_tool": {},
        "by_mode": {},
        "by_status": {},
        "error_count": 0,
        "fallback_count": 0,
        "compact_result_count": 0,
        "token_estimates": {},
        "byte_totals": {"raw_equivalent_sum": 0, "delivered_sum": 0},
    }


def _valid_counter_map(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and len(value) <= _CUMULATIVE_MAX_KEYS
        and all(isinstance(key, str) and _is_nonnegative_int(count) for key, count in value.items())
    )


def _valid_token_map(value: Any) -> bool:
    if not isinstance(value, dict) or len(value) > _CUMULATIVE_MAX_KEYS:
        return False
    for key, entry in value.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            return False
        if not all(
            _is_nonnegative_int(entry.get(field))
            for field in ("calls", "raw_equivalent_sum", "delivered_sum")
        ):
            return False
    return True


def _parse_cumulative_file(raw: bytes) -> dict[str, Any] | None:
    """Return validated totals from a stats file, or None when it is unusable."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("schema_version")
    if not _is_nonnegative_int(version) or version != _CUMULATIVE_SCHEMA_VERSION:
        return None
    totals = _empty_cumulative_totals()
    for stamp in ("since", "last_updated"):
        value = data.get(stamp)
        if value is not None and (not isinstance(value, str) or len(value) > 64):
            return None
        totals[stamp] = value
    if not all(_is_nonnegative_int(data.get(field)) for field in _CUMULATIVE_COUNT_FIELDS):
        return None
    if not all(_valid_counter_map(data.get(name)) for name in _CUMULATIVE_COUNTER_MAPS):
        return None
    if not _valid_token_map(data.get("token_estimates")):
        return None
    byte_totals = data.get("byte_totals")
    if not isinstance(byte_totals, dict) or not all(
        _is_nonnegative_int(byte_totals.get(field))
        for field in ("raw_equivalent_sum", "delivered_sum")
    ):
        return None
    for field in _CUMULATIVE_COUNT_FIELDS:
        totals[field] = data[field]
    for name in _CUMULATIVE_COUNTER_MAPS:
        totals[name] = dict(data[name])
    totals["token_estimates"] = {
        key: {field: entry[field] for field in ("calls", "raw_equivalent_sum", "delivered_sum")}
        for key, entry in data["token_estimates"].items()
    }
    totals["byte_totals"] = {
        field: byte_totals[field] for field in ("raw_equivalent_sum", "delivered_sum")
    }
    return totals


def _cumulative_key(value: Any) -> str:
    if isinstance(value, str) and _SAFE_LABEL.fullmatch(value):
        return value
    return "unknown"


def _cumulative_choice(value: Any, allowed: frozenset[str]) -> str:
    return value if isinstance(value, str) and value in allowed else "unknown"


def _cumulative_bucket(counts: dict[str, Any], key: str) -> str:
    """Keep at most _CUMULATIVE_MAX_KEYS distinct keys; extras share "other"."""
    if key in counts or len(counts) < _CUMULATIVE_MAX_KEYS:
        return key
    return _CUMULATIVE_OTHER_KEY


def _cumulative_basis_key(event: dict[str, Any]) -> str:
    tokenizer = _cumulative_key(event.get("tokenizer_name"))
    counter = _cumulative_key(event.get("counter_version"))
    # "tokenizer" means a real token counter; "byte_estimate" means the
    # UTF-8 byte heuristic. Not to be confused with the exact read mode.
    flavor = "byte_estimate" if event.get("fallback") is True else "tokenizer"
    return f"{tokenizer}:{counter}:{flavor}"[:_CUMULATIVE_BASIS_MAX]


def _utc_stamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CumulativeUsageStats:
    """Per-call usage counters that survive broker and worker restarts.

    The file holds only the aggregate counters below, never per-call rows,
    timestamps per call, paths, queries, source text, hashes or error codes.
    Counts are per-call estimates; they are not task totals, measured savings
    or billing data. A missing, unreadable or invalid file starts fresh, and
    the file is left alone until the next successful write replaces it.
    """

    def __init__(self, path: Path | str, *, clock: Any = time.time) -> None:
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()
        self._totals = _empty_cumulative_totals()
        self.reset_reason: str | None = None
        self.write_error = False
        self._load()

    def _load(self) -> None:
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(_CUMULATIVE_MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return
        except OSError:
            self.reset_reason = "unreadable"
            return
        totals = None
        if len(raw) <= _CUMULATIVE_MAX_FILE_BYTES:
            totals = _parse_cumulative_file(raw)
        if totals is None:
            self.reset_reason = "invalid"
            return
        self._totals = totals

    def add(self, event: dict[str, Any]) -> None:
        """Fold one sanitized ledger event into the totals and persist them.

        Never raises: a failed write keeps the in-memory totals and sets
        ``write_error``.
        """
        if not isinstance(event, dict):
            return
        with self._lock:
            totals = self._totals
            stamp = _utc_stamp(float(self._clock()))
            if totals["since"] is None:
                totals["since"] = stamp
            totals["last_updated"] = stamp
            totals["calls"] += 1
            tool = _cumulative_choice(event.get("tool"), _ALLOWED_TOOLS)
            mode = _cumulative_choice(event.get("mode"), _ALLOWED_MODES)
            status = _cumulative_choice(event.get("status"), _ALLOWED_STATUS)
            for name, key in (("by_tool", tool), ("by_mode", mode), ("by_status", status)):
                counts = totals[name]
                bucket = _cumulative_bucket(counts, key)
                counts[bucket] = counts.get(bucket, 0) + 1
            if status == "error":
                totals["error_count"] += 1
            if event.get("fallback") is True:
                totals["fallback_count"] += 1
            if (
                status == "success"
                and event.get("compression_applied") is True
                and mode == "compact"
            ):
                totals["compact_result_count"] += 1
            raw_tokens = event.get("result_raw_equivalent_token_estimate")
            delivered_tokens = event.get("result_delivered_token_estimate")
            if _is_nonnegative_int(raw_tokens) and _is_nonnegative_int(delivered_tokens):
                tokens = totals["token_estimates"]
                bucket = _cumulative_bucket(tokens, _cumulative_basis_key(event))
                entry = tokens.setdefault(
                    bucket, {"calls": 0, "raw_equivalent_sum": 0, "delivered_sum": 0}
                )
                entry["calls"] += 1
                entry["raw_equivalent_sum"] += raw_tokens
                entry["delivered_sum"] += delivered_tokens
            raw_bytes = event.get("result_raw_equivalent_utf8_bytes")
            delivered_bytes = event.get("result_delivered_utf8_bytes")
            if _is_nonnegative_int(raw_bytes) and _is_nonnegative_int(delivered_bytes):
                totals["byte_totals"]["raw_equivalent_sum"] += raw_bytes
                totals["byte_totals"]["delivered_sum"] += delivered_bytes
            self._write_locked()

    def _write_locked(self) -> None:
        document = {"schema_version": _CUMULATIVE_SCHEMA_VERSION, **self._totals}
        data = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode("utf-8")
        temporary: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp"
            )
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
            self.write_error = False
        except OSError:
            self.write_error = True
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def snapshot(self) -> dict[str, Any]:
        """Public view of the totals; never includes the file path."""
        with self._lock:
            totals = copy.deepcopy(self._totals)
            reset_reason = self.reset_reason
            write_error = self.write_error
        token_estimates = {
            key: {
                **entry,
                "estimated_tokens_avoided": entry["raw_equivalent_sum"] - entry["delivered_sum"],
            }
            for key, entry in totals["token_estimates"].items()
        }
        view: dict[str, Any] = {
            "schema_version": _CUMULATIVE_SCHEMA_VERSION,
            **totals,
            "token_estimates": token_estimates,
            "persistence": "local_file",
            "scope": _CUMULATIVE_SCOPE,
            "billing_attribution": "not_measured",
            "integrity": _CUMULATIVE_INTEGRITY,
            "reset": _CUMULATIVE_RESET,
        }
        if reset_reason is not None:
            view["reset_reason"] = reset_reason
        if write_error:
            view["write_error"] = True
        return view
