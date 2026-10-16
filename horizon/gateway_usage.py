"""Exact per-request usage metering for the Desktop gateway.

``horizon proxy`` used as Claude Desktop's 3P gateway (or any Anthropic client)
needs one durable record per ``/v1/messages`` request: the provider's own usage
split into uncached input, cache reads, 5m and 1h cache writes and output, plus
the list-price cost of exactly that usage. :func:`emit_outcome` writes those
records to ``gateway_usage.jsonl`` in the workspace.

Guarantees:

* One line per request. The line is written when the request's outcome is
  recorded, which covers every upstream status including errors. A request that
  never reaches the outcome funnel (a pre-upstream rejection, for example) gets
  a zero-usage line at request end, with ``usage_source="none"``.
* The sink never raises into the request path, and never writes message
  content. Lines hold ids, counts, prices, the model name and the flags.
* Only ``POST .../v1/messages`` is metered. The middleware is a pure ASGI
  wrapper that reads the request body only to pick up ``stream``, ``model`` and
  ``metadata.user_id``, and keeps nothing else from it.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from horizon.paths import workspace_dir

logger = logging.getLogger("horizon.gateway_usage")

SINK_FILENAME = "gateway_usage.jsonl"
SCHEMA_VERSION = 1
SESSION_HEADER = "x-claude-code-session-id"
MESSAGES_PATH_SUFFIX = "/v1/messages"

# Bodies larger than this are not parsed for metadata; the session then comes
# from the header alone. Keeps the middleware's buffering bounded.
_MAX_PARSED_BODY_BYTES = 32 * 1024 * 1024

BASIS_NONE = "no-usage"
BASIS_UNPRICED = "unpriced"
# Seat and usage plans (Claude Enterprise, Claude Code OAuth) may bill
# differently from API list prices, so their cost is labelled as an estimate.
BASIS_LIST_ESTIMATE = "list_price_estimate"

AUTH_API_KEY = "api_key"
AUTH_OAUTH = "oauth"
AUTH_OTHER = "other"

_TOKEN_KEYS = ("input", "cache_read", "cache_write_5m", "cache_write_1h", "output")
_COST_KEYS = ("input", "cache_read", "cache_write", "output")
_ZERO_BREAKDOWN: dict[str, float] = dict.fromkeys(_COST_KEYS, 0.0)

# A session id is an opaque token. Anything that is not shaped like one is
# dropped rather than recorded, so a stray value can never carry content.
_SESSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
# Claude Code's metadata.user_id ends in ``..._session_<id>``.
_USER_ID_SESSION_RE = re.compile(r"_session_([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})$")

_write_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Session extraction                                                           #
# --------------------------------------------------------------------------- #


def session_from_header(value: Any) -> str | None:
    """The ``x-claude-code-session-id`` header value, when it is a plain token."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _SESSION_RE.fullmatch(candidate) else None


def session_from_user_id(user_id: Any) -> str | None:
    """Session id from ``metadata.user_id``.

    Accepts the ``..._session_<id>`` suffix and a JSON object with a
    ``session_id`` (or ``sessionId``) member. Never raises. Any other shape gives
    ``None``, and the raw value is not kept.
    """
    try:
        candidate: Any = None
        if isinstance(user_id, dict):
            candidate = user_id.get("session_id", user_id.get("sessionId"))
        elif isinstance(user_id, str):
            text = user_id.strip()
            if text.startswith("{"):
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    candidate = parsed.get("session_id", parsed.get("sessionId"))
            else:
                match = _USER_ID_SESSION_RE.search(text)
                candidate = match.group(1) if match else None
        if isinstance(candidate, str) and _SESSION_RE.fullmatch(candidate.strip()):
            return candidate.strip()
    except Exception:  # noqa: BLE001 - metadata shapes vary; never fail a request
        return None
    return None


