"""Bundle LiteLLM's price rows into the frozen desktop client.

The client is built without the litellm package (build-client.ps1 excludes it),
so it cannot read ``litellm.model_cost``. This writes the rows the repo's dev
venv prices with to ``horizon/pricing/_model_prices.json``, which PyInstaller
ships through ``--collect-data horizon``; the pricing module reads it only when
litellm is not importable. The file is git-ignored and removed after the build.

    python build_pricing.py write    # prints the number of rows written
    python build_pricing.py remove
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "horizon" / "pricing" / "_model_prices.json"

#: Exact field names the pricing code reads.
_FIELDS = frozenset(
    {
        "input_cost_per_token",
        "output_cost_per_token",
        "cache_read_input_token_cost",
        "cache_creation_input_token_cost",
        "cache_creation_input_token_cost_above_1hr",
        "max_tokens",
        "max_input_tokens",
        "max_output_tokens",
        "supports_vision",
        "supports_function_calling",
        "litellm_provider",
        "mode",
    }
)
#: Tiered rates ("..._above_200k_tokens", "..._above_1hr") are kept by prefix.
_RATE_PREFIXES = (
    "input_cost_per_token_above_",
    "output_cost_per_token_above_",
    "cache_read_input_token_cost_above_",
    "cache_creation_input_token_cost_above_",
)


def _keep(field: str) -> bool:
    return field in _FIELDS or field.startswith(_RATE_PREFIXES)


def rows_from_live_map() -> dict:
    import litellm  # the dev venv's package: the same rows every local price comes from

    rows = {}
    for name, info in litellm.model_cost.items():
        if not isinstance(info, dict) or info.get("input_cost_per_token") is None:
            continue
        rows[name] = {key: value for key, value in info.items() if _keep(key)}
    return rows


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "write":
        rows = rows_from_live_map()
        TARGET.write_text(json.dumps(rows, separators=(",", ":"), sort_keys=True), encoding="utf-8")
        print(len(rows))
    elif action == "remove":
        TARGET.unlink(missing_ok=True)
    else:
        raise SystemExit("usage: build_pricing.py write|remove")


if __name__ == "__main__":
    main()
