"""One switch for the cost policies: ``HORIZON_SAVINGS=off | auto | max``.

Each cost feature keeps its own environment variable. A profile only fills in
the ones the operator left unset (or empty), once, at proxy start-up, so an
explicit setting always wins and ``off`` (the default) changes nothing.

``auto`` turns on what the live runs showed to save money without the user
choosing anything, each protected by an automatic safety net:

* ``HORIZON_CACHE_MISS_WATCH=1``: telemetry only.
* ``HORIZON_FLASH_OBSERVATIONS=1``: Claude, official API only (-22% measured).
* ``HORIZON_FLASH_GEMINI=1``: the same next-turn stubbing on the native Gemini
  API (``generateContent`` / ``streamGenerateContent``, e.g. Gemini CLI).
* ``HORIZON_FLASH_OPENAI=1`` on any host (``HORIZON_FLASH_OPENAI_UPSTREAMS=*``):
  an OpenAI-format stub can only shrink what is billed, the per-model price
  check skips models where it saves nothing, and a rejected request is retried
  unflashed (:mod:`horizon.proxy.flash_guard`).
* ``HORIZON_OPENAI_FLEX_POLICY=headless`` and ``HORIZON_FAST_MODE_POLICY=headless``:
  cheaper tiers only for traffic nobody is waiting on.
* ``HORIZON_PRICE_CLIFF_GUARD=1``: compress harder just below a whole-request
  price tier.

``max`` adds ``HORIZON_MODEL_MODERNIZE=1``, which serves a superseded model id
on its successor. That changes the model the user picked, so ``auto`` leaves it
out.
"""

from __future__ import annotations

import logging
import os
from collections.abc import MutableMapping

logger = logging.getLogger(__name__)

PROFILE_ENV = "HORIZON_SAVINGS"

_AUTO: dict[str, str] = {
    "HORIZON_CACHE_MISS_WATCH": "1",
    "HORIZON_FLASH_OBSERVATIONS": "1",
    "HORIZON_FLASH_OPENAI": "1",
    "HORIZON_FLASH_OPENAI_UPSTREAMS": "*",
    "HORIZON_FLASH_GEMINI": "1",
    "HORIZON_OPENAI_FLEX_POLICY": "headless",
    "HORIZON_FAST_MODE_POLICY": "headless",
    "HORIZON_PRICE_CLIFF_GUARD": "1",
}
PROFILES: dict[str, dict[str, str]] = {
    "off": {},
    "auto": _AUTO,
    "max": {**_AUTO, "HORIZON_MODEL_MODERNIZE": "1"},
}

_applied: dict[str, str] = {}
_profile = "off"


def profile_name(environ: MutableMapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    value = (env.get(PROFILE_ENV) or "off").strip().lower()
    if value not in PROFILES:
        logger.warning("unknown %s=%r; using off", PROFILE_ENV, value)
        return "off"
    return value


def apply_savings_profile(environ: MutableMapping[str, str] | None = None) -> dict[str, str]:
    """Fill unset feature variables from the profile; returns what was set.

    Idempotent: a second call sets nothing new. A variable that is set to a
    non-empty value, including ``0`` or ``off``, is never touched.
    """
    global _profile
    env = os.environ if environ is None else environ
    name = profile_name(env)
    applied: dict[str, str] = {}
    for key, value in PROFILES[name].items():
        if not (env.get(key) or "").strip():
            env[key] = value
            applied[key] = value
    if environ is None:
        _profile = name
        _applied.update(applied)
    if applied:
        logger.info(
            "savings profile %s: %s",
            name,
            ", ".join(f"{k}={v}" for k, v in sorted(applied.items())),
        )
    return applied


def snapshot() -> dict[str, object]:
    """For ``/stats``: the active profile and the defaults it filled in."""
    return {"profile": _profile, "applied": dict(sorted(_applied.items()))}


def reset_for_tests() -> None:
    global _profile
    _profile = "off"
    _applied.clear()