def auth_class(headers: Mapping[str, Any] | None) -> str:
    """Classify how the request authenticates: ``api_key``, ``oauth`` or ``other``.

    Classification only. The credential itself is never returned, stored or
    logged. ``x-api-key`` wins. A ``Bearer`` token is an API key when it has the
    ``sk-ant-api`` prefix, and an OAuth token otherwise (Claude Code seat
    sign-in).
    """
    try:
        values = headers or {}
        if values.get("x-api-key"):
            return AUTH_API_KEY
        authorization = values.get("authorization")
        if isinstance(authorization, str):
            scheme, _, credential = authorization.strip().partition(" ")
            credential = credential.strip()
            if scheme.lower() == "bearer" and credential:
                return AUTH_API_KEY if credential.startswith("sk-ant-api") else AUTH_OAUTH
    except Exception:  # noqa: BLE001 - classification must never fail a request
        return AUTH_OTHER
    return AUTH_OTHER


def resolve_session(headers: Mapping[str, Any] | None, body: Any = None) -> str | None:
    """Session for a request: the header, else ``body.metadata.user_id``, else None."""
    header_value = session_from_header((headers or {}).get(SESSION_HEADER))
    if header_value is not None:
        return header_value
    if isinstance(body, dict):
        metadata = body.get("metadata")
        if isinstance(metadata, dict):
            return session_from_user_id(metadata.get("user_id"))
    return None


# --------------------------------------------------------------------------- #
# Usage and pricing                                                            #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Usage:
    """Provider-reported token counts for one request, by billing class."""

    input: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    output: int = 0

    def is_zero(self) -> bool:
        return not (
            self.input
            or self.cache_read
            or self.cache_write_5m
            or self.cache_write_1h
            or self.output
        )


@dataclass(frozen=True)
class PriceQuote:
    """List-price cost of a :class:`Usage`, with the basis it was priced on."""

    cost_usd: float | None
    breakdown: dict[str, float] | None
    basis: str


def usage_from_outcome(outcome: Any) -> Usage:
    """The provider's usage from a :class:`~horizon.proxy.outcome.RequestOutcome`.

    The TTL split comes from ``usage.cache_creation``. Cache writes that carry
    no TTL split are billed at the default 5m rate, so the unsplit remainder of
    ``cache_write_tokens`` is counted as 5m.
    """
    write_5m = _int(getattr(outcome, "cache_write_5m_tokens", 0))
    write_1h = _int(getattr(outcome, "cache_write_1h_tokens", 0))
    write_total = _int(getattr(outcome, "cache_write_tokens", 0))
    write_5m += max(0, write_total - write_5m - write_1h)
    return Usage(
        input=_int(getattr(outcome, "uncached_input_tokens", 0)),
        cache_read=_int(getattr(outcome, "cache_read_tokens", 0)),
        cache_write_5m=write_5m,
        cache_write_1h=write_1h,
        output=_int(getattr(outcome, "output_tokens", 0)),
    )


def price_usage(model: str | None, usage: Usage) -> PriceQuote:
    """Price ``usage`` at Anthropic list rates, with 5m and 1h writes kept apart.

    Rates come from :mod:`horizon.pricing.counterfactual` with billing
    semantics. The 1h write rate is the catalog's when it publishes one, and the
    structural 2.00x otherwise. ``basis`` is the resolver's basis string. A model
    the catalog does not know is ``unpriced`` with a ``None`` cost, never zero.
    Never raises.
    """
    if usage.is_zero():
        return PriceQuote(0.0, dict(_ZERO_BREAKDOWN), BASIS_NONE)
    if not model:
        return PriceQuote(None, None, BASIS_UNPRICED)
    try:
        from horizon.pricing.counterfactual import (
            is_long_context_for,
            output_rate_per_token,
            resolve_rates,
        )

        prompt_tokens = usage.input + usage.cache_read + usage.cache_write_5m + usage.cache_write_1h
        long_context = is_long_context_for(model, prompt_tokens)
        rates = resolve_rates(model, long_context=long_context, for_billing=True)
        output_rate = output_rate_per_token(model, long_context=long_context)
    except Exception:  # noqa: BLE001 - pricing must never fail a request
        logger.debug("gateway usage: pricing lookup failed for %s", model, exc_info=True)
        return PriceQuote(None, None, BASIS_UNPRICED)
    if rates is None or output_rate is None:
        return PriceQuote(None, None, BASIS_UNPRICED)

    parts = {
        "input": usage.input * rates.uncached,
        "cache_read": usage.cache_read * rates.read,
        "cache_write": usage.cache_write_5m * rates.write_5m
        + usage.cache_write_1h * rates.write_1h,
        "output": usage.output * output_rate,
    }
    total = sum(parts.values())
    return PriceQuote(
        cost_usd=round(total, 10),
        breakdown={key: round(value, 10) for key, value in parts.items()},
        basis=rates.basis,
    )


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- #
# Sink                                                                         #
# --------------------------------------------------------------------------- #


