"""What Flash Observations and the price policies saved on one request, in dollars.

Compression savings are priced from the tokens it removed before the request
was counted (:func:`horizon.proxy.savings_tracker.estimate_request_savings_usd`).
The features below change what a request costs without showing up there, so
the account ledger prices them here, from the request's own usage and the
markers its handler appended to ``transforms_applied``:

``flash``
    ``flash_saved:N``: tokens of stubbed tool outputs this request no longer
    carries. Without the flash they would sit in the cached prefix, so they
    price at the model's cache-read rate: what they would have cost on a warm
    turn. The flashed request's own cache reads are not used, because a stub
    can itself break the cache (the OpenAI form edits an earlier item), and
    pricing removed tokens as fresh input there credited about three times
    the measured saving. The exception is an upstream that evidently does not
    cache (:func:`upstream_never_caches`: several sizeable requests for the
    model, none with a cache read): without the flash those tokens would have
    been billed as fresh input too, so they price at the input rate. The turn
    that shows an output in full is not credited, nor a request whose usage
    reports nothing. Tokens are counted on the forwarded (already compressed)
    text, so nothing compression claimed is counted again.
``fast_mode``
    ``fast_mode:dropped:*`` on a model that bills the fast premium: the
    request at fast price minus the same request at standard price.
``flex``
    ``service_tier:flex`` that the upstream served (no 429 fallback to the
    standard tier): standard price minus Flex price. The request's own cost
    is then the Flex price, returned as ``cost_delta``.
``modernize``
    ``modernize:OLD>NEW``: the same usage on the requested model minus on the
    served one. A successor with the newer Claude tokenizer counts about 30%
    more tokens for the same text, so usage is scaled back for a requested
    model that predates it.

The price-cliff guard is not credited here: the extra compression it causes is
already in the compression figure, and whether a request would have crossed
the tier without it cannot be known from the request alone.

Every figure is ``>= 0``, and ``0`` when a price is unknown: a feature is never
credited from a fallback rate. Never raises.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

FLASH_TAG = "flash_saved:"
FAST_TAG = "fast_mode:dropped:"
FLEX_TAG = "service_tier:flex"
MODERNIZE_TAG = "modernize:"

FEATURES = ("flash", "fast_mode", "flex", "modernize")


def flash_tag(tokens: int) -> str:
    return f"{FLASH_TAG}{max(0, int(tokens))}"


def modernize_tag(requested: str, served: str) -> str:
    return f"{MODERNIZE_TAG}{requested}>{served}"


def flash_tokens(transforms: Iterable[Any]) -> int:
    """Tokens the request's ``flash_saved:N`` markers report."""
    total = 0
    for t in transforms or ():
        text = str(t)
        if text.startswith(FLASH_TAG):
            try:
                total += max(0, int(text[len(FLASH_TAG) :]))
            except ValueError:
                continue
    return total


_COUNTS: OrderedDict[tuple[Any, bytes], int] = OrderedDict()
_COUNTS_MAX = 4096
_counts_lock = threading.Lock()


def _count(count_text: Any, text: str) -> int:
    """``count_text(text)``, memoized: a stubbed output recurs on every later turn."""
    # Keyed by the tokenizer itself (two models count differently); holding it
    # keeps its identity from being reused by another object.
    owner = getattr(count_text, "__self__", count_text)
    key = (owner, hashlib.blake2b(text.encode("utf-8", "ignore"), digest_size=16).digest())
    try:
        with _counts_lock:
            if key in _COUNTS:
                _COUNTS.move_to_end(key)
                return _COUNTS[key]
    except TypeError:  # an unhashable tokenizer: count without the memo
        return int(count_text(text))
    value = int(count_text(text))
    with _counts_lock:
        _COUNTS[key] = value
        while len(_COUNTS) > _COUNTS_MAX:
            _COUNTS.popitem(last=False)
    return value


def cleared_tokens(pairs: Iterable[tuple[str, str]], count_text: Any = None) -> int:
    """Tokens a stub saves over the text it replaced, summed over ``pairs``.

    ``pairs`` holds (forwarded text, stub). ``count_text`` is the handler's
    tokenizer; without one, four characters count as a token, which undercounts
    code and logs and so errs low.
    """
    total = 0
    for text, stub in pairs:
        try:
            if count_text is not None:
                total += max(0, _count(count_text, text) - _count(count_text, stub))
                continue
        except Exception:
            logger.debug("flash token count failed; using a character estimate", exc_info=True)
        total += max(0, len(text) - len(stub)) // 4
    return total


