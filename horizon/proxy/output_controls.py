"""Native output controls, restricted to known protocol/model combinations."""

from __future__ import annotations

import re
from typing import Any

from horizon.proxy import runtime_env

# Legacy Codex models have different verbosity constraints. Unknown providers
# and models keep the instruction-based path rather than receiving a new field.
_GPT_VERBOSITY = re.compile(r"^gpt-[56](?:$|[.-])")


def apply_responses_native_verbosity(body: dict[str, Any], level: int) -> tuple[bool, bool]:
    """Return (handled, changed), preserving explicit client verbosity/format.

    Native control avoids adding a prose directive to every input. An explicit
    client setting is handled without mutation, including medium/high. Stronger
    opt-in levels still need their instruction-based constraints.
    """
    if level < 2 or level > 4:
        return False, False
    if (runtime_env.getenv("HORIZON_OUTPUT_NATIVE_VERBOSITY", "1") or "1").lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return False, False
    model = str(body.get("model") or "").lower()
    if model.startswith("openai/"):
        model = model[len("openai/") :]
    if not _GPT_VERBOSITY.match(model) or "codex" in model:
        return False, False
    text = body.get("text")
    if text is not None and not isinstance(text, dict):
        return False, False
    if isinstance(text, dict) and text.get("verbosity") is not None:
        return True, False
    updated = dict(text or {})
    updated["verbosity"] = "low"
    body["text"] = updated
    return True, True


def apply_legacy_thinking_budget(body: dict[str, Any], cap: int) -> bool:
    """Opt-in, fixed budget for the validated Haiku 4.5 enabled-thinking API.

    Never inject/toggle thinking, touch adaptive models, or clamp max_tokens.
    The startup setting must stay fixed throughout the conversation: changing
    thinking parameters mid-session invalidates provider prompt caches.
    """
    if cap < 1024:
        return False
    model = str(body.get("model") or "")
    if not re.fullmatch(r"claude-haiku-4-5(?:-\d{8})?", model):
        return False
    thinking = body.get("thinking")
    if not isinstance(thinking, dict) or thinking.get("type") != "enabled":
        return False
    budget = thinking.get("budget_tokens")
    ceiling = body.get("max_tokens")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= cap:
        return False
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling <= cap:
        return False
    body["thinking"] = {**thinking, "budget_tokens": cap}
    return True
