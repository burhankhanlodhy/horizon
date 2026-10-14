from __future__ import annotations

import hashlib
import http.client
import json
import os
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from horizon.desktop_mcp.companion import CompanionError, PreparationCompanion, serve_companion
from horizon.desktop_mcp.staging_fs import PinnedStagingDirectory, StagingFilesystemError


@pytest.fixture
def running_companion(monkeypatch: pytest.MonkeyPatch) -> dict[str, str | int]:
    ready = threading.Event()
    state: dict[str, str | int] = {}
    launched: list[str] = []

    def on_ready(url: str, token: str) -> None:
        state["url"] = url
        state["token"] = token
        ready.set()

    monkeypatch.setattr(
        "horizon.desktop_mcp.companion.webbrowser.open",
        lambda url, **_kwargs: launched.append(url) or True,
    )

    failures: list[BaseException] = []

    def run() -> None:
        try:
            serve_companion(port=0, on_ready=on_ready, open_browser=True)
        except BaseException as error:  # surfaced in the test thread
            failures.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(5), "companion did not become ready"
    state["thread"] = thread  # type: ignore[assignment]
    state["launch_url"] = launched[0]
    state["failures"] = failures  # type: ignore[assignment]
    yield state

    if thread.is_alive():
        _http(state, "DELETE", "/api/session", origin=True)
        thread.join(timeout=5)
    assert not thread.is_alive(), "companion did not stop after authenticated revocation"
    assert not failures


def _http(
    state: dict[str, str | int],
    method: str,
    path: str,
    *,
    origin: bool | str | None = None,
    token: str | None = None,
    host: str | None = None,
    cookie: str | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    url = urlsplit(str(state["url"]))
    headers = {
        "Host": host or f"{url.hostname}:{url.port}",
        "Authorization": f"Bearer {token or state['token']}",
    }
    if origin is True:
        headers["Origin"] = f"{url.scheme}://{url.netloc}"
    elif isinstance(origin, str):
        headers["Origin"] = origin
    if cookie is not None:
        headers["Cookie"] = cookie
    if body is not None:
        headers["Content-Type"] = "application/json"
    connection = http.client.HTTPConnection(str(url.hostname), int(url.port), timeout=4)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_browser_capability_is_only_in_fragment_and_http_guards_are_strict(
    running_companion: dict[str, str | int],
) -> None:
    state = running_companion
    launch = urlsplit(str(state["launch_url"]))
    assert launch.fragment == state["token"]
    assert launch.query == ""

    status, headers, body = _http(state, "GET", "/api/session")
    assert status == 200
    assert json.loads(body)["status"] == "ready"
    assert str(state["token"]).encode("ascii") not in body
    assert headers.get("Cache-Control") == "no-store"
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert "Access-Control-Allow-Origin" not in headers
    assert "Set-Cookie" not in headers

    status, _headers, body = _http(
        state,
        "POST",
        "/api/prepare",
        origin=True,
        body=json.dumps({"request": "preserve", "mode": "passthrough"}).encode(),
    )
    assert status == 200
    assert str(state["token"]).encode("ascii") not in body
    assert str(state["token"]) not in json.loads(body)["packet_json"]

    assert _http(state, "GET", "/api/session", token="invalid")[0] in {401, 403}
    assert _http(state, "GET", "/api/session", host="localhost:1")[0] == 421
    assert _http(state, "GET", "/api/session", origin="https://attacker.invalid")[0] in {401, 403}
    assert _http(state, "GET", "/api/session", cookie="session=ambient")[0] == 403
    assert _http(state, "POST", "/api/prepare", body=b"{}", origin=None)[0] in {401, 403}
    assert _http(
        state,
        "POST",
        "/api/prepare",
        body=b"{}",
        origin="https://attacker.invalid",
    )[0] in {401, 403}


def test_http_rejects_oversized_and_ambiguous_request_framing(
    running_companion: dict[str, str | int],
) -> None:
    state = running_companion
    url = urlsplit(str(state["url"]))

    connection = http.client.HTTPConnection(str(url.hostname), int(url.port), timeout=4)
    try:
        connection.putrequest("POST", "/api/prepare", skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", f"{url.hostname}:{url.port}")
        connection.putheader("Authorization", f"Bearer {state['token']}")
        connection.putheader("Origin", f"{url.scheme}://{url.netloc}")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(20 * 1024 * 1024 + 1))
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read())["error"] == "request_too_large"
    finally:
        connection.close()

    body = json.dumps({"request": "preserve", "mode": "passthrough"}).encode()
    for extra_headers in (
        (("Content-Length", str(len(body))), ("Content-Length", str(len(body)))),
        (("Content-Length", str(len(body))), ("Transfer-Encoding", "chunked")),
    ):
        connection = http.client.HTTPConnection(str(url.hostname), int(url.port), timeout=4)
        try:
            connection.putrequest("POST", "/api/prepare", skip_host=True, skip_accept_encoding=True)
            connection.putheader("Host", f"{url.hostname}:{url.port}")
            connection.putheader("Authorization", f"Bearer {state['token']}")
            connection.putheader("Origin", f"{url.scheme}://{url.netloc}")
            connection.putheader("Content-Type", "application/json")
            for name, value in extra_headers:
                connection.putheader(name, value)
            connection.endheaders()
            connection.send(body)
            response = connection.getresponse()
            assert response.status != 200, "ambiguous framing reached preparation"
            response.read()
        finally:
            connection.close()


