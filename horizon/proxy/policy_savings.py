"""What Flash Observations and the price policies saved on one request, in dollars.

Compression savings are priced from the tokens it removed before the request
was counted (:func:`horizon.proxy.savings_tracker.estimate_request_savings_usd`).
The features below change what a request costs without showing up there, so
the account ledger prices them here, from the request's own usage and the
markers its handler appended to ``transforms_applied``:

``flash``
    Tokens of stubbed tool outputs this request no longer carries, at what the
    provider would have billed for them:

    * **Tokens.** ``flash_bytes:N`` (the request-body bytes the stubs removed)
      times the provider's own billed tokens per byte for the model, learned
      from requests flash did not touch (:func:`tokens_per_byte`). No local
      tokenizer matches every provider (one pytest log: 8,452 by characters /
      4, 11,313 by o200k; billed about 16,000 by Gemini, 15,000 by GPT-6.1 Sol,
      19,000 by Claude). Until a model is calibrated, the handler's own count
      (``flash_saved:N``) is used.
    * **Price.** Without the flash these tokens would sit in the prefix the
      conversation already sent, billed as a cache read when that prefix hits
      and as fresh input (or a cache write, where the provider charges one)
      when it misses. The hit rate is the model's measured share of repeated
      prefix served from cache (:func:`prefix_hit_rate`), from later turns of
      conversations no stub edited: a stub can itself break the cache, and
      reading those misses as the provider's caching overcredited flash
      several times over (native Gemini, 2026-10-09). Until a model has that
      evidence, the cache-read rate is used.

    The turn that shows an output in full is not credited, nor a request whose
    usage reports nothing. Bytes and tokens are taken from the forwarded
    (already compressed) text, so nothing compression claimed is counted again.
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
import json
import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

FLASH_TAG = "flash_saved:"
FLASH_BYTES_TAG = "flash_bytes:"
FAST_TAG = "fast_mode:dropped:"
FLEX_TAG = "service_tier:flex"
MODERNIZE_TAG = "modernize:"

FEATURES = ("flash", "fast_mode", "flex", "modernize")


def flash_tag(tokens: int) -> str:
    return f"{FLASH_TAG}{max(0, int(tokens))}"


def flash_bytes_tag(size: int) -> str:
    return f"{FLASH_BYTES_TAG}{max(0, int(size))}"


def modernize_tag(requested: str, served: str) -> str:
    return f"{MODERNIZE_TAG}{requested}>{served}"


def _tagged(transforms: Iterable[Any], prefix: str) -> int:
    total = 0
    for t in transforms or ():
        text = str(t)
        if text.startswith(prefix):
            try:
                total += max(0, int(text[len(prefix) :]))
            except ValueError:
                continue
    return total


def flash_tokens(transforms: Iterable[Any]) -> int:
    """Tokens the request's ``flash_saved:N`` markers report (the handler's count)."""
    return _tagged(transforms, FLASH_TAG)


def flash_bytes(transforms: Iterable[Any]) -> int:
    """Request-body bytes the request's ``flash_bytes:N`` markers report."""
    return _tagged(transforms, FLASH_BYTES_TAG)


def _json_bytes(text: str) -> int:
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8"))


def cleared_bytes(pairs: Iterable[tuple[str, str]]) -> int:
    """Request-body bytes (JSON-encoded, as sent) the stubs save over the texts they replaced."""
    return sum(max(0, _json_bytes(text) - _json_bytes(stub)) for text, stub in pairs)


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


#: Prompt size from which a request is evidence: providers cache only prefixes
#: of at least about 1,024 tokens, and small bodies make a noisy byte ratio.
_MIN_PROMPT = 2_048
#: Observations before a model's measurement is used.
_MIN_OBSERVATIONS = 3
#: Weight kept by older observations on each new one, so a model's figures
#: follow its traffic (about the last fifty requests dominate).
_DECAY = 0.98
_MAX_KEYS = 1_024

#: (provider, model) -> [billed tokens, body bytes, observations]
_density: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
#: (provider, model) -> [prefix tokens read from cache, prefix tokens repeated, observations]
_hits: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
#: conversation -> tokens its next request repeats (this prompt plus the reply)
_previous: OrderedDict[str, int] = OrderedDict()
_STUBBED = re.compile(r"^flash:\d+/[1-9]\d*$")


def _model_key(outcome: Any) -> tuple[str, str]:
    return (str(outcome.provider or "").lower(), str(outcome.model or "").lower())