#: Prompt size from which a request is evidence about caching: providers cache
#: only prefixes of at least about 1,024 tokens.
_CACHE_EVIDENCE_MIN_PROMPT = 2_048
#: Sizeable requests, none with a cache read, before an upstream counts as not caching.
_NEVER_CACHES_AFTER = 3
_cache_seen: OrderedDict[tuple[str, str], list[int]] = OrderedDict()
_CACHE_SEEN_MAX = 1_024


def observe_cache(outcome: Any) -> None:
    """Record whether a sizeable successful request for this model read the cache."""
    try:
        prompt = (
            outcome.cache_read_tokens + outcome.cache_write_tokens + outcome.uncached_input_tokens
        ) or outcome.provider_input_tokens
        if prompt < _CACHE_EVIDENCE_MIN_PROMPT:
            return
        key = (str(outcome.provider or "").lower(), str(outcome.model or "").lower())
        with _counts_lock:
            seen = _cache_seen.setdefault(key, [0, 0])
            seen[1 if outcome.cache_read_tokens > 0 else 0] += 1
            _cache_seen.move_to_end(key)
            while len(_cache_seen) > _CACHE_SEEN_MAX:
                _cache_seen.popitem(last=False)
    except Exception:  # pragma: no cover - evidence only
        logger.debug("cache observation failed", exc_info=True)


def upstream_never_caches(provider: str, model: str) -> bool:
    """True once several sizeable requests for ``model`` reported no cache read and none did.

    Process-wide and one-way per model: a single read anywhere keeps the
    model on the cache-read floor, so evidence from one account never raises
    another's credit above it while their upstream caches.
    """
    with _counts_lock:
        seen = _cache_seen.get((str(provider or "").lower(), str(model or "").lower()))
    return bool(seen) and seen[1] == 0 and seen[0] >= _NEVER_CACHES_AFTER


def reset_for_tests() -> None:
    with _counts_lock:
        _cache_seen.clear()
        _COUNTS.clear()


@dataclass
class PolicySavings:
    usd: dict[str, float] = field(default_factory=dict)
    # Correction to the request's cost estimate, which assumes standard prices
    # (negative for a request served at the Flex tier).
    cost_delta: float = 0.0

    @property
    def total(self) -> float:
        return sum(self.usd.values())


@dataclass(frozen=True)
class _Usage:
    read: float
    write_5m: float
    write_1h: float
    uncached: float
    output: float

    def scaled(self, k: float) -> _Usage:
        return _Usage(
            *(v * k for v in (self.read, self.write_5m, self.write_1h, self.uncached, self.output))
        )

    @property
    def prompt(self) -> float:
        return self.read + self.write_5m + self.write_1h + self.uncached


def _usage(outcome: Any) -> _Usage:
    from horizon.pricing.counterfactual import CacheMix

    mix = CacheMix.from_usage(
        cache_read_tokens=outcome.cache_read_tokens,
        cache_write_tokens=outcome.cache_write_tokens,
        cache_write_5m_tokens=outcome.cache_write_5m_tokens,
        cache_write_1h_tokens=outcome.cache_write_1h_tokens,
        uncached_input_tokens=outcome.uncached_input_tokens,
        cache_inferred=outcome.cache_inferred,
    ).normalized()
    if mix.billed > 0:
        return _Usage(mix.read, mix.write_5m, mix.write_1h, mix.uncached, outcome.output_tokens)
    # No breakdown reported: the whole prompt bills as fresh input.
    prompt = outcome.provider_input_tokens or outcome.optimized_tokens
    return _Usage(0, 0, 0, max(0, prompt), outcome.output_tokens)


def _catalog(model: str) -> dict[str, Any]:
    from horizon.pricing.counterfactual import _catalog_row

    return _catalog_row(model)