def _open_staging(tmp_path: Path) -> tuple[PinnedStagingDirectory, Path]:
    approved = tmp_path / "approved"
    approved.mkdir()
    name = ".contextshrink-session-" + "a" * 32
    return PinnedStagingDirectory.open(approved, name), approved


def test_staging_writes_and_cleanup_are_owned_and_bounded(tmp_path: Path) -> None:
    staging, approved = _open_staging(tmp_path)
    data = b"Exact pasted source\r\n"
    filename = "context-" + "b" * 32 + ".txt"
    digest = hashlib.sha256(data).hexdigest()
    identity = staging.write_exclusive(filename, data)
    assert staging.verify_owned(filename, identity)
    staging.pin_owned_file(filename, identity, digest)
    assert staging.verify_owned(filename, identity)
    assert (staging.directory / filename).read_bytes() == data

    assert staging.delete_owned(filename, identity)
    assert staging.close()
    assert not (approved / (".contextshrink-session-" + "a" * 32)).exists()

    second_root = tmp_path / "second"
    second_root.mkdir()
    staging, _approved = _open_staging(second_root)
    with pytest.raises(StagingFilesystemError):
        staging.write_exclusive(filename, b"x" * (8 * 1024 * 1024 + 1))
    assert staging.close()


def test_staging_rejects_hardlink_before_broker_source_can_be_pinned(tmp_path: Path) -> None:
    staging, approved = _open_staging(tmp_path)
    filename = "context-" + "c" * 32 + ".txt"
    data = b"bounded imported source"
    identity = staging.write_exclusive(filename, data)
    outside_link = approved / "user-owned.txt"
    try:
        os.link(staging.directory / filename, outside_link)
    except (OSError, NotImplementedError):
        staging.close()
        pytest.skip("hard links are unavailable on this filesystem")

    with pytest.raises(StagingFilesystemError):
        staging.pin_owned_file(filename, identity, hashlib.sha256(data).hexdigest())
    assert outside_link.read_bytes() == data
    assert staging.delete_owned(filename, identity)
    assert staging.close()
    assert outside_link.read_bytes() == data