def _accumulate(table: OrderedDict, key: Any, a: float, b: float) -> None:
    entry = table.setdefault(key, [0.0, 0.0, 0.0])
    entry[0] = entry[0] * _DECAY + a
    entry[1] = entry[1] * _DECAY + b
    entry[2] += 1
    table.move_to_end(key)
    while len(table) > _MAX_KEYS:
        table.popitem(last=False)


def _prompt(outcome: Any) -> int:
    return (
        outcome.cache_read_tokens + outcome.cache_write_tokens + outcome.uncached_input_tokens
    ) or outcome.provider_input_tokens


def observe(outcome: Any, forwarded_bytes: int = 0) -> None:
    """Learn a model's billed tokens per byte and its prefix cache hit rate.

    Bytes: from every request except Claude flash bodies, which carry cleared
    messages the provider does not bill (the OpenAI and Gemini forms bill the
    body as sent). Hit rate: only from requests no stub edited, because a stub
    can break the cache.
    """
    try:
        prompt = _prompt(outcome)
        transforms = [str(t) for t in (outcome.transforms_applied or ())]
        flashed = any(t.startswith("flash:") for t in transforms)
        stubbed = any(_STUBBED.match(t) for t in transforms)
        conversation = str(
            getattr(outcome, "conversation_key", None) or getattr(outcome, "turn_id", None) or ""
        )
        key = _model_key(outcome)
        with _counts_lock:
            previous = _previous.get(conversation, 0) if conversation else 0
            if conversation:
                _previous[conversation] = prompt + int(outcome.output_tokens or 0)
                _previous.move_to_end(conversation)
                while len(_previous) > _MAX_KEYS * 4:
                    _previous.popitem(last=False)
            if prompt < _MIN_PROMPT:
                return
            claude_flash = flashed and key[0] in ("anthropic", "bedrock", "vertex")
            if not claude_flash and forwarded_bytes >= 4 * _MIN_PROMPT:
                _accumulate(_density, key, prompt, forwarded_bytes)
            if not stubbed and previous >= _MIN_PROMPT:
                # The repeated prefix is at most what this request carries.
                repeated = min(previous, prompt)
                _accumulate(_hits, key, min(outcome.cache_read_tokens, repeated), repeated)
    except Exception:  # pragma: no cover - evidence only
        logger.debug("savings evidence failed", exc_info=True)


def _measured(table: OrderedDict, provider: str, model: str) -> float | None:
    with _counts_lock:
        entry = table.get((str(provider or "").lower(), str(model or "").lower()))
    if not entry or entry[2] < _MIN_OBSERVATIONS or entry[1] <= 0:
        return None
    return entry[0] / entry[1]


def tokens_per_byte(provider: str, model: str) -> float | None:
    """The provider's billed input tokens per request-body byte for ``model``."""
    return _measured(_density, provider, model)


def prefix_hit_rate(provider: str, model: str) -> float | None:
    """Share of a conversation's repeated prefix ``model``'s upstream serves from cache."""
    rate = _measured(_hits, provider, model)
    return None if rate is None else min(1.0, max(0.0, rate))


def reset_for_tests() -> None:
    with _counts_lock:
        _density.clear()
        _hits.clear()
        _previous.clear()
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


def flash_billed_tokens(outcome: Any) -> int:
    """Tokens the provider would have billed for what the stubs removed."""
    transforms = outcome.transforms_applied or ()
    size = flash_bytes(transforms)
    density = tokens_per_byte(outcome.provider, outcome.model) if size else None
    if density is not None:
        return int(round(size * density))
    return flash_tokens(transforms)


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
    hit = prefix_hit_rate(outcome.provider, outcome.model)
    if hit is None:
        return tokens * rates.read
    # A missed prefix is written again where the provider charges for writes.
    miss = rates.write_5m if rates.write_5m > rates.uncached else rates.uncached
    return tokens * (hit * rates.read + (1.0 - hit) * miss)


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


def price(outcome: Any, *, flex_served: bool = True, forwarded_bytes: int = 0) -> PolicySavings:
    """Dollar savings per feature for one successful request.

    ``forwarded_bytes``: size of the body this request sent upstream
    (:mod:`horizon.proxy.forwarded_size`), for :func:`tokens_per_byte`.
    """
    result = PolicySavings()
    observe(outcome, forwarded_bytes)
    try:
        transforms = [str(t) for t in (outcome.transforms_applied or ())]
        if not transforms:
            return result
        usage = _usage(outcome)

        if flash_tokens(transforms) or flash_bytes(transforms):
            result.usd["flash"] = _flash(outcome, flash_billed_tokens(outcome))

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
