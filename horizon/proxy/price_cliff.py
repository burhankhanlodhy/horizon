"""Price-cliff guard: compress harder when a request is about to cross a price tier.

Some models re-price the WHOLE request once its prompt passes a threshold:
Claude Haiku 5.5 costs 5x more above 100k tokens ($0.10 -> $0.50 per MTok
input, the cache and output rates likewise), and the GPT-5.4 / 6.x family about
2x input and 1.5x output above 272k. A 101k-token Haiku 5.5 request costs 5.1x
a 99k one. A growing session that crosses stays across, so every later request
pays the higher tier too (``wiki/plans/2026-10-08-cost-savings-research.md``,
option 3). Opus and Sonnet 4.6+ are flat-rated and never engage the guard.

WHAT IT DOES
    When the projected prompt lands in a band around the model's threshold
    (``HORIZON_PRICE_CLIFF_MARGIN`` below it, default 10%, up to the same
    fraction above it), the compression pipeline for this request runs with
    tighter per-request knobs: a Kompress keep-ratio of at most
    ``HORIZON_PRICE_CLIFF_TARGET_RATIO`` (default 0.30), at most 8 items kept
    per crushed array, and a 10-token eligibility floor. Read protection and
    other accuracy contracts are untouched. Far below the band nothing changes;
    far above it the tier is already paid and compressing harder cannot undo it.

CACHE SAFETY
    In cache mode (the default) only the newest turn is compressed, and the
    proxy replays each compressed turn byte for byte on later requests, so a
    knob change on one request never rewrites cached history. The request that
    crosses a cliff is usually pushed over by one large new tool result, which
    is exactly the content the guard can shrink.

WHERE IT RUNS
    The Claude Messages handler, Chat Completions (any model, e.g. GPT,
    Gemini or Grok through an OpenAI-compatible endpoint) and the Responses
    API over HTTP and WebSocket. On Responses only tool outputs are
    compressed, so the guard tightens their target ratio; a request chained
    with ``previous_response_id`` carries only its increment and is not
    guarded, because the tier depends on context the provider holds.

Opt-in: ``HORIZON_PRICE_CLIFF_GUARD=1``. Never raises.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from horizon.proxy import runtime_env

logger = logging.getLogger(__name__)

ENABLE_ENV = "HORIZON_PRICE_CLIFF_GUARD"
MARGIN_ENV = "HORIZON_PRICE_CLIFF_MARGIN"
TARGET_RATIO_ENV = "HORIZON_PRICE_CLIFF_TARGET_RATIO"

DEFAULT_MARGIN = 0.10
DEFAULT_TARGET_RATIO = 0.30
MAX_ITEMS = 8
MIN_TOKENS = 10


def _float_env(name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(runtime_env.getenv(name, str(default)) or default)
    except ValueError:
        return default
    return min(max(value, lo), hi)


def enabled() -> bool:
    return (runtime_env.getenv(ENABLE_ENV, "") or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class CliffDecision:
    threshold: int
    projected_tokens: int
    kwargs: dict[str, Any]

    @property
    def label(self) -> str:
        return f"price_cliff:{self.threshold // 1000}k"


def guard(model: str, projected_tokens: int, kwargs: dict[str, Any]) -> CliffDecision | None:
    """Tightened pipeline kwargs when ``projected_tokens`` is near ``model``'s cliff."""
    try:
        if not enabled() or projected_tokens <= 0:
            return None
        from horizon.pricing.counterfactual import long_context_threshold

        threshold = long_context_threshold(model)
        if not threshold:
            return None
        margin = _float_env(MARGIN_ENV, DEFAULT_MARGIN, 0.0, 0.5)
        if not threshold * (1 - margin) <= projected_tokens <= threshold * (1 + margin):
            return None
        target = _float_env(TARGET_RATIO_ENV, DEFAULT_TARGET_RATIO, 0.05, 1.0)
        tightened = dict(kwargs)
        current = tightened.get("target_ratio")
        tightened["target_ratio"] = target if current is None else min(float(current), target)
        tightened["max_items_after_crush"] = min(
            int(tightened.get("max_items_after_crush") or MAX_ITEMS), MAX_ITEMS
        )
        tightened["min_tokens_to_compress"] = min(
            int(tightened.get("min_tokens_to_compress") or MIN_TOKENS), MIN_TOKENS
        )
        return CliffDecision(threshold, projected_tokens, tightened)
    except Exception:  # pragma: no cover - never fail a request over a price guard
        logger.debug("price cliff guard failed", exc_info=True)
        return None


def outcome(decision: CliffDecision, tokens_after_messages: int, overhead_tokens: int) -> str:
    """``"avoided"`` / ``"crossed"`` / ``"already_over"`` for logs and labels."""
    after = tokens_after_messages + overhead_tokens
    if decision.projected_tokens > decision.threshold:
        return "avoided" if after <= decision.threshold else "already_over"
    return "kept_below" if after <= decision.threshold else "crossed"