def sink_path() -> Path:
    """``gateway_usage.jsonl`` in the workspace (``HORIZON_WORKSPACE_DIR`` or ``~/.horizon``)."""
    return workspace_dir() / SINK_FILENAME


def build_line(
    *,
    ts: str | None = None,
    request_id: str | None,
    status: int,
    stream: bool,
    provider: str | None,
    model: str | None,
    client: str | None,
    session: str | None,
    turn_id: str | None,
    usage: Usage,
    usage_source: str,
    quote: PriceQuote,
    passthrough: bool,
    transforms: Iterable[str] = (),
    auth: str = AUTH_OTHER,
) -> dict[str, Any]:
    """One sink line. Field order is the order on disk. No headers, no credentials."""
    return {
        "v": SCHEMA_VERSION,
        "ts": ts or datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "request_id": request_id,
        "status": int(status),
        "stream": bool(stream),
        "provider": provider,
        "model": model,
        "client": client,
        "auth": auth,
        "session": session,
        "turn_id": turn_id,
        "usage": {
            "input": usage.input,
            "cache_read": usage.cache_read,
            "cache_write_5m": usage.cache_write_5m,
            "cache_write_1h": usage.cache_write_1h,
            "output": usage.output,
        },
        "usage_source": usage_source,
        "cost_usd": quote.cost_usd,
        "cost_breakdown_usd": quote.breakdown,
        "pricing_basis": quote.basis,
        "passthrough": bool(passthrough),
        "transforms": list(transforms),
    }


def append_line(line: Mapping[str, Any], path: Path | None = None) -> bool:
    """Append one JSON line. Returns ``False`` instead of raising."""
    try:
        target = path or sink_path()
        payload = json.dumps(line, separators=(",", ":"), default=str) + "\n"
        with _write_lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
        return True
    except Exception:  # noqa: BLE001 - metering must never fail a request
        logger.debug("gateway usage: sink write failed", exc_info=True)
        return False


# --------------------------------------------------------------------------- #
# Per-request scope                                                            #
# --------------------------------------------------------------------------- #


@dataclass
class _RequestRecord:
    headers: dict[str, str]
    passthrough: Callable[[], bool]
    session: str | None = None
    auth: str = AUTH_OTHER
    stream: bool = False
    model: str | None = None
    status: int | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    wrote_usage: bool = False
    body_pending: bool = True


_current: contextvars.ContextVar[_RequestRecord | None] = contextvars.ContextVar(
    "horizon_gateway_usage_request", default=None
)


