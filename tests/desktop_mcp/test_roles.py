from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sys
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from horizon.desktop_mcp import broker as broker_module
from horizon.desktop_mcp import broker_transport as transport
from horizon.desktop_mcp.artifacts import ArtifactStore, Dependency
from horizon.desktop_mcp.backend_worker import Runtime
from horizon.desktop_mcp.broker import (
    BrokerClient,
    BrokerError,
    BrokerService,
    CompanionBrokerClient,
    stop_broker,
)
from horizon.desktop_mcp.broker_transport import (
    ARTIFACT_CHUNK_BYTES,
    COMPANION_OPERATIONS,
    BrokerTransportError,
    current_user_sid,
    decode_frame,
    pipe_name_for,
)
from horizon.desktop_mcp.local_backend import LocalBackend
from horizon.desktop_mcp.roles import (
    capability_path,
    issue_capability,
    load_capability,
    revoke_capability,
)
from horizon.desktop_mcp.settings import load_settings
from horizon.desktop_mcp.tables import ResultTable

SID = "S-1-5-21-1000-2000-3000-1001"
ARTIFACT_ID = "0123456789abcdef0123456789abcdef"
OTHER_ID = "f" * 32
SHA_ZERO = "0" * 64


def _settings(tmp_path: Path, **updates: Any) -> Any:
    config = tmp_path / "settings.json"
    data: dict[str, Any] = {
        "profile": "local",
        "broker_id": f"t-{uuid.uuid4().hex[:12]}",
        **updates,
    }
    config.write_text(json.dumps(data), encoding="utf-8")
    return load_settings(config)


def _caps(tmp_path: Path) -> Path:
    return tmp_path / "caps"


def _frame(request: dict[str, Any]) -> bytes:
    payload = json.dumps(request, separators=(",", ":")).encode("utf-8")
    return b"CSM\x01" + len(payload).to_bytes(4, "big") + payload


