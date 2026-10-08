"""Serve requests for superseded Claude models on their cheaper successors.

Clients, MCP tools and scripts pin model ids for years. Anthropic's newer
models are both cheaper and cache-read cheaper: Haiku 4.5 costs $1 / $5 per
MTok against Haiku 5.5's $0.10 / $0.50, Sonnet 4.6 $3 / $15 against Sonnet
5.5's $2 / $10, and the 5.5 models read the prompt cache at 0.05x input
instead of 0.1x. Mapping the old id to the new one is the largest per-request
price cut a proxy can make without touching content (see
``wiki/plans/2026-10-08-cost-savings-research.md``, option 2).

WHY THIS IS NOT A ``ModelRouter`` RULE
    ``ModelRouter`` decides per request from the request's size, so a growing
    conversation can cross a rule's token bound and switch models mid-session.
    Every switch is a full prompt-cache miss: the new model has no cache entry,
    so the whole context is written again at 1.25x-2x input. This mapping is a
    pure function of the model id the client sent. A client sends the same id
    on every turn of a conversation, so the whole conversation is served by one
    model and nothing is ever rewritten for the switch.

THE DEFAULT MAP ONLY HOLDS STRICT PRICE CUTS
    Claude 4.7 and later use a tokenizer that produces about 30% more tokens
    for the same text. Moving Sonnet 4.5 / 4.6 to Sonnet 5.5 is still about
    31% cheaper per turn after that, and Haiku 4.5 to Haiku 5.5 about 87%
    cheaper. Opus 4.5 / 4.6 to Opus 5.5 is not a clear cut once the tokenizer
    is counted (output +4%), so it is left out; an operator can add it.

FAST MODE
    Opus 4.6 accepts ``speed: "fast"`` but runs and bills at standard speed;
    Opus 5.5 honours it at 2x price. A mapping from a model that does not
    honour fast mode therefore drops ``speed``, so the client is never moved
    onto a premium it was not paying.

HOLDOUT
    ``HORIZON_MODEL_MODERNIZE_HOLDOUT`` keeps a fraction of conversations on
    the requested model, assigned per conversation (the same stable key the
    output shaper uses), so quality and cost can be compared on real traffic.

Opt-in: ``HORIZON_MODEL_MODERNIZE=1``. ``HORIZON_MODEL_MODERNIZE_MAP`` (JSON
object, ``{"old-prefix": "new-model"}``) replaces the default map. Never
raises: any failure leaves the request on the model the client asked for.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from typing import Any

from horizon.proxy import runtime_env

logger = logging.getLogger(__name__)

ENABLE_ENV = "HORIZON_MODEL_MODERNIZE"
MAP_ENV = "HORIZON_MODEL_MODERNIZE_MAP"
HOLDOUT_ENV = "HORIZON_MODEL_MODERNIZE_HOLDOUT"

#: Superseded model id prefix -> successor. Only strict price cuts (see module
#: docstring). Longest prefix wins, so a specific entry can override a family.
DEFAULT_MAP: dict[str, str] = {
    "claude-haiku-4-5": "claude-haiku-5-5",
    "claude-3-5-haiku": "claude-haiku-5-5",
    "claude-sonnet-4-5": "claude-sonnet-5-5",
    "claude-sonnet-4-6": "claude-sonnet-5-5",
    "claude-sonnet-5": "claude-sonnet-5-5",
    "claude-opus-4-7": "claude-opus-5-5",
    "claude-opus-4-8": "claude-opus-5-5",
    "claude-opus-5": "claude-opus-5-5",
}

#: Models that honour ``speed: "fast"`` (and bill its premium).
FAST_MODE_MODELS: tuple[str, ...] = ("claude-opus-4-8", "claude-opus-5", "claude-opus-5-5")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _matches(model: str, prefix: str) -> bool:
    """``prefix`` names ``model``'s family: exact, or followed by a version suffix.

    ``claude-opus-5`` matches ``claude-opus-5`` and ``claude-opus-5-20260301``
    but not ``claude-opus-5-5``: a two-digit remainder is a newer minor version,
    not a date or snapshot suffix.
    """
    if model == prefix:
        return True
    if not model.startswith(prefix):
        return False
    rest = model[len(prefix) :]
    if rest[0] == "@":  # Vertex-style snapshot
        return True
    if rest[0] != "-":
        return False
    head = rest[1:].split("-", 1)[0]
    # A date (20251001) or a "latest" alias is a snapshot of the same model;
    # a short number (the "5" in "-5") is a different minor version.
    return not (head.isdigit() and len(head) < 4)


def supports_fast_mode(model: str) -> bool:
    return any(_matches(model, p) for p in FAST_MODE_MODELS)


@dataclass(frozen=True)
class ModernizeDecision:
    requested: str
    served: str
    reason: str
    holdout: bool = False

    @property
    def changed(self) -> bool:
        return self.served != self.requested


@dataclass(frozen=True)
class ModernizeConfig:
    enabled: bool = False
    mapping: tuple[tuple[str, str], ...] = ()
    holdout: float = 0.0

    @classmethod
    def from_env(cls) -> ModernizeConfig:
        enabled = _truthy(runtime_env.getenv(ENABLE_ENV))
        mapping: dict[str, str] = dict(DEFAULT_MAP)
        raw = runtime_env.getenv(MAP_ENV)
        if raw:
            try:
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    raise ValueError("must be a JSON object")
                mapping = {str(k): str(v) for k, v in parsed.items() if k and v}
            except (ValueError, TypeError) as exc:
                logger.warning("invalid %s, using the default map: %s", MAP_ENV, exc)
        try:
            holdout = float(runtime_env.getenv(HOLDOUT_ENV, "0") or "0")
        except ValueError:
            holdout = 0.0
        holdout = min(max(holdout, 0.0), 1.0)
        # Longest prefix first, so the most specific entry wins.
        ordered = tuple(sorted(mapping.items(), key=lambda kv: len(kv[0]), reverse=True))
        return cls(enabled=enabled and bool(ordered), mapping=ordered, holdout=holdout)


class ModelModernizer:
    """Maps superseded model ids to their successors, with decision counters."""

    def __init__(self, config: ModernizeConfig | None = None) -> None:
        self.config = config or ModernizeConfig()
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, str, bool], int] = {}

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def successor(self, model: str) -> str | None:
        for prefix, target in self.config.mapping:
            if _matches(model, prefix) and target != model:
                return target
        return None

    def decide(self, model: str, conversation_key: str = "") -> ModernizeDecision:
        if not self.enabled or not model:
            return ModernizeDecision(model, model, "disabled")
        target = self.successor(model)
        if target is None:
            return ModernizeDecision(model, model, "no successor")
        if self.config.holdout > 0.0:
            from horizon.proxy.output_savings_policy import assign_arm

            if assign_arm("modernize:" + conversation_key, self.config.holdout) == "control":
                return ModernizeDecision(model, model, f"holdout ({target})", holdout=True)
        return ModernizeDecision(model, target, f"{model} -> {target}")

    def apply(self, body: dict[str, Any], conversation_key: str = "") -> ModernizeDecision:
        """Rewrite ``body['model']`` in place when a successor applies."""
        model = str(body.get("model") or "")
        try:
            decision = self.decide(model, conversation_key)
        except Exception:  # pragma: no cover - never fail a request over a price cut
            logger.debug("model modernize failed for %s", model, exc_info=True)
            return ModernizeDecision(model, model, "error")
        if decision.changed:
            body["model"] = decision.served
            if body.get("speed") == "fast" and not supports_fast_mode(model):
                # The client was billed standard speed; keep it that way.
                body.pop("speed", None)
        if decision.changed or decision.holdout:
            key = (decision.requested, decision.served, decision.holdout)
            with self._lock:
                self._counts[key] = self._counts.get(key, 0) + 1
        return decision

    def stats(self) -> dict[str, Any] | None:
        """Routing-stats provider payload (``horizon.proxy.routing_stats``)."""
        with self._lock:
            counts = dict(self._counts)
        if not counts:
            return None
        pairs: list[dict[str, Any]] = [
            {
                "requested": requested,
                "served": served,
                "direction": "downgrade" if not holdout else "same",
                "count": n,
                "enforced": 0 if holdout else n,
                "holdout": n if holdout else 0,
            }
            for (requested, served, holdout), n in sorted(counts.items())
        ]
        changed = sum(p["enforced"] for p in pairs)
        return {
            "decisions": sum(counts.values()),
            "downgrades": changed,
            "upgrades": 0,
            "unchanged": sum(p["holdout"] for p in pairs),
            "pairs": pairs,
            "window": "session",
        }