def _lower_headers(scope: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in scope.get("headers") or []:
        try:
            out[key.decode("latin-1").lower()] = value.decode("latin-1")
        except Exception:  # noqa: BLE001 - malformed header pair; skip it
            continue
    return out


def _absorb_request_body(record: _RequestRecord, raw: bytes) -> None:
    """Keep the request's ``stream`` flag, ``model`` and session. Nothing else."""
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return
    if not isinstance(parsed, dict):
        return
    record.stream = parsed.get("stream") is True
    model = parsed.get("model")
    if isinstance(model, str) and 0 < len(model) <= 200:
        record.model = model
    if record.session is None:
        record.session = resolve_session({}, parsed)


class GatewayUsageMiddleware:
    """Pure ASGI middleware that scopes one usage record per ``/v1/messages`` POST.

    ``passthrough`` is a zero-argument callable that reports whether pass-through
    mode is on. ``create_app`` passes ``getattr(config, "passthrough", False)``.
    """

    def __init__(self, app: Any, *, passthrough: Callable[[], bool] | None = None) -> None:
        self.app = app
        self._passthrough = passthrough or (lambda: False)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or not str(scope.get("path", "")).endswith(MESSAGES_PATH_SUFFIX)
        ):
            await self.app(scope, receive, send)
            return

        headers = _lower_headers(scope)
        record = _RequestRecord(
            headers=headers, passthrough=self._passthrough, auth=auth_class(headers)
        )
        record.session = session_from_header(headers.get(SESSION_HEADER))
        buffered = bytearray()

        async def _receive() -> Any:
            message = await receive()
            if message.get("type") == "http.request" and record.body_pending:
                chunk = bytes(message.get("body", b"") or b"")
                if len(buffered) + len(chunk) > _MAX_PARSED_BODY_BYTES:
                    record.body_pending = False
                    buffered.clear()
                else:
                    buffered.extend(chunk)
                    if not message.get("more_body", False):
                        record.body_pending = False
                        _absorb_request_body(record, bytes(buffered))
                        buffered.clear()
            return message

        async def _send(message: Any) -> None:
            if message.get("type") == "http.response.start" and record.status is None:
                try:
                    record.status = int(message.get("status") or 0) or None
                except (TypeError, ValueError):
                    record.status = None
            await send(message)

        token = _current.set(record)
        try:
            await self.app(scope, _receive, _send)
        finally:
            try:
                _current.reset(token)
            except ValueError:  # pragma: no cover - reset from a different context
                pass
            _write_fallback(record)


def _write_fallback(record: _RequestRecord) -> None:
    """Zero-usage line for a request whose outcome was never recorded."""
    with record.lock:
        if record.wrote_usage:
            return
        record.wrote_usage = True
    try:
        from horizon.proxy.auth_mode import classify_client

        client = classify_client(record.headers)
    except Exception:  # noqa: BLE001
        client = None
    append_line(
        build_line(
            request_id=None,
            status=record.status or 500,
            stream=record.stream,
            provider=None,
            model=record.model,
            client=client,
            session=record.session,
            turn_id=None,
            usage=Usage(),
            usage_source="none",
            quote=PriceQuote(0.0, dict(_ZERO_BREAKDOWN), BASIS_NONE),
            passthrough=_safe_passthrough(record),
            auth=record.auth,
        )
    )


def _safe_passthrough(record: _RequestRecord) -> bool:
    try:
        return bool(record.passthrough())
    except Exception:  # noqa: BLE001
        return False