def _cost(
    model: str, usage: _Usage, *, tier: str = "", provider: str | None = None
) -> float | None:
    """``usage`` priced on ``model`` (``tier="_flex"`` for the Flex rates), or ``None``."""
    from horizon.pricing.counterfactual import (
        is_long_context_for,
        long_context_suffix,
        resolve_rates,
    )

    long_context = is_long_context_for(model, int(usage.prompt))
    if not tier:
        rates = resolve_rates(model, long_context=long_context, provider=provider)
        row = _catalog(model)
        out_rate = row.get("output_cost_per_token")
        if long_context:
            out_rate = row.get("output_cost_per_token" + long_context_suffix(model)) or out_rate
        if rates is None or out_rate is None:
            return None
        return (
            usage.read * rates.read
            + usage.write_5m * rates.write_5m
            + usage.write_1h * rates.write_1h
            + usage.uncached * rates.uncached
            + usage.output * float(out_rate)
        )
    row = _catalog(model)
    suffix = long_context_suffix(model) if long_context else ""

    def rate(name: str) -> float | None:
        value = row.get(f"{name}{suffix}{tier}")
        if value is None and suffix:
            return None
        return float(value) if value is not None else None

    inp, read, out = (
        rate("input_cost_per_token"),
        rate("cache_read_input_token_cost"),
        rate("output_cost_per_token"),
    )
    if inp is None or out is None:
        return None
    # A tier without a published cache-read rate bills reads as input; tiers
    # (Flex, OpenAI) carry no write premium, so writes price as input.
    read = inp if read is None else read
    return (
        usage.read * read
        + (usage.write_5m + usage.write_1h + usage.uncached) * inp
        + usage.output * out
    )


def _flash(outcome: Any, tokens: int) -> float:
    from horizon.pricing.counterfactual import CacheMix, is_long_context_for, resolve_rates

    mix = CacheMix.from_usage(
        cache_read_tokens=outcome.cache_read_tokens,
        cache_write_tokens=outcome.cache_write_tokens,
        cache_write_5m_tokens=outcome.cache_write_5m_tokens,
        cache_write_1h_tokens=outcome.cache_write_1h_tokens,
        uncached_input_tokens=outcome.uncached_input_tokens,
        cache_inferred=outcome.cache_inferred,
    )
    if not mix.has_signal():
        return 0.0
    # Long-context tier from the size the request would have had.
    size = (outcome.provider_input_tokens or outcome.optimized_tokens) + tokens
    rates = resolve_rates(
        outcome.model,
        long_context=is_long_context_for(outcome.model, size),
        provider=outcome.provider,
    )
    if rates is None:
        return 0.0
    if upstream_never_caches(outcome.provider, outcome.model):
        return tokens * rates.uncached
    return tokens * rates.read


def _modernize(outcome: Any, usage: _Usage, requested: str) -> float:
    from horizon.proxy.model_modernize import NEW_TOKENIZER_RATIO, uses_new_tokenizer

    served = outcome.model
    actual = _cost(served, usage, provider=outcome.provider)
    if actual is None:
        return 0.0
    k = 1.0
    if uses_new_tokenizer(served) and not uses_new_tokenizer(requested):
        k = 1.0 / NEW_TOKENIZER_RATIO
    before = _cost(requested, usage.scaled(k), provider=outcome.provider)
    return 0.0 if before is None else max(0.0, before - actual)


def price(outcome: Any, *, flex_served: bool = True) -> PolicySavings:
    """Dollar savings per feature for one successful request."""
    result = PolicySavings()
    observe_cache(outcome)
    try:
        transforms = [str(t) for t in (outcome.transforms_applied or ())]
        if not transforms:
            return result
        usage = _usage(outcome)

        tokens = flash_tokens(transforms)
        if tokens:
            result.usd["flash"] = _flash(outcome, tokens)

        if any(t.startswith(FAST_TAG) for t in transforms):
            from horizon.proxy.fast_mode_policy import PRICE_MULTIPLIER
            from horizon.proxy.model_modernize import supports_fast_mode

            if supports_fast_mode(outcome.model):
                standard = _cost(outcome.model, usage, provider=outcome.provider)
                if standard is not None:
                    result.usd["fast_mode"] = standard * (PRICE_MULTIPLIER - 1.0)

        if FLEX_TAG in transforms and flex_served:
            standard = _cost(outcome.model, usage, provider=outcome.provider)
            flex = _cost(outcome.model, usage, tier="_flex", provider=outcome.provider)
            if standard is not None and flex is not None and flex < standard:
                result.usd["flex"] = standard - flex
                result.cost_delta -= standard - flex

        for t in transforms:
            if t.startswith(MODERNIZE_TAG):
                requested = t[len(MODERNIZE_TAG) :].split(">", 1)[0]
                if requested:
                    result.usd["modernize"] = _modernize(outcome, usage, requested)
                break
    except Exception:  # pragma: no cover - a savings figure must never fail a request
        logger.debug("policy savings failed", exc_info=True)
        return PolicySavings()
    result.usd = {k: v for k, v in result.usd.items() if v > 0}
    return result
