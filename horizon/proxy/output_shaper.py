"""Request-side controls for output token usage.

Level 2 preserves required content while suppressing repeated context and
unrequested examples. Known OpenAI Responses models use native text.verbosity
without adding input instructions; explicit client verbosity is preserved.
Other protocols use byte-stable, idempotent steering at the system prompt tail.

An opt-in startup budget caps already-enabled Haiku 4.5 thinking. It never
injects thinking, changes adaptive effort, removes history or caps final text.
Keep the budget fixed across a conversation to avoid invalidating prompt caches.
Budget reductions can affect reasoning quality and require workload evaluation.
Dynamic effort routing is not implemented.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import wraps
from typing import Any

from horizon.proxy import runtime_env
from horizon.proxy.output_controls import (
    apply_legacy_thinking_budget,
    apply_responses_native_verbosity,
)
from horizon.proxy.output_steering import (
    apply_openai_chat_verbosity_steering,
    apply_openai_responses_verbosity_steering,
    apply_verbosity_steering,
    replace_or_append_steering_block,
    steering_text,
)
from horizon.proxy.output_turn_policy import (
    TurnKind,
    classify_openai_responses_input,
    classify_turn,
)
from horizon.proxy.symbol_read_plan import apply_symbol_read_plan
from horizon.proxy.tool_json_compaction import apply_tool_json_compaction
from horizon.proxy.validation_feedback import annotate_validation_results, load_inspection_json
from horizon.rollout import FeatureDecisionReason

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_VERBOSITY_LEVEL",
    "OutputShaperSettings",
    "ShapeResult",
    "TurnKind",
    "apply_openai_chat_verbosity_steering",
    "apply_openai_responses_verbosity_steering",
    "apply_verbosity_steering",
    "classify_openai_responses_input",
    "classify_turn",
    "resolve_verbosity_level",
    "shape_openai_chat_request",
    "shape_openai_responses_request",
    "shape_request",
    "steering_text",
]

_replace_or_append_steering_block = replace_or_append_steering_block


#: Default content-preserving prose level. Savings depend on the workload;
#: paired measurements, including failures, live in experiments/output-20.
DEFAULT_VERBOSITY_LEVEL = 2


@dataclass(frozen=True)
class OutputShaperSettings:
    """Output-shaping settings with rollout enablement injected by the proxy."""

    enabled: bool = False
    verbosity_level: int = DEFAULT_VERBOSITY_LEVEL
    # False in cache mode: select a startup-pinned level instead of learned
    # profiles or the live controller, which could rewrite cached instructions.
    steering_enabled: bool = True
    thinking_budget_cap: int = 0
    tool_json_compaction_enabled: bool = False
    symbol_read_plan: str | None = None

    @classmethod
    def from_env(
        cls, *, enabled: bool | None = None, steering_enabled: bool = True
    ) -> OutputShaperSettings:
        """Resolve tuning; running proxies always inject the resolved gate.

        ``None`` preserves the helper's direct-call compatibility for SDK/tests,
        but proxy request paths never use it and therefore never re-resolve the
        rollout alias.
        """
        if enabled is None:
            enabled = runtime_env.getenv("HORIZON_OUTPUT_SHAPER", "").lower() in (
                "1",
                "true",
                "yes",
            )
        try:
            level = int(runtime_env.getenv("HORIZON_VERBOSITY_LEVEL", str(DEFAULT_VERBOSITY_LEVEL)))
        except ValueError:
            level = DEFAULT_VERBOSITY_LEVEL
        level = max(0, min(4, level))
        try:
            # Deliberately not a hot-reload knob: a mid-session budget switch
            # would invalidate the prompt prefix. Select this before startup.
            thinking_cap = int(os.environ.get("HORIZON_OUTPUT_THINKING_BUDGET", "0"))
        except ValueError:
            thinking_cap = 0
        if thinking_cap < 1024:
            thinking_cap = 0
        return cls(
            enabled=enabled,
            verbosity_level=level,
            steering_enabled=steering_enabled,
            thinking_budget_cap=thinking_cap,
            tool_json_compaction_enabled=os.environ.get("HORIZON_OUTPUT_TOOL_JSON", "0").lower()
            in ("1", "true", "yes"),
        )


def shaper_enabled_for(config: Any) -> bool | None:
    """Resolve the output-shaper gate for a proxy config.

    Output shaping is deliberately independent of input compression — an
    operator can run ``optimize=False`` and still want terser responses, and
    the WS/Responses shaper tests pin that combination. So ``optimize`` does
    not veto shaping outright. What it vetoes is shaping that nobody asked
    for.

    Since the feature defaults on, ``optimize=False`` plus *no* explicit
    request means an operator who turned every transform off would silently
    start getting a steering block appended to their system-prompt tail — and
    on a request carrying no ``system`` field at all, would have one created.
    That breaks the byte-faithful forwarding invariant. So in that one
    combination the default loses:

    * enabled explicitly (``HORIZON_OUTPUT_SHAPER=1``, ``HORIZON_FEATURES``)
      → shape, whatever ``optimize`` says;
    * enabled only by default, with ``optimize=False`` → do not shape;
    * enabled only by default, with ``optimize=True`` → shape.

    Returns ``None`` when there is no rollout snapshot to consult, which
    preserves :meth:`OutputShaperSettings.from_env`'s env-var fallback for the
    SDK and test callers that construct a config without one.
    """
    rollout = getattr(config, "rollout", None)
    if rollout is None:
        return None
    try:
        decision = rollout.decision("proxy_output_shaper")
    except (KeyError, AttributeError):
        return None
    if not decision.enabled:
        return False
    if getattr(config, "optimize", True):
        return True
    return decision.reason is not FeatureDecisionReason.DEFAULT


def steering_allowed_for(config: Any) -> bool:
    """False when the proxy is in prefix-freezing cache mode.

    ``mode="cache"`` freezes prior turns specifically to keep the provider's
    prefix-cache key byte-stable (see ``ProxyConfig.mode``). Verbosity steering
    writes into that key, so a block that CHANGES there trades a large, certain
    cache cost for a small, uncertain output saving — the wrong side of a
    roughly 60x margin on a long context. Effort routing and the thinking
    budget are unaffected: they ride request parameters outside the cache key,
    so they keep saving in cache mode.

    This is the gate for the sources that can change mid-conversation. It is
    NOT the last word: :func:`resolve_verbosity_level` lets an explicitly
    pinned ``HORIZON_VERBOSITY_LEVEL`` through even here, because a level
    fixed before startup is written into turn 1's prefix and never moves. See
    that function for the reasoning.
    """
    return getattr(config, "mode", None) != "cache"


#: Keys already warned about. Resolution runs on every request of every
#: conversation, so an unguarded warning would be one log line per turn.
_REPORTED: set[str] = set()


def _report_once(key: str, message: str, *args: Any) -> None:
    """Warn the first time only. Cleared by tests via ``_REPORTED.clear()``."""
    if key in _REPORTED:
        return
    _REPORTED.add(key)
    logger.warning(message, *args)


def _resolve_unpinned_level(settings: OutputShaperSettings) -> tuple[int, str]:
    """Resolve the level from the sources that can move under our feet.

    The controller rewrites its state file as it hunts, and ``verbosity.json``
    appears the moment someone runs ``learn --verbosity``. Both can therefore
    change mid-conversation, which is what makes them unsafe in cache mode.
    """
    try:
        from ..paths import workspace_dir

        ws = workspace_dir()
    except Exception:
        return settings.verbosity_level, "default"

    autotune = runtime_env.getenv("HORIZON_VERBOSITY_AUTOTUNE", "").lower() in ("1", "true", "yes")
    if autotune:
        ctrl_path = ws / "verbosity_controller.json"
        if ctrl_path.exists():
            try:
                import json as _json

                level = int(
                    _json.loads(ctrl_path.read_text()).get("level", settings.verbosity_level)
                )
                return max(0, min(4, level)), "controller"
            except (OSError, ValueError):
                pass

    prof_path = ws / "verbosity.json"
    if prof_path.exists():
        try:
            import json as _json

            level = int(_json.loads(prof_path.read_text()).get("verbosity_level", -1))
            if 0 <= level <= 4:
                return level, "learned"
        except (OSError, ValueError):
            pass

    return settings.verbosity_level, "default"


def resolve_verbosity_level(settings: OutputShaperSettings) -> tuple[int, str]:
    """Resolve the live verbosity level and its source.

    Precedence:
      1. ``HORIZON_VERBOSITY_LEVEL`` set explicitly → manual override.
      2. AIMD controller state (when ``HORIZON_VERBOSITY_AUTOTUNE`` is on).
      3. Learned ``verbosity.json`` from ``learn --verbosity``.
      4. The settings default.

    Returns ``(level, source)``. Kept separate from :func:`shape_request` so the
    body-mutating core stays a pure function of an explicit level.

    Cache mode and the pinned level
    -------------------------------
    ``mode="cache"`` used to force this to ``0`` unconditionally, outranking
    even an explicit ``HORIZON_VERBOSITY_LEVEL``. That guarded the right
    hazard with the wrong instrument.

    What costs a prefix cache is the steering block *changing* — appearing on a
    conversation already in flight, or moving between levels — because the
    block sits in the ``system`` array, which is inside the cached prefix of
    every message-level ``cache_control`` breakpoint the client sets. What does
    NOT cost anything is the block simply *being there*: :func:`shape_request`
    applies it on every request with no turn-kind gating, and the text is
    byte-stable per level and idempotent via the sentinel. So a level pinned in
    the environment before the proxy starts is written into turn 1's prefix and
    is byte-identical on every turn after it. The cache is established
    including the block and hits normally from then on; the only cost is the
    block's own tokens riding along in the cached prefix. Net savings depend
    on the response, provider rates and whether the prefix is already cached.

    What matters is therefore not WHICH level, but whether it can move. Both
    sources that cannot move are allowed through in cache mode:

    * an explicit ``HORIZON_VERBOSITY_LEVEL`` → ``env_pinned``
    * otherwise the settings level, fixed at startup → ``cache_mode_default``

    The second is what makes ``HORIZON_OUTPUT_SHAPER=1`` sufficient on its
    own. Optimized proxies enable the shaper by default; optimize=False retains
    byte-faithful forwarding unless shaping is explicitly enabled.

    The controller and the learned profile stay out of cache mode entirely —
    those are exactly the sources that rewrite themselves while conversations
    are open. Skipping them also spares the default mode two filesystem stats
    per request.
    """
    if runtime_env.getenv("HORIZON_VERBOSITY_LEVEL"):
        return settings.verbosity_level, ("env" if settings.steering_enabled else "env_pinned")

    if not settings.steering_enabled:
        # Cache mode: steer at the settings level selected at startup.
        #
        # The controller and the learned profile are deliberately NOT consulted
        # here. Both rewrite their files while conversations are open, and a
        # level that moves mid-conversation rewrites the system array, which
        # sits inside the cached prefix of every message-level cache_control
        # breakpoint. That is the bust this mode exists to prevent -- and
        # skipping the lookup also spares the default mode two filesystem stats
        # per request.
        if runtime_env.getenv("HORIZON_VERBOSITY_AUTOTUNE", "").lower() in ("1", "true", "yes"):
            _report_once(
                "autotune_in_cache_mode",
                "OutputShaper: HORIZON_VERBOSITY_AUTOTUNE is set but the AIMD controller is "
                "not consulted in cache mode — a level that moves mid-conversation busts the "
                "prefix cache. Steering at L%d. Set HORIZON_MODE=token to autotune.",
                settings.verbosity_level,
            )
        return settings.verbosity_level, "cache_mode_default"

    return _resolve_unpinned_level(settings)


@dataclass
class ShapeResult:
    """What the shaper did to a request body."""

    changed: bool = False
    labels: list[str] | None = None

    def __post_init__(self) -> None:
        if self.labels is None:
            self.labels = []


# Bound staging work; text/image strings are immutable and shared, not copied.
_MAX_STAGING_DEPTH = 64
_MAX_STAGING_NODES = 100_000


def _stage_body(body: dict[str, Any]) -> dict[str, Any]:
    remaining = _MAX_STAGING_NODES

    def clone(value: Any, depth: int) -> Any:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > _MAX_STAGING_DEPTH:
            raise ValueError("output-shaper staging budget exceeded")
        if isinstance(value, dict):
            if len(value) > remaining:
                raise ValueError("output-shaper staging budget exceeded")
            return {key: clone(item, depth + 1) for key, item in value.items()}
        if isinstance(value, list):
            if len(value) > remaining:
                raise ValueError("output-shaper staging budget exceeded")
            return [clone(item, depth + 1) for item in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        raise TypeError("output-shaper requires JSON containers")

    return clone(body, 0)


def _atomic_shaping(shape):
    """Commit optional transforms together, or forward the original request.

    No exception text is logged: parser/transform errors can contain secrets.
    Unchanged top-level fields retain their original objects and cache history.
    """

    @wraps(shape)
    def apply(body, settings=None, level_override=None):
        try:
            settings = settings if settings is not None else OutputShaperSettings.from_env()
            if not settings.enabled:
                return ShapeResult()
            staged = _stage_body(body)
            result = shape(staged, settings, level_override)
            # Compute every comparison before touching the original body.
            updates = {
                key: value for key, value in staged.items() if key not in body or value != body[key]
            }
            removed = body.keys() - staged.keys()
        except Exception as exc:
            logger.warning("OutputShaper: skipped %s (%s)", shape.__name__, type(exc).__name__)
            return ShapeResult()
        for key in removed:
            del body[key]
        body.update(updates)
        return result

    return apply


@_atomic_shaping
def shape_openai_responses_request(
    body: dict[str, Any],
    settings: OutputShaperSettings | None = None,
    level_override: int | None = None,
) -> ShapeResult:
    """Apply OpenAI Responses output-shaping levers in place."""
    if settings is None:
        settings = OutputShaperSettings.from_env()
    result = ShapeResult()
    if not settings.enabled:
        return result

    assert result.labels is not None  # __post_init__ guarantees

    level = settings.verbosity_level if level_override is None else level_override
    _compact_tool_json(body, settings, result)
    _validation_feedback(body, result)
    handled, changed = apply_responses_native_verbosity(body, level)
    if changed:
        result.changed = True
        result.labels.append(f"output_shaper:verbosity:L{level}")
        result.labels.append("output_shaper:native_verbosity:low")
    if (
        level > 0
        and (not handled or level >= 3)
        and apply_openai_responses_verbosity_steering(body, level)
    ):
        result.changed = True
        label = f"output_shaper:verbosity:L{level}"
        if label not in result.labels:
            result.labels.append(label)

    return result


@_atomic_shaping
def shape_request(
    body: dict[str, Any],
    settings: OutputShaperSettings | None = None,
    level_override: int | None = None,
) -> ShapeResult:
    """Apply all output-shaping levers to an Anthropic request body in place.

    ``level_override`` supersedes ``settings.verbosity_level`` when given — the
    handler passes the level resolved by :func:`resolve_verbosity_level` (learned
    profile / controller / env) so the body-mutating core stays level-agnostic.
    """
    if settings is None:
        settings = OutputShaperSettings.from_env()
    result = ShapeResult()
    if not settings.enabled:
        return result

    assert result.labels is not None  # __post_init__ guarantees this

    level = settings.verbosity_level if level_override is None else level_override
    if level > 0 and apply_verbosity_steering(body, level):
        result.changed = True
        result.labels.append(f"output_shaper:verbosity:L{level}")
    _compact_tool_json(body, settings, result)
    _validation_feedback(body, result)
    if apply_legacy_thinking_budget(body, settings.thinking_budget_cap):
        result.changed = True
        result.labels.append(f"output_shaper:thinking_budget:{settings.thinking_budget_cap}")
    if apply_symbol_read_plan(body, settings.symbol_read_plan):
        result.changed = True
        result.labels.append("input_planning:symbol_reads")

    return result


@_atomic_shaping
def shape_openai_chat_request(
    body: dict[str, Any],
    settings: OutputShaperSettings | None = None,
    level_override: int | None = None,
) -> ShapeResult:
    """Apply output-shaping levers to an OpenAI chat/completions body in place.

    The chat counterpart of :func:`shape_request`. Chat carries the system
    prompt as a ``role: "system"`` message, so verbosity steering uses the
    chat-specific injector.
    """
    if settings is None:
        settings = OutputShaperSettings.from_env()
    result = ShapeResult()
    if not settings.enabled:
        return result

    assert result.labels is not None  # __post_init__ guarantees this

    level = settings.verbosity_level if level_override is None else level_override
    if level > 0 and apply_openai_chat_verbosity_steering(body, level):
        result.changed = True
        result.labels.append(f"output_shaper:verbosity:L{level}")
    _compact_tool_json(body, settings, result)
    _validation_feedback(body, result)

    return result


def _validation_feedback(body: dict, result: ShapeResult) -> None:
    if (runtime_env.getenv("HORIZON_VALIDATION_FEEDBACK", "1") or "1").lower() in {
        "0",
        "false",
        "off",
        "no",
    }:
        return
    count = annotate_validation_results(body)
    if count:
        result.changed = True
        result.labels.append(f"validation_feedback:{count}")


def _compact_tool_json(
    body: dict[str, Any], settings: OutputShaperSettings, result: ShapeResult
) -> None:
    if settings.tool_json_compaction_enabled:
        count = apply_tool_json_compaction(body)
        if count:
            result.changed = True
            result.labels.append(f"input_compaction:tool_json:{count}")


# ---------------------------------------------------------------------------
# OpenAI Responses format (Codex, /v1/responses HTTP + WebSocket)
# ---------------------------------------------------------------------------

# Trailing ``input`` item types that represent tool output coming back to the
# model — the Responses counterpart of an Anthropic ``tool_result`` block.
_RESPONSES_TOOL_OUTPUT_TYPES = frozenset(
    {
        "custom_tool_call_output",
        "function_call_output",
        "local_shell_call_output",
        "apply_patch_call_output",
    }
)


def _responses_tool_output_is_error(item: dict[str, Any]) -> bool:
    """Structural error sniff on a Responses tool-output item.

    The Responses format has no ``is_error`` flag, but agent harnesses encode
    failure structurally in the ``output`` payload: a JSON object with a
    nonzero ``exit_code``, ``success: false``, or a truthy ``error`` field.
    Only those JSON fields are inspected — never prose content.
    """
    output = item.get("output")
    data: Any = output
    if isinstance(output, str):
        stripped = output.strip()
        if not (stripped.startswith("{") and stripped.endswith("}")):
            return False
        try:
            data = load_inspection_json(stripped)
        except (ValueError, TypeError):
            return False
    if not isinstance(data, dict):
        return False
    # Direct fields, plus the common {"output": ..., "metadata": {...}} nesting.
    scopes: list[dict[str, Any]] = [data]
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        scopes.append(metadata)
    for scope in scopes:
        exit_code = scope.get("exit_code")
        if isinstance(exit_code, int) and exit_code != 0:
            return True
        if scope.get("success") is False:
            return True
        if scope.get("error"):
            return True
    return False


def classify_responses_turn(input_data: Any) -> TurnKind:
    """Classify a Responses request's turn from its ``input`` field.

    Mirrors :func:`classify_turn` semantics on the Responses item list: the
    trailing run of tool-output items decides the turn. A trailing user
    message is a new ask; tool outputs are mechanical unless any carries a
    structural error marker. Purely structural — item types and JSON fields,
    no content regexes.
    """
    if isinstance(input_data, str):
        return TurnKind.NEW_USER_ASK if input_data.strip() else TurnKind.UNKNOWN
    if not isinstance(input_data, list) or not input_data:
        return TurnKind.UNKNOWN

    saw_tool_output = False
    saw_error = False
    for item in reversed(input_data):
        if not isinstance(item, dict):
            return TurnKind.UNKNOWN
        itype = item.get("type")
        if itype in _RESPONSES_TOOL_OUTPUT_TYPES:
            saw_tool_output = True
            if _responses_tool_output_is_error(item):
                saw_error = True
            continue
        # First non-tool-output item ends the trailing run.
        if saw_tool_output:
            break
        if itype == "message" or (itype is None and "role" in item):
            role = item.get("role")
            if role == "user":
                return TurnKind.NEW_USER_ASK
            return TurnKind.UNKNOWN
        return TurnKind.UNKNOWN

    if saw_error:
        return TurnKind.ERROR_CONTINUATION
    if saw_tool_output:
        return TurnKind.MECHANICAL_CONTINUATION
    return TurnKind.UNKNOWN


def apply_responses_verbosity_steering(body: dict[str, Any], level: int) -> bool:
    """Append the steering block to the tail of ``instructions``.

    ``instructions`` is the Responses cache hot zone: the appended block is
    byte-stable per level, so within a conversation every shaped turn sends
    identical instructions bytes and the provider prefix cache stays hot
    after the first shaped turn (the same contract as the Anthropic
    system-tail append).
    """
    return apply_openai_responses_verbosity_steering(body, level)


def shape_responses_request(
    body: dict[str, Any],
    settings: OutputShaperSettings | None = None,
    level_override: int | None = None,
) -> ShapeResult:
    """Apply all output-shaping levers to a Responses payload in place.

    The Responses counterpart of :func:`shape_request`: same settings, same
    labels, same level-resolution contract.
    """
    return shape_openai_responses_request(body, settings, level_override)
