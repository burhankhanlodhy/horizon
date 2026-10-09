"""Reproduce the recorded targeted matrix with its exact public pricing catalog.

No provider/model calls. The ignored snapshot must be supplied; this runner does
not download a changing catalog or reset any spend ledger.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
CATALOG_SHA256 = "e1160204c0513d78a1b22e7885f6862e0dea31698e206848c333f75d67b95b73"
FILES = [
    "tests/test_proxy/test_compact_edit_pipeline.py",
    "tests/test_proxy/test_compact_edit_cohort.py",
    "tests/test_proxy/test_gemini_payload_research.py",
    "tests/test_anthropic_auto_mode_passthrough.py",
    "tests/test_account_compression_cap.py",
    "tests/test_keepalive_hosted.py",
    "tests/test_proxy_response_cache_replay.py",
    "tests/test_proxy_anthropic_cache_stability.py",
    "tests/test_5xx_accounting_all_providers.py",
    "tests/test_proxy/test_policy_savings.py",
    "tests/test_proxy/test_price_cliff.py",
    "tests/test_transforms/test_flash_gemini.py",
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, default=HERE / "runs/catalog.json")
    args = parser.parse_args()
    data = args.catalog.read_bytes()
    if hashlib.sha256(data).hexdigest() != CATALOG_SHA256:
        raise RuntimeError(
            "catalog differs from the recorded snapshot; review and repin explicitly"
        )
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    import litellm
    import pytest

    litellm.model_cost.update(json.loads(data))
    litellm.add_known_models()
    raise SystemExit(pytest.main(FILES + ["-q", "--disable-warnings"]))


if __name__ == "__main__":
    main()