def _request(operation: str, args: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {"operation": operation, "args": args or {}, "request_id": "req-1", **extra}


# --- capability lifecycle ---------------------------------------------------


def test_issue_then_load_returns_the_same_urlsafe_token(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    token = issue_capability(settings, base_dir=_caps(tmp_path))
    assert len(token) == 43
    assert transport.is_valid_capability(token)
    assert load_capability(settings, base_dir=_caps(tmp_path)) == token


def test_capability_path_hides_broker_id_and_depends_on_it(tmp_path: Path) -> None:
    first = _settings(tmp_path)
    second = _settings(tmp_path)
    path = capability_path(first, base_dir=_caps(tmp_path))
    assert path.parent == _caps(tmp_path)
    assert first.broker_id not in path.name
    assert path != capability_path(second, base_dir=_caps(tmp_path))


def test_load_missing_capability_is_unavailable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with pytest.raises(BrokerError) as caught:
        load_capability(settings, base_dir=_caps(tmp_path))
    assert caught.value.code == "companion_capability_unavailable"


@pytest.mark.parametrize("contents", [b"short\n", b"a" * 43 + b"+", b"a" * 200, b""])
def test_load_rejects_malformed_capability_files(tmp_path: Path, contents: bytes) -> None:
    settings = _settings(tmp_path)
    path = capability_path(settings, base_dir=_caps(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    with pytest.raises(BrokerError) as caught:
        load_capability(settings, base_dir=_caps(tmp_path))
    assert caught.value.code == "companion_capability_unavailable"


def test_revoke_deletes_only_when_contents_match(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    token = issue_capability(settings, base_dir=_caps(tmp_path))
    wrong = secrets.token_urlsafe(32)
    revoke_capability(settings, wrong, base_dir=_caps(tmp_path))
    assert load_capability(settings, base_dir=_caps(tmp_path)) == token

    revoke_capability(settings, token, base_dir=_caps(tmp_path))
    assert not capability_path(settings, base_dir=_caps(tmp_path)).exists()
    with pytest.raises(BrokerError):
        load_capability(settings, base_dir=_caps(tmp_path))


def test_revoke_missing_file_is_a_noop(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    revoke_capability(settings, secrets.token_urlsafe(32), base_dir=_caps(tmp_path))
    revoke_capability(settings, "not-a-capability", base_dir=_caps(tmp_path))


def test_stale_capability_is_replaced_and_old_token_cannot_revoke_new_one(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    stale = issue_capability(settings, base_dir=_caps(tmp_path))
    fresh = issue_capability(settings, base_dir=_caps(tmp_path))
    assert fresh != stale
    assert load_capability(settings, base_dir=_caps(tmp_path)) == fresh
    revoke_capability(settings, stale, base_dir=_caps(tmp_path))
    assert load_capability(settings, base_dir=_caps(tmp_path)) == fresh


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_posix_capability_file_and_directory_are_owner_only(tmp_path: Path) -> None:
    import os
    import stat

    settings = _settings(tmp_path)
    issue_capability(settings, base_dir=_caps(tmp_path))
    path = capability_path(settings, base_dir=_caps(tmp_path))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DACL")
def test_windows_capability_dacl_grants_only_user_and_system(tmp_path: Path) -> None:
    import win32security

    settings = _settings(tmp_path)
    issue_capability(settings, base_dir=_caps(tmp_path))
    path = capability_path(settings, base_dir=_caps(tmp_path))
    descriptor = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
    dacl = descriptor.GetSecurityDescriptorDacl()
    sids = {
        win32security.ConvertSidToStringSid(dacl.GetAce(index)[2])
        for index in range(dacl.GetAceCount())
    }
    assert dacl.GetAceCount() == 2
    assert sids == {current_user_sid(), "S-1-5-18"}
    control, _revision = descriptor.GetSecurityDescriptorControl()
    assert control & 0x1000, "DACL must be protected from inheritance"


# --- transport envelope -----------------------------------------------------


def test_frame_with_capability_decodes_and_frame_without_still_decodes() -> None:
    token = secrets.token_urlsafe(32)
    with_cap = decode_frame(
        json.dumps(
            {"operation": "artifact_list", "args": {}, "request_id": "r", "capability": token}
        ).encode()
    )
    assert with_cap["capability"] == token
    without = decode_frame(b'{"operation":"stats","args":{},"request_id":"r"}')
    assert "capability" not in without


@pytest.mark.parametrize(
    "capability",
    ["short", "a" * 42 + "+", "a" * 42 + "/", "a" * 44, 12345, None, ["a" * 43]],
)
def test_frame_with_malformed_capability_is_rejected(capability: Any) -> None:
    frame = json.dumps(
        {"operation": "artifact_list", "args": {}, "request_id": "r", "capability": capability}
    ).encode()
    with pytest.raises(BrokerTransportError) as caught:
        decode_frame(frame)
    assert caught.value.code == "invalid_request"


@pytest.mark.parametrize("with_capability", [False, True])
def test_caller_asserted_role_is_still_rejected(with_capability: bool) -> None:
    body: dict[str, Any] = {"operation": "stats", "args": {}, "request_id": "r", "role": "admin"}
    if with_capability:
        body["capability"] = secrets.token_urlsafe(32)
    with pytest.raises(BrokerTransportError) as caught:
        decode_frame(json.dumps(body).encode())
    assert caught.value.code == "invalid_request"


def test_companion_operations_are_exactly_the_three_artifact_operations() -> None:
    assert COMPANION_OPERATIONS == frozenset({"artifact_list", "artifact_read", "artifact_delete"})


@pytest.mark.parametrize(
    ("operation", "args"),
    [
        ("artifact_list", {"extra": 1}),
        ("artifact_read", {"artifact_id": "G" * 32, "offset": 0, "length": 1}),
        ("artifact_read", {"artifact_id": ARTIFACT_ID, "offset": -1, "length": 1}),
        ("artifact_read", {"artifact_id": ARTIFACT_ID, "offset": True, "length": 1}),
        ("artifact_read", {"artifact_id": ARTIFACT_ID, "offset": 0, "length": 0}),
        ("artifact_read", {"artifact_id": ARTIFACT_ID, "offset": 0, "length": True}),
        (
            "artifact_read",
            {"artifact_id": ARTIFACT_ID, "offset": 0, "length": ARTIFACT_CHUNK_BYTES + 1},
        ),
        ("artifact_read", {"artifact_id": ARTIFACT_ID, "offset": 0}),
        ("artifact_delete", {"artifact_id": "../x"}),
    ],
)
def test_artifact_arguments_are_closed_and_bounded(operation: str, args: dict[str, Any]) -> None:
    with pytest.raises(BrokerTransportError) as caught:
        transport._validate_operation_args(operation, args)
    assert caught.value.code == "invalid_arguments"


def test_artifact_arguments_accept_the_bounds() -> None:
    transport._validate_operation_args("artifact_list", {})
    transport._validate_operation_args(
        "artifact_read",
        {"artifact_id": ARTIFACT_ID, "offset": 0, "length": ARTIFACT_CHUNK_BYTES},
    )
    transport._validate_operation_args("artifact_delete", {"artifact_id": ARTIFACT_ID})


# --- request-level enforcement (no pipe needed) -----------------------------


class _FakePipe:
    def __init__(self, frame: bytes) -> None:
        # The trailing ACK is what the client sends after reading the response.
        self.incoming = bytearray(frame + b"ACK\x01")
        self.written = bytearray()


def _fake_read_exact(pipe: _FakePipe, size: int, _deadline: float) -> bytes:
    if len(pipe.incoming) < size:
        raise BrokerTransportError("pipe_closed")
    data = bytes(pipe.incoming[:size])
    del pipe.incoming[:size]
    return data


def _serve(
    monkeypatch: pytest.MonkeyPatch, request: dict[str, Any], companion: str | None
) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
    monkeypatch.setattr(transport, "_read_exact", _fake_read_exact)
    monkeypatch.setattr(transport, "_client_sid", lambda _pipe: SID)
    monkeypatch.setattr(transport, "_await_client_close", lambda _pipe, _timeout: None)
    monkeypatch.setattr(
        transport, "_write_all", lambda pipe, data, _deadline: pipe.written.extend(data)
    )
    calls: list[tuple[str, dict[str, Any]]] = []

    def handler(operation: str, args: dict[str, Any]) -> dict[str, Any]:
        calls.append((operation, args))
        return {"handled": operation}

    pipe = _FakePipe(_frame(request))
    transport._serve_request(pipe, handler, SID, 5, companion)
    body = bytes(pipe.written)
    assert body[:4] == b"CSM\x01"
    size = int.from_bytes(body[4:8], "big")
    response = json.loads(body[8 : 8 + size].decode("utf-8"))
    assert response["request_id"] == "req-1"
    return response["result"], calls


CAPABILITY = secrets.token_urlsafe(32)
WRONG_CAPABILITY = secrets.token_urlsafe(32)
LIST_ARGS: dict[str, Any] = {}
READ_ARGS = {"artifact_id": ARTIFACT_ID, "offset": 0, "length": 16}


def test_companion_operation_without_capability_is_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, calls = _serve(monkeypatch, _request("artifact_list", LIST_ARGS), CAPABILITY)
    assert result == {"error": {"code": "operation_unauthorized"}}
    assert calls == []


def test_companion_operation_with_wrong_capability_is_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request("artifact_read", READ_ARGS, capability=WRONG_CAPABILITY)
    result, calls = _serve(monkeypatch, request, CAPABILITY)
    assert result == {"error": {"code": "operation_unauthorized"}}
    assert calls == []


def test_companion_operation_is_unauthorized_when_broker_has_no_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request("artifact_delete", {"artifact_id": ARTIFACT_ID}, capability=CAPABILITY)
    result, calls = _serve(monkeypatch, request, None)
    assert result == {"error": {"code": "operation_unauthorized"}}
    assert calls == []


def test_capability_on_a_non_companion_operation_is_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, calls = _serve(monkeypatch, _request("stats", {}, capability=CAPABILITY), CAPABILITY)
    assert result == {"error": {"code": "operation_unauthorized"}}
    assert calls == []


def test_companion_operation_with_correct_capability_reaches_the_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request("artifact_read", READ_ARGS, capability=CAPABILITY)
    result, calls = _serve(monkeypatch, request, CAPABILITY)
    assert result == {"handled": "artifact_read"}
    assert calls == [("artifact_read", READ_ARGS)]


def test_non_companion_operation_without_capability_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, calls = _serve(monkeypatch, _request("stats", {}), CAPABILITY)
    assert result == {"handled": "stats"}
    assert calls == [("stats", {})]


def test_unknown_operation_is_unavailable_before_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _calls = _serve(monkeypatch, _request("delete", {}, capability=CAPABILITY), CAPABILITY)
    assert result == {"error": {"code": "operation_unavailable"}}


# --- client side ------------------------------------------------------------


class _FakeArtifactServer:
    """Answers artifact_read one chunk at a time; ``mutate`` can corrupt a reply."""

    def __init__(
        self, data: bytes, mutate: Callable[[int, dict[str, Any]], None] | None = None
    ) -> None:
        self.data = data
        self.mutate = mutate
        self.lengths: list[int] = []

    def __call__(self, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        assert operation == "artifact_read"
        self.lengths.append(args["length"])
        offset = args["offset"]
        chunk = self.data[offset : offset + args["length"]]
        payload = {
            "artifact_id": args["artifact_id"],
            "offset": offset,
            "data_base64": base64.b64encode(chunk).decode("ascii"),
            "total_bytes": len(self.data),
            "sha256": hashlib.sha256(self.data).hexdigest(),
            "eof": offset + len(chunk) >= len(self.data),
        }
        if self.mutate is not None:
            self.mutate(len(self.lengths), payload)
        return payload


def _companion_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: Callable[..., Any]
) -> CompanionBrokerClient:
    settings = _settings(tmp_path)
    monkeypatch.setattr(broker_module, "current_user_sid", lambda: SID)
    client = CompanionBrokerClient(settings, base_dir=_caps(tmp_path))
    monkeypatch.setattr(client, "_request", server)
    return client


MULTI_CHUNK = bytes(range(256)) * 4000  # 1,024,000 bytes: three 384 KiB reads


def test_read_artifact_reassembles_every_chunk_and_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _FakeArtifactServer(MULTI_CHUNK)
    client = _companion_client(tmp_path, monkeypatch, server)
    assert client.read_artifact(ARTIFACT_ID) == MULTI_CHUNK
    assert server.lengths == [ARTIFACT_CHUNK_BYTES] * 3


def test_read_empty_artifact_returns_no_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _FakeArtifactServer(b"")
    client = _companion_client(tmp_path, monkeypatch, server)
    assert client.read_artifact(ARTIFACT_ID) == b""


def _mutated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate: Callable[[int, dict[str, Any]], None]
) -> None:
    client = _companion_client(tmp_path, monkeypatch, _FakeArtifactServer(MULTI_CHUNK, mutate))
    with pytest.raises(BrokerError) as caught:
        client.read_artifact(ARTIFACT_ID)
    assert caught.value.code == "artifact_unavailable"


def test_sha_change_between_chunks_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def mutate(call: int, payload: dict[str, Any]) -> None:
        if call == 2:
            payload["sha256"] = SHA_ZERO

    _mutated(tmp_path, monkeypatch, mutate)


def test_total_change_between_chunks_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def mutate(call: int, payload: dict[str, Any]) -> None:
        if call == 2:
            payload["total_bytes"] += 1

    _mutated(tmp_path, monkeypatch, mutate)


def test_altered_bytes_fail_the_final_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def mutate(call: int, payload: dict[str, Any]) -> None:
        if call == 2:
            flipped = bytearray(base64.b64decode(payload["data_base64"]))
            flipped[0] ^= 1
            payload["data_base64"] = base64.b64encode(bytes(flipped)).decode("ascii")

    _mutated(tmp_path, monkeypatch, mutate)


def test_early_eof_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def mutate(call: int, payload: dict[str, Any]) -> None:
        if call == 1:
            payload["eof"] = True

    _mutated(tmp_path, monkeypatch, mutate)


def test_wrong_offset_or_bad_base64_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def bad_id(_call: int, payload: dict[str, Any]) -> None:
        payload["artifact_id"] = OTHER_ID

    def bad_b64(_call: int, payload: dict[str, Any]) -> None:
        payload["data_base64"] = "!!!"

    _mutated(tmp_path, monkeypatch, bad_id)
    _mutated(tmp_path, monkeypatch, bad_b64)


def test_read_artifact_rejects_malformed_ids_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _FakeArtifactServer(b"x")
    client = _companion_client(tmp_path, monkeypatch, server)
    with pytest.raises(BrokerError) as caught:
        client.read_artifact("../escape")
    assert caught.value.code == "invalid_arguments"
    assert server.lengths == []


def test_list_and_delete_validate_reply_shapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _companion_client(tmp_path, monkeypatch, lambda op, args: {"artifacts": "nope"})
    with pytest.raises(BrokerError) as listed:
        client.list_artifacts()
    assert listed.value.code == "artifact_unavailable"

    client = _companion_client(tmp_path, monkeypatch, lambda op, args: {"deleted": "yes"})
    with pytest.raises(BrokerError) as deleted:
        client.delete_artifact(ARTIFACT_ID)
    assert deleted.value.code == "artifact_unavailable"


def test_companion_loads_the_current_capability_per_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str | None] = []

    def fake_exchange(
        _pipe: str,
        _operation: str,
        _args: dict[str, Any],
        timeout_seconds: float = 10,
        capability: str | None = None,
    ) -> dict[str, Any]:
        seen.append(capability)
        return {"artifacts": []}

    monkeypatch.setattr(broker_module, "exchange", fake_exchange)
    settings = _settings(tmp_path)
    monkeypatch.setattr(broker_module, "current_user_sid", lambda: SID)
    client = CompanionBrokerClient(settings, base_dir=_caps(tmp_path))

    first = issue_capability(settings, base_dir=_caps(tmp_path))
    assert client.list_artifacts() == []
    second = issue_capability(settings, base_dir=_caps(tmp_path))  # broker restart
    client.list_artifacts()
    assert seen == [first, second]


def test_basic_broker_client_never_sends_a_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []

    def fake_exchange(
        _pipe: str, operation: str, _args: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        seen.append({"operation": operation, **kwargs})
        return {"stats": True}

    monkeypatch.setattr(broker_module, "exchange", fake_exchange)
    settings = _settings(tmp_path)
    issue_capability(settings, base_dir=_caps(tmp_path))
    BrokerClient(settings).stats()
    assert seen and "capability" not in seen[0]


# --- local backend and worker dispatch --------------------------------------


def _store_backend(tmp_path: Path) -> tuple[LocalBackend, Any]:
    backend = LocalBackend(object())  # type: ignore[arg-type]  # artifact ops never read sources
    root = tmp_path / "artifacts"
    root.mkdir()
    backend.artifact_store = ArtifactStore(root, dependency_check=lambda _deps: True)
    manifest = backend.artifact_store.create(
        "csv",
        ResultTable(("region", "total"), (("north", "4"), ("south", "2")), ()),
        title="Totals",
        dependencies=(Dependency("cs1_example", "v1", "root"),),
    )
    return backend, manifest


def test_backend_without_store_lists_empty_and_unknowns_otherwise() -> None:
    backend = LocalBackend(object())  # type: ignore[arg-type]
    try:
        assert backend.artifact_list() == {"artifacts": []}
        for call in (
            lambda: backend.artifact_read(ARTIFACT_ID, 0, 4),
            lambda: backend.artifact_delete(ARTIFACT_ID),
        ):
            with pytest.raises(Exception) as caught:
                call()
            assert caught.value.code == "artifact_unknown"
    finally:
        backend.close()


def test_backend_reads_slices_and_reports_whole_artifact_facts(tmp_path: Path) -> None:
    backend, manifest = _store_backend(tmp_path)
    try:
        artifact_id = manifest["artifact_id"]
        full = backend.artifact_store.read(artifact_id)

        first = backend.artifact_read(artifact_id, 0, 4)
        assert base64.b64decode(first["data_base64"]) == full[:4]
        assert first["total_bytes"] == len(full)
        assert first["sha256"] == manifest["sha256"]
        assert first["eof"] is False

        tail = backend.artifact_read(artifact_id, 4, len(full))
        assert base64.b64decode(tail["data_base64"]) == full[4:]
        assert tail["eof"] is True

        at_end = backend.artifact_read(artifact_id, len(full), 10)
        assert at_end["data_base64"] == "" and at_end["eof"] is True

        with pytest.raises(Exception) as beyond:
            backend.artifact_read(artifact_id, len(full) + 1, 10)
        assert beyond.value.code == "invalid_arguments"
        with pytest.raises(Exception) as boolean:
            backend.artifact_read(artifact_id, True, 10)  # type: ignore[arg-type]
        assert boolean.value.code == "invalid_arguments"
    finally:
        backend.close()


def test_backend_lists_then_deletes_and_forgets_an_artifact(tmp_path: Path) -> None:
    backend, manifest = _store_backend(tmp_path)
    try:
        listed = backend.artifact_list()["artifacts"]
        assert [item["artifact_id"] for item in listed] == [manifest["artifact_id"]]
        assert backend.artifact_delete(manifest["artifact_id"]) == {"deleted": True}
        assert backend.artifact_list() == {"artifacts": []}
        with pytest.raises(Exception) as gone:
            backend.artifact_read(manifest["artifact_id"], 0, 4)
        assert gone.value.code == "artifact_unknown"
    finally:
        backend.close()


def test_worker_dispatches_artifact_operations_after_argument_checks(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    config = tmp_path / "settings.json"
    runtime = Runtime(
        config,
        expected_broker_id=settings.broker_id,
        expected_max_clients=settings.max_broker_clients,
        expected_timeout=settings.broker_timeout_seconds,
    )

    class _Recorder:
        calls: list[tuple[str, tuple[Any, ...]]] = []

        def artifact_list(self) -> dict[str, Any]:
            self.calls.append(("list", ()))
            return {"artifacts": []}

        def artifact_read(self, artifact_id: str, offset: int, length: int) -> dict[str, Any]:
            self.calls.append(("read", (artifact_id, offset, length)))
            return {"artifact_id": artifact_id}

        def artifact_delete(self, artifact_id: str) -> dict[str, Any]:
            self.calls.append(("delete", (artifact_id,)))
            return {"deleted": True}

        def stats(self) -> dict[str, Any]:
            return {}

    recorder = _Recorder()
    runtime.backend = recorder  # type: ignore[assignment]
    assert runtime.request("artifact_list", {}) == {"artifacts": []}
    runtime.request("artifact_read", {"artifact_id": ARTIFACT_ID, "offset": 2, "length": 3})
    assert runtime.request("artifact_delete", {"artifact_id": ARTIFACT_ID}) == {"deleted": True}
    assert recorder.calls == [
        ("list", ()),
        ("read", (ARTIFACT_ID, 2, 3)),
        ("delete", (ARTIFACT_ID,)),
    ]
    with pytest.raises(BrokerError) as invalid:
        runtime.request("artifact_read", {"artifact_id": ARTIFACT_ID, "offset": 0, "length": 0})
    assert invalid.value.code == "invalid_request"
    assert len(recorder.calls) == 3


# --- real named pipe end to end (Windows) -----------------------------------


def _wait_for_broker(settings: Any, timeout: float = 15.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while True:
        try:
            BrokerClient(settings).stats()
            return
        except BrokerError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named pipe broker")
def test_companion_artifact_round_trip_over_named_pipe(tmp_path: Path) -> None:
    approved = tmp_path / "approved"
    approved.mkdir()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    rows = "".join(f"{index},north,{index % 97}.50,xxxxxxxx\n" for index in range(20000))
    (approved / "sales.csv").write_text("id,region,amount,note\n" + rows, encoding="utf-8")
    config = tmp_path / "settings.json"
    broker_id = f"t-{uuid.uuid4().hex[:12]}"
    config.write_text(
        json.dumps(
            {
                "profile": "local",
                "broker_id": broker_id,
                "approved_roots": [str(approved)],
                "tool_profile": "compute",
                "artifact_root": str(artifacts),
                "max_source_bytes": 4 * 1024 * 1024,
                "max_snapshot_bytes": 8 * 1024 * 1024,
                "max_store_bytes": 16 * 1024 * 1024,
            }
        ),
        encoding="utf-8",
    )
    settings = load_settings(config)
    caps = tmp_path / "caps"
    service = BrokerService(config)
    pipe_name = pipe_name_for(current_user_sid(), broker_id)
    stop = threading.Event()
    server = threading.Thread(
        target=service.serve,
        kwargs={"stop_event": stop, "capability_base_dir": caps},
        daemon=True,
    )
    server.start()
    try:
        _wait_for_broker(settings)
        client = BrokerClient(settings)
        source_id = client.search("sales", limit=5)["hits"][0]["source_id"]
        manifest = client.render(source_id, fmt="csv", title="Sales")["artifact"]
        artifact_id = manifest["artifact_id"]
        assert load_capability(settings, base_dir=caps)

        # The MCP adapter has no capability and must be refused.
        with pytest.raises(BrokerError) as denied:
            client._request("artifact_list", {})
        assert denied.value.code == "operation_unauthorized"

        companion = CompanionBrokerClient(settings, base_dir=caps)
        listed = companion.list_artifacts()
        assert [item["artifact_id"] for item in listed] == [artifact_id]

        rendered = next(artifacts.rglob(f"artifact-{artifact_id}.csv")).read_bytes()
        assert len(rendered) > ARTIFACT_CHUNK_BYTES
        assert companion.read_artifact(artifact_id) == rendered

        assert companion.delete_artifact(artifact_id) is True
        assert companion.list_artifacts() == []
        with pytest.raises(BrokerError) as gone:
            companion.read_artifact(artifact_id)
        assert gone.value.code == "artifact_unknown"
    finally:
        stop_broker(pipe_name, stop)
        server.join(15)
    assert not server.is_alive()
    assert not capability_path(settings, base_dir=caps).exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named pipe broker")
def test_second_broker_with_same_id_cannot_replace_the_live_capability(tmp_path: Path) -> None:
    approved = tmp_path / "approved"
    approved.mkdir()
    config = tmp_path / "settings.json"
    broker_id = f"t-{uuid.uuid4().hex[:12]}"
    config.write_text(
        json.dumps({"profile": "local", "broker_id": broker_id, "approved_roots": [str(approved)]}),
        encoding="utf-8",
    )
    settings = load_settings(config)
    caps = tmp_path / "caps"
    pipe_name = pipe_name_for(current_user_sid(), broker_id)
    stop = threading.Event()
    first = threading.Thread(
        target=BrokerService(config).serve,
        kwargs={"stop_event": stop, "capability_base_dir": caps},
        daemon=True,
    )
    first.start()
    try:
        _wait_for_broker(settings)
        live = load_capability(settings, base_dir=caps)

        # An accidental second start fails to own the pipe and must leave the
        # running broker's capability, and so its companion, untouched.
        with pytest.raises(BrokerTransportError) as second:
            BrokerService(config).serve(capability_base_dir=caps)
        assert second.value.code == "pipe_create_failed"
        assert load_capability(settings, base_dir=caps) == live
        assert CompanionBrokerClient(settings, base_dir=caps).list_artifacts() == []
    finally:
        stop_broker(pipe_name, stop)
        first.join(15)
    assert not capability_path(settings, base_dir=caps).exists()