def _write_outcome(record: _RequestRecord, outcome: Any) -> None:
    with record.lock:
        if record.wrote_usage:
            return
        record.wrote_usage = True
    usage = usage_from_outcome(outcome)
    quote = price_usage(getattr(outcome, "model", None), usage)
    # OAuth (seat or usage plan) cost is still the list-price figure, but it is
    # an estimate: the plan may bill differently. Unpriced stays unpriced.
    if record.auth == AUTH_OAUTH and quote.cost_usd is not None and quote.basis != BASIS_NONE:
        quote = PriceQuote(quote.cost_usd, quote.breakdown, BASIS_LIST_ESTIMATE)
    if getattr(outcome, "from_response_cache", False):
        usage_source = "response_cache"
    else:
        usage_source = "none" if usage.is_zero() else "provider"
    client = getattr(outcome, "client", None)
    if not client:
        try:
            from horizon.proxy.auth_mode import classify_client

            client = classify_client(record.headers)
        except Exception:  # noqa: BLE001
            client = None
    transforms = [str(item) for item in (getattr(outcome, "transforms_applied", None) or ())]
    append_line(
        build_line(
            request_id=getattr(outcome, "request_id", None),
            status=_int(getattr(outcome, "status_code", 200)) or 200,
            stream=record.stream,
            provider=getattr(outcome, "provider", None),
            model=getattr(outcome, "model", None) or record.model,
            client=client,
            session=record.session,
            turn_id=getattr(outcome, "turn_id", None),
            usage=usage,
            usage_source=usage_source,
            quote=quote,
            passthrough=_safe_passthrough(record),
            transforms=transforms,
            auth=record.auth,
        )
    )


async def emit_outcome(outcome: Any) -> None:
    """Outcome-funnel hook: write the line for the current ``/v1/messages`` request.

    A no-op for every other route, because only ``/v1/messages`` binds a
    record. Pricing and the file append run in a worker thread so the event loop
    never waits on them. Never raises.
    """
    record = _current.get()
    if record is None:
        return
    try:
        await asyncio.to_thread(_write_outcome, record, outcome)
    except Exception:  # noqa: BLE001
        logger.debug("gateway usage: outcome write failed", exc_info=True)


# --------------------------------------------------------------------------- #
# Report                                                                       #
# --------------------------------------------------------------------------- #


