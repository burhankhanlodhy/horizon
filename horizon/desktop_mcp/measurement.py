"""Offline token estimates and metadata-only local evaluation accounting.

Claude's tokenizer is not public.  This module uses a verified local
``o200k_base`` cache when one is already present, and never asks tiktoken to
download a missing vocabulary.  Counts are estimates, not provider usage.
"""

from __future__ import annotations

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
    if isinstance(value, str) and len(value) <= maximum and _SAFE_CODE.fullmatch(value):
        return value
    return None


def _safe_nonnegative(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _sum(events: tuple[dict[str, Any], ...], field: str) -> int:
    return sum(event[field] for event in events if isinstance(event.get(field), int))
