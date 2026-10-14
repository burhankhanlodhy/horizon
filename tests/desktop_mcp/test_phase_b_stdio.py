from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_phase_b_smoke_uses_temporary_synthetic_sources_and_shared_broker() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "desktop" / "mcp" / "phase_b_smoke_client.py"

    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )

    assert completed.returncode == 0
    report = json.loads(completed.stdout)
    assert set(report) == {
        "phase",
        "status",
        "runtime",
        "synthetic_fixtures_only",
        "user_configuration_modified",
        "api_key_or_model_used",
        "checks",
    }
    assert report["phase"] == "B"
    assert report["status"] == "passed"
    assert report["synthetic_fixtures_only"] is True
    assert report["user_configuration_modified"] is False
    assert report["api_key_or_model_used"] is False
    assert all(report["checks"].values())
    assert "synthetic payload" not in completed.stdout
    assert "meeting-notes.txt" not in completed.stdout
