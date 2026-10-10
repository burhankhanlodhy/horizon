"""OpenCode sends the per-launch cache keep-alive id on every model request.

The hosted proxy keeps a session's prompt cache warm only while its client
sends ``X-Horizon-Keepalive-Id``. Providers pointed straight at the proxy
(``openai``, ``anthropic``, ``horizon``) carry it as a provider header; the
transport plugin skips loopback URLs and adds it to everything it reroutes.
Verified end to end against opencode 1.18.34.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import horizon.providers.opencode.runtime as oc_runtime
from horizon.providers.opencode.runtime import build_launch_env

_HERE = Path(oc_runtime.__file__).resolve().parent
_BUNDLES = (_HERE / "hook-shim" / "handler.js", _HERE / "_dist" / "entry.opencode.js")


def _providers(env: dict[str, str]) -> dict[str, dict]:
    return json.loads(env["OPENCODE_CONFIG_CONTENT"])["provider"]


def test_every_proxy_provider_carries_the_launch_id() -> None:
    env, _ = build_launch_env(9000, {"HORIZON_KEEPALIVE_ID": "ka-1"}, include_mcp=False)
    providers = _providers(env)
    assert set(providers) == {"anthropic", "openai", "horizon"}
    for name, entry in providers.items():
        assert entry["options"]["headers"] == {"X-Horizon-Keepalive-Id": "ka-1"}, name
        assert entry["options"]["baseURL"].startswith("http://127.0.0.1:9000")
    assert env["HORIZON_KEEPALIVE_ID"] == "ka-1"  # the plugin reads it from here


def test_no_id_means_no_header() -> None:
    env, _ = build_launch_env(9000, {}, include_mcp=False)
    assert all("headers" not in entry["options"] for entry in _providers(env).values())


@pytest.mark.parametrize("bundle", _BUNDLES, ids=lambda p: p.name)
def test_the_plugin_adds_the_id_to_rerouted_requests(bundle: Path, tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    # A stub fetch records what the transport hands it after rerouting.
    runner = tmp_path / "run.mjs"
    runner.write_text(
        f"""
const seen = [];
globalThis.fetch = async (input, init) => {{
  seen.push({{url: String(input), id: new Headers(init?.headers).get("x-horizon-keepalive-id")}});
  return new Response("{{}}");
}};
const mod = await import({json.dumps(bundle.as_uri())});
if (typeof mod.default === "function") await mod.default({{}});
await fetch("https://generativelanguage.googleapis.com/v1beta/models/m:generateContent", {{method: "POST"}});
await fetch("https://api.x.ai/v1/chat/completions", {{headers: {{"X-Horizon-Keepalive-Id": "mine"}}}});
console.log(JSON.stringify(seen));
"""
    )
    env = {
        "PATH": str(Path(node).parent),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "HORIZON_OPENCODE_TRANSPORT_PROXY_URL": "http://127.0.0.1:9000",
        "HORIZON_PROXY_URL": "http://127.0.0.1:9000",
        "HORIZON_KEEPALIVE_ID": "ka-1",
    }
    out = subprocess.run(
        [node, str(runner)], env=env, capture_output=True, text=True, timeout=60, check=True
    )
    seen = json.loads(out.stdout.strip().splitlines()[-1])
    assert seen[0] == {
        "url": "http://127.0.0.1:9000/v1beta/models/m:generateContent",
        "id": "ka-1",
    }
    assert seen[1]["id"] == "mine"  # a header the caller set is kept