def test_busy_windows_source_cleanup_remains_retryable(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows share-mode cleanup behavior")
    staging, _approved = _open_staging(tmp_path)
    filename = "context-" + "f" * 32 + ".txt"
    data = b"busy source cleanup"
    identity = staging.write_exclusive(filename, data)
    staging.pin_owned_file(filename, identity, hashlib.sha256(data).hexdigest())
    blocker = staging._win_create_file(
        str(staging.directory / filename),
        0x80000000,
        0x00000001,
        None,
        3,
        0x00200000,
        None,
    )
    import ctypes.wintypes

    assert blocker != ctypes.wintypes.HANDLE(-1).value
    blocker = int(blocker)
    try:
        assert not staging.delete_owned(filename, identity)
        assert staging._entries[filename] == identity
    finally:
        staging._close_handle(blocker)

    assert staging.delete_owned(filename, identity)
    assert staging.close()


def test_exclusive_collision_never_deletes_an_unowned_file(tmp_path: Path) -> None:
    staging, _approved = _open_staging(tmp_path)
    filename = "context-" + "e" * 32 + ".txt"
    path = staging.directory / filename
    path.write_bytes(b"preexisting user data")

    with pytest.raises(StagingFilesystemError):
        staging.write_exclusive(filename, b"replacement")
    assert path.read_bytes() == b"preexisting user data"
    staging.close()
    assert path.read_bytes() == b"preexisting user data"
    path.unlink()
    staging.directory.rmdir()


def test_staging_directory_redirection_cannot_write_through_replacement(
    tmp_path: Path,
) -> None:
    staging, approved = _open_staging(tmp_path)
    moved = approved / "moved-session"
    outside = tmp_path / "outside"
    outside.mkdir()
    if os.name == "nt":
        with pytest.raises(OSError):
            staging.directory.rename(moved)
        staging.close()
        return
    staging.directory.rename(moved)
    try:
        staging.directory.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        staging.close(remove_directory=False)
        moved.rmdir()
        pytest.skip("directory symlinks are unavailable on this filesystem")

    with pytest.raises(StagingFilesystemError):
        staging.check_directory()
    filename = "context-" + "d" * 32 + ".txt"
    identity = staging.write_exclusive(filename, b"anchored")
    assert not list(outside.iterdir())
    assert (moved / filename).read_bytes() == b"anchored"
    staging.delete_owned(filename, identity)
    staging.close()
    staging.directory.unlink()
    moved.rmdir()


def test_large_single_line_import_and_reference_only_use_overview_without_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approved = tmp_path / "approved"
    approved.mkdir()
    config = tmp_path / "desktop.json"
    config.write_text(
        json.dumps(
            {
                "profile": "local",
                "approved_roots": [str(approved)],
                "max_source_bytes": 4 * 1024 * 1024,
                "max_snapshot_bytes": 4 * 1024 * 1024,
            }
        ),
        encoding="utf-8",
    )
    body = "Sensitive pasted text " + ("x" * (300 * 1024))
    version = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest() + ":text-v1"
    source_id = "cs1_" + uuid.uuid4().hex
    calls: list[tuple[str, str]] = []

    class FakeBrokerClient:
        def __init__(self, _settings: object) -> None:
            pass

        def open(self, source: str, *, mode: str, result_token_budget: int) -> dict[str, object]:
            del result_token_budget
            calls.append((source, mode))
            assert mode == "overview", "this path must not request an exact text page"
            if source == source_id:
                return {
                    "source_id": source_id,
                    "version": version,
                    "kind": "text",
                    "coverage": "overview metadata only",
                    "more_available": True,
                }
            assert Path(source).read_bytes() == body.encode("utf-8")
            return {
                "source_id": source_id,
                "version": version,
                "kind": "text",
                "coverage": "snapshot created",
                "more_available": False,
            }

    monkeypatch.setattr("horizon.desktop_mcp.companion.BrokerClient", FakeBrokerClient)
    companion = PreparationCompanion(config, approved)
    assert companion.vault is not None
    staged = companion.vault.import_text(body, config)
    assert staged["utf8_bytes"] > 256 * 1024
    assert calls[-1][1] == "overview"

    request = "Please check the imported source without embedding it."
    result = companion.prepare(
        {"request": request, "mode": "reference_only", "source_id": source_id}
    )
    packet = json.loads(result["packet_json"])
    context = packet["supporting_context"]
    assert packet["request"] == request
    assert "evidence" not in context and "text" not in context
    assert body not in result["packet_json"]
    assert context["source"]["availability"] == "companion_session"
    assert context["exact_recovery"]["source_id"] == source_id
    assert context["exact_recovery"]["availability"] == "companion_session"
    assert "local companion session" in context["exact_recovery"]["note"]
    assert result["measurements"]["comparison_scope"] == (
        "provided_evidence_only_not_full_source_or_task"
    )
    assert result["measurements"]["local_baseline"] == (
        "provided_evidence_only_not_full_source_or_task"
    )
    assert calls == [
        (str(companion.vault.directory / staged["name"]), "overview"),
        (source_id, "overview"),
    ]
    if os.name == "nt":
        filename = staged["name"]
        blocker = companion.vault._filesystem._win_create_file(
            str(companion.vault.directory / filename),
            0x80000000,
            0x00000001,
            None,
            3,
            0x00200000,
            None,
        )
        import ctypes.wintypes

        assert blocker != ctypes.wintypes.HANDLE(-1).value
        blocker = int(blocker)
        try:
            with pytest.raises(CompanionError) as blocked:
                companion.vault.delete(staged["id"])
            assert blocked.value.code == "staging_unavailable"
            assert [item["id"] for item in companion.vault.list_entries()] == [staged["id"]]
        finally:
            companion.vault._filesystem._close_handle(blocker)
        companion.vault.delete(staged["id"])
        assert companion.vault.list_entries() == []
    assert companion.vault.close()