def read_lines(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Return the valid v1 lines in ``path`` and the number of lines skipped."""
    rows: list[dict[str, Any]] = []
    skipped = 0
    if not path.exists():
        return rows, 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            text = raw.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except ValueError:
                skipped += 1
                continue
            if not isinstance(row, dict) or row.get("v") != SCHEMA_VERSION:
                skipped += 1
                continue
            rows.append(row)
    return rows, skipped


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _new_group() -> dict[str, Any]:
    return {
        "first": None,
        "last": None,
        "requests": 0,
        "tokens": dict.fromkeys(_TOKEN_KEYS, 0),
        "cost_usd": 0.0,
        "cost_by_class_usd": dict.fromkeys(_COST_KEYS, 0.0),
        "unpriced_requests": 0,
    }


def _add_row(group: dict[str, Any], row: dict[str, Any], ts: datetime | None) -> None:
    group["requests"] += 1
    if ts is not None:
        if group["first"] is None or ts < group["first"]:
            group["first"] = ts
        if group["last"] is None or ts > group["last"]:
            group["last"] = ts
    usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
    for key in _TOKEN_KEYS:
        group["tokens"][key] += _int(usage.get(key))
    cost = row.get("cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        group["cost_usd"] += float(cost)
        breakdown = row.get("cost_breakdown_usd")
        if isinstance(breakdown, dict):
            for key in _COST_KEYS:
                value = breakdown.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    group["cost_by_class_usd"][key] += float(value)
    elif row.get("pricing_basis") != BASIS_NONE:
        group["unpriced_requests"] += 1


def _finish_group(group: dict[str, Any]) -> dict[str, Any]:
    tokens = group["tokens"]
    write = tokens["cache_write_5m"] + tokens["cache_write_1h"]
    denominator = tokens["input"] + tokens["cache_read"] + write
    return {
        "first": group["first"].isoformat() if group["first"] else None,
        "last": group["last"].isoformat() if group["last"] else None,
        "requests": group["requests"],
        "tokens": {
            **tokens,
            "cache_write": write,
        },
        "cache_hit_rate": round(tokens["cache_read"] / denominator, 4) if denominator else None,
        "cost_usd": round(group["cost_usd"], 6),
        "cost_by_class_usd": {k: round(v, 6) for k, v in group["cost_by_class_usd"].items()},
        "unpriced_requests": group["unpriced_requests"],
    }


def build_report(
    path: Path,
    *,
    since: datetime | None = None,
    session: str | None = None,
) -> dict[str, Any]:
    """Aggregate the sink per session with grand totals. Pure: reads ``path`` only."""
    rows, skipped = read_lines(path)
    groups: dict[str | None, dict[str, Any]] = {}
    totals = _new_group()
    considered = 0
    for row in rows:
        ts = _parse_ts(row.get("ts"))
        if since is not None and (ts is None or ts < since):
            continue
        row_session = row.get("session") if isinstance(row.get("session"), str) else None
        if session is not None and row_session != session:
            continue
        considered += 1
        group = groups.setdefault(row_session, _new_group())
        _add_row(group, row, ts)
        _add_row(totals, row, ts)

    sessions = []
    for key, group in groups.items():
        summary = _finish_group(group)
        sessions.append({"session": key, **summary})
    sessions.sort(key=lambda item: (item["first"] or "", item["session"] or ""))
    return {
        "path": str(path),
        "lines": considered,
        "skipped": skipped,
        "filters": {
            "since": since.isoformat() if since else None,
            "session": session,
        },
        "sessions": sessions,
        "totals": _finish_group(totals),
    }


def _money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.4f}"


def render_report(report: dict[str, Any]) -> str:
    """Human-readable text for :func:`build_report`."""
    lines: list[str] = []
    if report["lines"] == 0:
        lines.append(f"No gateway usage recorded at {report['path']}")
        if report["skipped"]:
            lines.append(f"({report['skipped']} unreadable line(s) skipped)")
        return "\n".join(lines)

    def block(label: str, item: dict[str, Any]) -> list[str]:
        tokens = item["tokens"]
        rate = item["cache_hit_rate"]
        rate_text = "n/a" if rate is None else f"{rate * 100:.1f}%"
        cost = item["cost_by_class_usd"]
        out = [
            f"{label}",
            f"  span      {item['first'] or '-'}  ..  {item['last'] or '-'}",
            f"  requests  {item['requests']:,}   cache hit rate {rate_text}",
            (
                f"  tokens    input {tokens['input']:,}  cache read {tokens['cache_read']:,}  "
                f"cache write {tokens['cache_write']:,} "
                f"(5m {tokens['cache_write_5m']:,} / 1h {tokens['cache_write_1h']:,})  "
                f"output {tokens['output']:,}"
            ),
            (
                f"  cost      {_money(item['cost_usd'])}  = input {_money(cost['input'])}"
                f" + cache read {_money(cost['cache_read'])}"
                f" + cache write {_money(cost['cache_write'])}"
                f" + output {_money(cost['output'])}"
            ),
        ]
        if item["unpriced_requests"]:
            out.append(f"  unpriced  {item['unpriced_requests']} request(s) excluded from cost")
        return out

    for session_item in report["sessions"]:
        name = session_item["session"] or "(no session id)"
        lines.extend(block(f"session {name}", session_item))
        lines.append("")
    lines.extend(block("TOTAL", report["totals"]))
    if report["skipped"]:
        lines.append(f"({report['skipped']} unreadable line(s) skipped)")
    return "\n".join(lines)


__all__ = [
    "BASIS_NONE",
    "BASIS_UNPRICED",
    "GatewayUsageMiddleware",
    "MESSAGES_PATH_SUFFIX",
    "PriceQuote",
    "SESSION_HEADER",
    "SINK_FILENAME",
    "Usage",
    "AUTH_API_KEY",
    "AUTH_OAUTH",
    "AUTH_OTHER",
    "BASIS_LIST_ESTIMATE",
    "append_line",
    "auth_class",
    "build_line",
    "build_report",
    "emit_outcome",
    "price_usage",
    "read_lines",
    "render_report",
    "resolve_session",
    "session_from_header",
    "session_from_user_id",
    "sink_path",
    "usage_from_outcome",
]
