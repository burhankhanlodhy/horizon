"""`horizon login` / `logout` / `account` and hosted `wrap` (signed-in CLI)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click
import httpx
import pytest
from click.testing import CliRunner

import horizon.vault as vault
from horizon import hosted
from horizon.cli.main import main


class FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def get_password(self, service, name):
        return self.store.get((service, name))

    def delete_password(self, service, name):
        self.store.pop((service, name), None)


class FakeApi:
    """The control-plane endpoints the CLI calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.revoked: set[str] = set()
        self.password = "right"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        token = request.headers.get("authorization", "")
        if path == "/auth/login":
            body = json.loads(request.content)
            if body["password"] != self.password:
                return httpx.Response(401, json={"detail": "bad"})
            return httpx.Response(
                200,
                json={
                    "token": "sess-1",
                    "user": {"id": "u1", "name": "Ada", "email": body["email"], "plan": "pro"},
                },
            )
        if token != "Bearer sess-1":
            return httpx.Response(401, json={"detail": "no session"})
        if path == "/keys" and request.method == "POST":
            body = json.loads(request.content)
            assert body["scopes"] == ["proxy:messages", "proxy:responses"]
            return httpx.Response(200, json={"id": "k1", "key": "hz_device", "name": body["name"]})
        if path == "/keys" and request.method == "GET":
            return httpx.Response(
                200, json=[{"id": "k1", "revoked_at": "x" if "k1" in self.revoked else None}]
            )
        if path == "/keys/k1" and request.method == "DELETE":
            self.revoked.add("k1")
            return httpx.Response(200, json={"ok": True})
        if path == "/auth/logout":
            return httpx.Response(200, json={})
        if path == "/auth/me":
            return httpx.Response(
                200,
                json={
                    "id": "u1",
                    "name": "Ada",
                    "email": "ada@x.dev",
                    "plan": "pro",
                    "subscription_status": "active",
                },
            )
        if path == "/billing/estimate":
            return httpx.Response(
                200,
                json={
                    "plan": "pro",
                    "period_start": "2026-10-01T00:00:00",
                    "period_end": "2026-11-01T00:00:00",
                    "estimated_savings_usd": 84.5,
                    "savings_fee_rate": 0.05,
                    "savings_fee_threshold_usd": 20,
                    "estimated_total_usd": 3.23,
                    "compression": {"plan": "pro", "capped": False},
                },
            )
        if path == "/usage/summary":
            return httpx.Response(
                200,
                json={
                    "totals": {
                        "requests": 1234,
                        "tokens_saved": 456789,
                        "savings_usd": 84.5,
                        "cost_usd": 210.0,
                    }
                },
            )
        return httpx.Response(404, json={"detail": path})


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    keyring = FakeKeyring()
    monkeypatch.setattr(vault, "_load_keyring", lambda: keyring)
    monkeypatch.setenv("HORIZON_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("HORIZON_WORKSPACE_DIR", str(tmp_path / "ws"))
    for name in ("CONTEXTSHRINK_API_KEY", "HORIZON_HOSTED", "HORIZON_VAULT_CREDENTIAL"):
        monkeypatch.delenv(name, raising=False)
    api = FakeApi()
    monkeypatch.setattr(hosted, "_transport", httpx.MockTransport(api))
    monkeypatch.setattr("socket.gethostname", lambda: "box")
    return keyring, api


def _login(runner: CliRunner, password: str = "right", *extra: str):
    import getpass

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(getpass, "getpass", lambda prompt="": password)
        return runner.invoke(main, ["login", "--email", "ada@x.dev", *extra])


def test_login_creates_this_devices_key_in_its_own_entry(env) -> None:
    keyring, api = env
    keyring.set_password("horizon", "remote-api-key", "hz_desktop")  # the desktop app's
    result = _login(CliRunner())
    assert result.exit_code == 0, result.output
    assert "Device key 'CLI: box' created" in result.output
    assert keyring.store[("horizon", "cli-api-key")] == "hz_device"
    assert keyring.store[("horizon", "cli-session")] == "sess-1"
    assert keyring.store[("horizon", "remote-api-key")] == "hz_desktop"  # untouched
    state = hosted.load_state()
    assert (
        state["key_id"] == "k1"
        and state["device"] == "CLI: box"
        and "password" not in json.dumps(state)
    )
    assert hosted.hosted_active()


def test_a_wrong_password_signs_nothing_in(env) -> None:
    keyring, _ = env
    result = _login(CliRunner(), "wrong")
    assert result.exit_code != 0 and "Incorrect email or password" in result.output
    assert not keyring.store and hosted.load_state() is None


def test_logout_revokes_the_key_and_forgets_everything(env) -> None:
    keyring, api = env
    _login(CliRunner())
    result = CliRunner().invoke(main, ["logout"])
    assert result.exit_code == 0 and "revoked" in result.output
    assert ("DELETE", "/keys/k1") in api.calls and ("POST", "/auth/logout") in api.calls
    assert ("horizon", "cli-api-key") not in keyring.store and hosted.load_state() is None
    assert not hosted.hosted_active()


def test_login_force_replaces_the_old_key(env) -> None:
    _, api = env
    _login(CliRunner())
    assert "Already signed in" in _login(CliRunner()).output
    _login(CliRunner(), "right", "--force")
    assert ("DELETE", "/keys/k1") in api.calls


def test_account_shows_plan_savings_fee_and_usage(env) -> None:
    _login(CliRunner())
    out = CliRunner().invoke(main, ["account"]).output
    assert "Plan:        pro (active)" in out
    assert "CLI: box - key active" in out
    assert "Est. savings this cycle: $84.50" in out
    assert "Est. fee this cycle: $3.23 (5% of savings above $20.00)" in out
    assert "1,234 requests, 456,789 tokens saved, $84.50 saved, $210.00 spent" in out


def test_without_a_keyring_the_secrets_go_to_an_owner_only_file(env, monkeypatch) -> None:
    def no_keyring():
        raise vault.VaultError("no usable OS credential-store backend found")

    monkeypatch.setattr(vault, "_load_keyring", no_keyring)
    result = _login(CliRunner())
    assert result.exit_code == 0 and "readable only by you" in result.output
    data = json.loads(vault.fallback_path().read_text())
    assert data == {"cli-api-key": "hz_device", "cli-session": "sess-1"}
    if os.name != "nt":
        assert (vault.fallback_path().stat().st_mode & 0o777) == 0o600
    monkeypatch.setenv("HORIZON_VAULT_CREDENTIAL", "cli-api-key")
    assert vault.get_credential() == "hz_device"


def test_a_ci_key_needs_no_login(env, monkeypatch) -> None:
    monkeypatch.setenv("CONTEXTSHRINK_API_KEY", "hz_ci")
    assert hosted.hosted_active() and vault.get_credential() == "hz_ci"
    monkeypatch.setenv("HORIZON_HOSTED", "0")
    assert not hosted.hosted_active()


# -- hosted wrap -------------------------------------------------------------


def test_signed_in_wraps_default_to_their_relay_port_and_no_local_extras(env, monkeypatch) -> None:
    from horizon.cli import wrap as wrap_mod

    _login(CliRunner())
    seen: dict = {}

    def capture(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(wrap_mod, "_run_codex_wrap", capture)
    CliRunner().invoke(main, ["wrap", "codex"])
    assert seen["port"] == hosted.RELAY_PORT and seen["no_mcp"] is True
    seen.clear()
    CliRunner().invoke(main, ["wrap", "codex", "--port", "9999"])
    assert seen["port"] == 9999  # a flag still wins
    seen.clear()
    CliRunner().invoke(main, ["wrap", "--local", "codex"])
    assert seen["port"] == 8787 and seen["no_mcp"] is False
    assert (
        hosted.wrap_defaults(click.Command("bob", params=[click.Option(["--port"])]))["port"]
        == 18692
    )


def test_ensure_proxy_starts_a_relay_instead_of_a_local_proxy(env, monkeypatch) -> None:
    from horizon.cli import wrap as wrap_mod

    _login(CliRunner())
    started: list[int] = []
    monkeypatch.setattr(hosted, "ensure_relay", lambda port: started.append(port) or "proc")
    monkeypatch.setattr(
        wrap_mod, "_ensure_proxy_unlocked", lambda *a, **k: pytest.fail("local proxy")
    )
    assert wrap_mod._ensure_proxy(18688, False) == ("proc", 18688)
    assert started == [18688]


def test_relay_command_pins_tool_upstreams_and_uses_the_cli_key(env, monkeypatch) -> None:
    assert hosted.relay_command(18692)[-2:] == ["--upstream", "https://api.us-east.bob.ibm.com"]
    assert "--upstream" not in hosted.relay_command(18688)
    assert hosted.relay_command(18688)[:3] == [sys.executable, "-m", "horizon.cli"]
    _login(CliRunner())
    spawned: dict = {}

    class _Proc:
        def poll(self):
            return None

    def fake_popen(cmd, env, **kwargs):
        spawned.update(cmd=cmd, credential=env.get("HORIZON_VAULT_CREDENTIAL"))
        return _Proc()

    listening = iter([False, True])
    monkeypatch.setattr(hosted, "_listening", lambda port: next(listening))
    monkeypatch.setattr(hosted.subprocess, "Popen", fake_popen)
    assert isinstance(hosted.ensure_relay(18689), _Proc)
    assert spawned["credential"] == "cli-api-key"
    assert spawned["cmd"][-2:] == ["--upstream", "https://api.kimi.com/coding/v1"]
