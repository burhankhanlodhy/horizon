"""OpenAI Flex tier for traffic nobody is waiting on.

``service_tier: "flex"`` on an ordinary synchronous request is billed at Batch
rates, half of standard for the GPT-5.4 / 5.6 / 6.x families (the bundled
catalog prices it as ``*_flex``), in exchange for slower answers and an
occasional 429 when Flex capacity is short
(``wiki/plans/2026-10-08-cost-savings-research.md``, option 5).

Policies (``HORIZON_OPENAI_FLEX_POLICY``):

``off`` (default)
    Never set a tier.
``headless``
    Set Flex on requests from a headless client (``codex exec``, CI, an
    explicit ``x-horizon-interactive: 0``).
``always``
    Set Flex on every eligible request.

ELIGIBILITY (all must hold)
    * The upstream is the OpenAI API (``api.openai.com``) with an API key. A
      ChatGPT sign-in (Codex's default) goes to the ChatGPT backend, where
      usage is a subscription allowance and service tiers do not apply.
    * The client did not choose a tier itself. A client's own
      ``service_tier`` (``priority``, ``flex``, ``auto``) is never changed.
    * The catalog prices the model at a Flex rate, so models without Flex
      never receive a parameter they would reject.

FALLBACK
    When Horizon added Flex and the upstream answers 429, the forwarders retry
    once immediately at the standard tier (:func:`fallback_body`), so a
    capacity shortfall costs latency, never a failed request. The flag lives in
    a ``ContextVar`` scoped to the request, so it never leaks across requests.

CACHE NOTE
    The decision is fixed per client launch (header and originator do not
    change mid-session), so a session never alternates between tiers.

Headless detection: ``x-horizon-interactive`` (``0`` headless, ``1``
interactive), else the ``originator`` header against
``HORIZON_OPENAI_FLEX_HEADLESS_ORIGINATORS`` (default ``codex_exec``; confirm it
against a wire capture of your Codex version). Never raises.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from horizon.proxy import runtime_env

logger = logging.getLogger(__name__)

POLICY_ENV = "HORIZON_OPENAI_FLEX_POLICY"
ORIGINATORS_ENV = "HORIZON_OPENAI_FLEX_HEADLESS_ORIGINATORS"
POLICIES = ("off", "headless", "always")
DEFAULT_HEADLESS_ORIGINATORS = ("codex_exec",)
OPENAI_API_HOSTS = frozenset({"api.openai.com"})
MUTATION_REASON = "openai_flex"

#: True while the current request carries a Flex tier Horizon added.
_flex_added: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "horizon_flex_added", default=False
)


def policy() -> str:
    value = (runtime_env.getenv(POLICY_ENV, "off") or "off").strip().lower()
    if value not in POLICIES:
        logger.warning("unknown %s=%r; Flex left off", POLICY_ENV, value)
        return "off"
    return value


def _header(headers: Mapping[str, str], name: str) -> str:
    value = headers.get(name)
    if value is None:
        value = {k.lower(): v for k, v in headers.items()}.get(name, "")
    return str(value or "").strip().lower()


def is_headless(headers: Mapping[str, str]) -> bool:
    flag = _header(headers, "x-horizon-interactive")
    if flag in ("0", "false", "no"):
        return True
    if flag in ("1", "true", "yes"):
        return False
    raw = runtime_env.getenv(ORIGINATORS_ENV)
    originators = (
        frozenset(DEFAULT_HEADLESS_ORIGINATORS)
        if raw is None
        else frozenset(p.strip().lower() for p in raw.split(",") if p.strip())
    )
    return _header(headers, "originator") in originators


def model_supports_flex(model: str) -> bool:
    try:
        import litellm

        from horizon.pricing.litellm_pricing import resolve_litellm_model

        info = litellm.model_cost.get(resolve_litellm_model(model), {}) or {}
    except Exception:
        return False
    return info.get("input_cost_per_token_flex") is not None


def _is_openai_api(url: str) -> bool:
    try:
        return (urlparse(url).hostname or "").lower() in OPENAI_API_HOSTS
    except ValueError:
        return False


def apply_flex(
    body: dict[str, Any],
    *,
    url: str,
    headers: Mapping[str, str],
    chatgpt_auth: bool = False,
) -> bool:
    """Set ``service_tier: "flex"`` in place when policy and eligibility allow."""
    _flex_added.set(False)
    try:
        mode = policy()
        if mode == "off" or chatgpt_auth or not _is_openai_api(url):
            return False
        if "service_tier" in body:
            return False
        if mode == "headless" and not is_headless(headers):
            return False
        if not model_supports_flex(str(body.get("model") or "")):
            return False
        body["service_tier"] = "flex"
        _flex_added.set(True)
        return True
    except Exception:  # pragma: no cover - never fail a request over a price tier
        logger.debug("flex policy failed", exc_info=True)
        return False


def fallback_body(body: dict[str, Any], status_code: int) -> dict[str, Any] | None:
    """The standard-tier retry body after a Flex 429, or ``None`` when not applicable.

    One fallback per request: the flag is cleared when the body is returned.
    """
    if status_code != 429 or not _flex_added.get() or body.get("service_tier") != "flex":
        return None
    _flex_added.set(False)
    retry = {k: v for k, v in body.items() if k != "service_tier"}
    logger.info("Flex capacity unavailable (429); retrying at the standard tier")
    return retry
