"""`horizon wrap bob`: IBM Bob CLI through the proxy (port of headroom #3801)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import click
import httpx
import pytest
from click.testing import CliRunner

import horizon.cli.wrap as wrap_mod
from horizon.cli.wrap import wrap
from horizon.providers.bob import (
    CHAT_ROUTE,
    DEFAULT_API_URL,
    build_launch_env,
    preflight,
    resolve_origin_passthrough_url,
    strip_origin_passthrough_response_keys,
)
from horizon.providers.route_specs import OPENAI_HANDLER_ROUTES

BASE = DEFAULT_API_URL


def test_env_is_the_bare_proxy_origin_with_project_prefix() -> None:
    env, display = build_launch_env(9000, {}, project="demo")
    # Bob appends /inference/v1/... itself: a /v1 base would double it.
    assert env["BOB_GATEWAY_URL"] == "http://127.0.0.1:9000/p/demo"
    assert display == ["BOB_GATEWAY_URL=http://127.0.0.1:9000/p/demo"]


def test_bobs_chat_path_reaches_the_compressing_handler() -> None:
    routes = {(r.method, r.path): r.handler_name for r in OPENAI_HANDLER_ROUTES}
    assert routes[("POST", CHAT_ROUTE)] == "handle_openai_chat"


def _settings(tmp_path: Path, gateway: str | None) -> Path:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"gatewayUrl": gateway} if gateway else {}), encoding="utf-8")
    return path


def test_preflight_refuses_a_saved_gateway_that_would_bypass_the_proxy(tmp_path) -> None:
    env = {"BOB_GATEWAY_URL": "http://127.0.0.1:8787/p/demo"}
    assert preflight(env, _settings(tmp_path, None)) is None
    assert preflight(env, tmp_path / "missing.json") is None
    # Same proxy, other project prefix: only attribution differs.
    assert preflight(env, _settings(tmp_path, "http://localhost:8787/p/other")) is None
    problem = preflight(env, _settings(tmp_path, "https://api.us-east.bob.ibm.com"))
    assert problem and "bypass the Horizon proxy" in problem


def test_gateway_paths_go_to_the_origin_verbatim() -> None:
    assert (
        resolve_origin_passthrough_url(BASE, "/admin/v1/profile")
        == "https://api.us-east.bob.ibm.com/admin/v1/profile"
    )
    assert (
        resolve_origin_passthrough_url(
            "https://api.eu-de.bob.ibm.com/inference/v1", "/inference/v1/model/info"
        )
        == "https://api.eu-de.bob.ibm.com/inference/v1/model/info"
    )
    assert resolve_origin_passthrough_url(BASE, "/v1/models") is None
    assert resolve_origin_passthrough_url("https://api.openai.com/v1", "/admin/v1/x") is None
    assert resolve_origin_passthrough_url("http://evil.bob.ibm.com", "/admin/v1/x") is None


def test_region_domain_is_stripped_at_every_depth_on_the_profile_only() -> None:
    body = json.dumps(
        {"region_domain": "x", "instances": [{"teams": [{"region_domain": "y", "id": 1}]}]}
    ).encode()
    out = strip_origin_passthrough_response_keys(BASE, "/admin/v1/profile", body)
    assert json.loads(out) == {"instances": [{"teams": [{"id": 1}]}]}
    assert strip_origin_passthrough_response_keys(BASE, "/admin/v1/budget", body) is None
    assert strip_origin_passthrough_response_keys(BASE, "/admin/v1/profile", b"not json") is None
    assert (
        strip_origin_passthrough_response_keys("https://api.openai.com", "/admin/v1/profile", body)
        is None
    )


def test_passthrough_roots_the_profile_at_the_origin_and_strips_region_domain() -> None:
    from horizon.proxy.handlers.openai import OpenAIHandlerMixin

    class _Upstream:
        calls: list[str] = []

        async def request(self, **kwargs):
            self.calls.append(kwargs["url"])
            return httpx.Response(
                200,
                request=httpx.Request(kwargs["method"], kwargs["url"]),
                json={"id": "p1", "region_domain": "us-east.bob.ibm.com"},
                headers={"ETag": '"v1"', "Digest": "SHA-256=x", "X-Request-Id": "req-1"},
            )

    class _ProfileRequest:
        method = "GET"
        headers: dict[str, str] = {}
        url = SimpleNamespace(path="/admin/v1/profile", query="")

        async def body(self) -> bytes:
            return b""

    handler = object.__new__(OpenAIHandlerMixin)
    handler.http_client = _Upstream()
    response = asyncio.run(handler.handle_passthrough(_ProfileRequest(), BASE))

    assert handler.http_client.calls == ["https://api.us-east.bob.ibm.com/admin/v1/profile"]
    assert json.loads(response.body) == {"id": "p1"}
    forwarded = {k.lower() for k in response.headers}
    assert not forwarded & {"etag", "digest"}  # they describe the unfiltered bytes
    assert response.headers["x-request-id"] == "req-1"


def _run_wrap(monkeypatch, tmp_path, *, mode=None, saved_gateway=None):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    if saved_gateway:
        (tmp_path / ".bob" / "settings").mkdir(parents=True)
        (tmp_path / ".bob" / "settings" / "settings.json").write_text(
            json.dumps({"gatewayUrl": saved_gateway}), encoding="utf-8"
        )
    monkeypatch.delenv("HORIZON_MODE", raising=False)
    monkeypatch.delenv("HORIZON_CONTEXT_TOOL", raising=False)
    if mode:
        monkeypatch.setenv("HORIZON_MODE", mode)
    monkeypatch.setattr(wrap_mod, "_resolve_windows_launcher", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(wrap_mod, "_project_name_from_cwd", lambda: "demo")
    captured: dict = {}

    def fake_launch_tool(**kwargs):
        captured.update(kwargs)
        captured["mode"] = wrap_mod.os.environ.get("HORIZON_MODE")
        args, env, display = kwargs["configure_launch"](
            9100, kwargs["args"], kwargs["env"], list(kwargs["env_vars_display"])
        )
        captured["final_env"] = env

    monkeypatch.setattr(wrap_mod, "_launch_tool", fake_launch_tool)
    result = CliRunner().invoke(wrap, ["bob", "--", "run", "fix it"])
    return result, captured


def test_wrap_bob_routes_bob_through_the_proxy_in_token_mode(monkeypatch, tmp_path) -> None:
    result, captured = _run_wrap(monkeypatch, tmp_path)
    assert result.exit_code == 0, result.output
    assert captured["args"] == ("run", "fix it")
    assert captured["openai_api_url"] == DEFAULT_API_URL
    assert captured["mode"] == "token"
    # The proxy fell back to another port: Bob follows it.
    assert captured["final_env"]["BOB_GATEWAY_URL"] == "http://127.0.0.1:9100/p/demo"


def test_an_explicit_mode_wins(monkeypatch, tmp_path) -> None:
    _, captured = _run_wrap(monkeypatch, tmp_path, mode="cache")
    assert captured["mode"] == "cache"


def test_a_saved_gateway_aborts_the_wrap(monkeypatch, tmp_path) -> None:
    result, _ = _run_wrap(monkeypatch, tmp_path, saved_gateway="https://api.us-east.bob.ibm.com")
    assert result.exit_code != 0
    assert "bypass the Horizon proxy" in result.output


@pytest.mark.parametrize(
    "running,requested,warns",
    [("cache", "token", True), ("token", "token", False), ("token", None, False)],
)
def test_reusing_a_proxy_in_another_mode_warns(monkeypatch, running, requested, warns) -> None:
    if requested:
        monkeypatch.setenv("HORIZON_MODE", requested)
    else:
        monkeypatch.delenv("HORIZON_MODE", raising=False)
    monkeypatch.setattr(wrap_mod, "_query_proxy_health", lambda port: {"config": {"mode": running}})
    lines: list[str] = []
    monkeypatch.setattr(click, "echo", lambda msg="", *a, **k: lines.append(str(msg)))
    wrap_mod._warn_proxy_mode_mismatch(8787)
    assert bool(lines) is warns
