"""Plan-note verification and the engine's refresh cycle (spec sections 2 and 3)."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from horizon.code_engine import license as lic
from horizon.code_engine.device import MACHINE_ID_ENV, device_hash
from horizon.code_engine.engine import Engine
from horizon.code_engine.license import (
    NoteError,
    read_license,
    verify_note,
    write_license,
)

MACHINE = "test-machine-id"
DH = device_hash(MACHINE)
NOW = 1_800_000_000
DAY = 86400


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@pytest.fixture
def signer(monkeypatch) -> Ed25519PrivateKey:
    """A fresh key pair; the engine is pointed at its public half for this test only."""
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    monkeypatch.setattr(lic, "CODE_ENTITLEMENT_PUBLIC_KEY_B64", base64.b64encode(raw).decode())
    return key


def _note(key: Ed25519PrivateKey, **claims) -> str:
    payload = {
        "v": 1,
        "iss": "contextshrink-code",
        "sub": "user-1",
        "dev": "dev-1",
        "dh": DH,
        "plan": "trial",
        "iat": NOW,
        "exp": NOW + 72 * 3600,
        "until": NOW + 7 * DAY,
    }
    payload.update(claims)
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return f"{_b64url(raw)}.{_b64url(key.sign(raw))}"


# ── Note verification (section 3) ─────────────────────────────────────


def test_good_trial_note_verifies(signer) -> None:
    entitlement = verify_note(_note(signer), device_hash=DH, now=NOW)
    assert entitlement.plan == "trial"
    assert entitlement.valid_at(NOW)


def test_active_note_verifies(signer) -> None:
    assert verify_note(_note(signer, plan="active"), device_hash=DH, now=NOW).plan == "active"


def test_bad_signature_is_refused(signer) -> None:
    other = Ed25519PrivateKey.generate()
    with pytest.raises(NoteError, match="bad signature"):
        verify_note(_note(other), device_hash=DH, now=NOW)


def test_tampered_payload_is_refused(signer) -> None:
    token = _note(signer)
    payload_part, signature_part = token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(payload_part + "=" * (-len(payload_part) % 4)))
    payload["plan"] = "active"
    forged = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    with pytest.raises(NoteError, match="bad signature"):
        verify_note(f"{forged}.{signature_part}", device_hash=DH, now=NOW)


def test_note_for_another_device_is_refused(signer) -> None:
    with pytest.raises(NoteError, match="another device"):
        verify_note(_note(signer, dh="0" * 64), device_hash=DH, now=NOW)


def test_expired_note_is_refused(signer) -> None:
    token = _note(signer)
    with pytest.raises(NoteError, match="expired"):
        verify_note(token, device_hash=DH, now=NOW + 72 * 3600)


def test_note_issued_in_the_future_is_refused(signer) -> None:
    token = _note(signer, iat=NOW + 3600)
    with pytest.raises(NoteError, match="not yet valid"):
        verify_note(token, device_hash=DH, now=NOW)


def test_small_clock_skew_is_tolerated(signer) -> None:
    token = _note(signer, iat=NOW + 200)
    assert verify_note(token, device_hash=DH, now=NOW).plan == "trial"


def test_plan_none_is_refused(signer) -> None:
    with pytest.raises(NoteError, match="no active plan"):
        verify_note(_note(signer, plan="none"), device_hash=DH, now=NOW)


@pytest.mark.parametrize("claim", [{"v": 2}, {"v": True}, {"iss": "someone-else"}])
def test_wrong_version_or_issuer_is_refused(signer, claim) -> None:
    with pytest.raises(NoteError):
        verify_note(_note(signer, **claim), device_hash=DH, now=NOW)


@pytest.mark.parametrize("token", ["", "no-dot", "a.b.c", "!!.!!", "abc.def"])
def test_malformed_tokens_are_refused(signer, token) -> None:
    with pytest.raises(NoteError):
        verify_note(token, device_hash=DH, now=NOW)


# ── The cached note ───────────────────────────────────────────────────


def test_cached_note_is_read_back_while_valid(signer, tmp_path) -> None:
    write_license(tmp_path, _note(signer), fetched_at=NOW)
    assert read_license(tmp_path, device_hash=DH, now=NOW).plan == "trial"


def test_cached_note_is_ignored_once_expired(signer, tmp_path) -> None:
    write_license(tmp_path, _note(signer), fetched_at=NOW)
    assert read_license(tmp_path, device_hash=DH, now=NOW + 72 * 3600) is None


def test_unreadable_license_file_is_ignored(tmp_path) -> None:
    (tmp_path / "license.json").write_text("{not json", encoding="utf-8")
    assert read_license(tmp_path, device_hash=DH, now=NOW) is None


# ── The refresh cycle ─────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("CONTEXTSHRINK_HOME", str(path))
    monkeypatch.setenv(MACHINE_ID_ENV, MACHINE)
    return path


def _write_device(home, *, device_hash_value: str = DH) -> None:
    data = {
        "version": 1,
        "api_url": "https://api.test",
        "device_id": "dev-1",
        "device_key": "cs_dev_test_key",
        "device_hash": device_hash_value,
        "tools": ["claude"],
        "created_at": "2026-10-22T00:00:00+00:00",
    }
    (home / "device.json").write_text(json.dumps(data), encoding="utf-8")


class _Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _engine(home, handler, clock: _Clock) -> Engine:
    return Engine(home, client=httpx.Client(transport=httpx.MockTransport(handler)), clock=clock)


def _entitled(signer, *, plan: str = "trial"):
    token = _note(signer, plan=plan)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token": token, "plan": plan, "until": NOW + 7 * DAY})

    return handler


def test_refresh_opens_the_gate_and_caches_the_note(signer, home) -> None:
    _write_device(home)
    clock = _Clock()
    engine = _engine(home, _entitled(signer), clock)
    assert engine.refresh() == "ok"
    assert engine.allows_compression()
    assert engine.health() == {"ok": True, "engine": engine.health()["engine"], "plan": "trial"}
    assert (home / "license.json").exists()


def test_refresh_sends_the_device_key_and_hash_only_to_the_api(signer, home) -> None:
    _write_device(home)
    seen = {}
    inner = _entitled(signer)

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return inner(request)

    _engine(home, handler, _Clock()).refresh()
    assert seen["url"] == "https://api.test/code/entitlement"
    assert seen["auth"] == "Bearer cs_dev_test_key"
    assert seen["body"] == {"device_hash": DH}


def test_refresh_402_closes_the_gate_and_clears_the_cache(signer, home) -> None:
    _write_device(home)
    clock = _Clock()
    engine = _engine(home, _entitled(signer), clock)
    engine.refresh()
    engine._client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(402, json={"detail": {"code": "plan_required"}})
        )
    )
    assert engine.refresh() == "not_entitled"
    assert not engine.allows_compression()
    assert not (home / "license.json").exists()


def test_refresh_server_error_keeps_the_cached_note(signer, home) -> None:
    _write_device(home)
    clock = _Clock()
    engine = _engine(home, _entitled(signer), clock)
    engine.refresh()
    engine._client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    assert engine.refresh() == "http_503"
    assert engine.allows_compression()


def test_offline_engine_keeps_the_note_until_its_own_expiry(signer, home) -> None:
    _write_device(home)
    clock = _Clock()
    engine = _engine(home, _entitled(signer), clock)
    engine.refresh()

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    engine._client = httpx.Client(transport=httpx.MockTransport(down))
    clock.now = NOW + 24 * 3600
    assert engine.refresh() == "unreachable"
    assert engine.allows_compression()
    clock.now = NOW + 72 * 3600
    assert not engine.allows_compression()


def test_note_that_fails_the_local_check_is_not_adopted(signer, home) -> None:
    _write_device(home)
    clock = _Clock()
    engine = _engine(home, _entitled(signer), clock)
    engine.refresh()
    forged = _note(Ed25519PrivateKey.generate(), plan="active")
    engine._client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"token": forged, "plan": "active"})
        )
    )
    assert engine.refresh() == "invalid_note"
    assert engine.plan() == "trial"


def test_restart_adopts_the_cached_note_without_network(signer, home) -> None:
    _write_device(home)
    write_license(home, _note(signer), fetched_at=NOW)
    engine = _engine(home, lambda r: httpx.Response(500), _Clock())
    engine.load_cached_note()
    assert engine.allows_compression()


def test_device_hash_mismatch_is_refused_without_a_request(signer, home) -> None:
    _write_device(home, device_hash_value="1" * 64)
    calls = []
    engine = _engine(home, lambda r: calls.append(r) or httpx.Response(200), _Clock())
    assert engine.refresh() == "device_mismatch"
    assert calls == []
    assert not engine.allows_compression()


def test_no_device_json_means_passthrough_only(signer, home) -> None:
    calls = []
    engine = _engine(home, lambda r: calls.append(r) or httpx.Response(200), _Clock())
    engine.load_cached_note()
    assert engine.refresh() == "no_device"
    assert calls == []
    assert engine.health() == {"ok": False, "engine": engine.health()["engine"], "plan": None}


def test_engine_still_serves_pass_through_without_cryptography(home, monkeypatch) -> None:
    """Claude Code is pointed at the engine's port: a missing crypto module must not stop it."""
    from horizon.code_engine import engine as engine_module

    _write_device(home)
    monkeypatch.setattr(engine_module, "_license", None)
    calls = []
    engine = _engine(home, lambda r: calls.append(r) or httpx.Response(200), _Clock())
    engine.load_cached_note()
    assert engine.refresh() == "no_cryptography"
    assert calls == []
    assert not engine.allows_compression()
