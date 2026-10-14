from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

import horizon.desktop_mcp.broker as broker_module
from horizon.desktop_mcp.broker import BrokerError
from horizon.desktop_mcp.companion import (
    CompanionError,
    PreparationCompanion,
    _artifact_file_name,
    serve_companion,
)

ARTIFACT_ID = "0123456789abcdef0123456789abcdef"
OTHER_ID = "fedcba9876543210fedcba9876543210"
PAYLOAD = b'a,b\r\n1,"\xc3\xa9"\n\x00<script>\r\n'


def _manifest(
    artifact_id: str = ARTIFACT_ID,
    *,
    fmt: str = "csv",
    title: Any = "Quarterly report",
) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "format": fmt,
        "title": title,
        "utf8_bytes": len(PAYLOAD),
        "row_count": 1,
        "columns": ["a", "b"],
        "preview_rows": [["1", "é"]],
        "expires_at": "2026-10-14T12:00:00Z",
    }


class FakeArtifacts:
    """In-memory stand-in for CompanionBrokerClient."""

    def __init__(self, manifests: list[dict[str, Any]] | None = None) -> None:
        self.manifests = [_manifest()] if manifests is None else manifests
        self.data: dict[str, bytes] = {item["artifact_id"]: PAYLOAD for item in self.manifests}
        self.read_error: BaseException | None = None
        self.list_error: BaseException | None = None
        self.delete_result: bool | None = None
        self.calls: list[tuple[str, ...]] = []

    def list_artifacts(self) -> list[dict[str, Any]]:
        self.calls.append(("list",))
        if self.list_error is not None:
            raise self.list_error
        return [dict(item) for item in self.manifests]

    def read_artifact(self, artifact_id: str) -> bytes:
        self.calls.append(("read", artifact_id))
        if self.read_error is not None:
            raise self.read_error
        return self.data[artifact_id]

    def delete_artifact(self, artifact_id: str) -> bool:
        self.calls.append(("delete", artifact_id))
        if self.delete_result is not None:
            return self.delete_result
        found = any(item["artifact_id"] == artifact_id for item in self.manifests)
        self.manifests = [item for item in self.manifests if item["artifact_id"] != artifact_id]
        return found


def _start(
    monkeypatch: pytest.MonkeyPatch, artifact_client: Any | None
) -> tuple[dict, threading.Thread, list]:
    ready = threading.Event()
    state: dict[str, str] = {}
    failures: list[BaseException] = []
    monkeypatch.setattr(
        "horizon.desktop_mcp.companion.webbrowser.open",
        lambda url, **_kwargs: True,
    )

    def on_ready(url: str, token: str) -> None:
        state["url"] = url
        state["token"] = token
        ready.set()

    def run() -> None:
        try:
            serve_companion(
                port=0,
                on_ready=on_ready,
                open_browser=True,
                artifact_client=artifact_client,
            )
        except BaseException as error:  # surfaced in the test thread
            failures.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(5), "companion did not become ready"
    return state, thread, failures


def _stop(state: dict, thread: threading.Thread, failures: list) -> None:
    if thread.is_alive():
        _http(state, "DELETE", "/api/session", origin=True)
        thread.join(timeout=5)
    assert not thread.is_alive(), "companion did not stop after authenticated revocation"
    assert not failures


@pytest.fixture
def artifact_companion(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[dict, FakeArtifacts]]:
    client = FakeArtifacts()
    state, thread, failures = _start(monkeypatch, client)
    yield state, client
    _stop(state, thread, failures)


@pytest.fixture
def unconfigured_companion(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict]:
    state, thread, failures = _start(monkeypatch, None)
    yield state
    _stop(state, thread, failures)


def _http(
    state: dict,
    method: str,
    path: str,
    *,
    origin: bool | str | None = None,
    authorization: str | None = "default",
    host: str | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    url = urlsplit(str(state["url"]))
    headers: dict[str, str] = {"Host": host or f"{url.hostname}:{url.port}"}
    if authorization == "default":
        headers["Authorization"] = f"Bearer {state['token']}"
    elif authorization is not None:
        headers["Authorization"] = authorization
    if origin is True:
        headers["Origin"] = f"{url.scheme}://{url.netloc}"
    elif isinstance(origin, str):
        headers["Origin"] = origin
    if body is not None:
        headers["Content-Type"] = "application/json"
    connection = http.client.HTTPConnection(str(url.hostname), int(url.port), timeout=4)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        lowered = {key.lower(): value for key, value in response.getheaders()}
        return response.status, lowered, response.read()
    finally:
        connection.close()


def _error(body: bytes) -> str:
    return json.loads(body)["error"]


ROUTES = [
    ("GET", "/api/artifacts"),
    ("GET", f"/api/artifacts/{ARTIFACT_ID}/download"),
    ("DELETE", f"/api/artifacts/{ARTIFACT_ID}"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_artifact_routes_reject_missing_token_wrong_origin_and_wrong_host(
    artifact_companion: tuple[dict, FakeArtifacts], method: str, path: str
) -> None:
    state, client = artifact_companion
    mutating = method == "DELETE"
    good_origin = True if mutating else None

    assert _http(state, method, path, origin=good_origin, authorization=None)[0] == 401
    assert _http(state, method, path, origin=good_origin, authorization="Bearer wrong")[0] == 401
    assert _http(state, method, path, origin="https://attacker.invalid")[0] in {401, 403}
    assert _http(state, method, path, origin=good_origin, host="localhost:1")[0] in {401, 403, 421}
    if mutating:
        # Deletion must carry the exact companion Origin, not merely the token.
        assert _http(state, method, path, origin=None)[0] in {401, 403}
    assert client.calls == []


def test_list_returns_manifests(artifact_companion: tuple[dict, FakeArtifacts]) -> None:
    state, client = artifact_companion
    status, headers, body = _http(state, "GET", "/api/artifacts")
    assert status == 200
    assert json.loads(body) == {"items": client.manifests}
    assert headers["cache-control"] == "no-store"
    assert client.calls == [("list",)]


@pytest.mark.parametrize(
    ("fmt", "content_type", "extension"),
    [
        ("csv", "text/csv; charset=utf-8", "csv"),
        ("json", "application/json; charset=utf-8", "json"),
        ("markdown", "text/markdown; charset=utf-8", "md"),
        ("text", "text/plain; charset=utf-8", "txt"),
    ],
)
def test_download_returns_exact_bytes_with_safe_headers(
    artifact_companion: tuple[dict, FakeArtifacts],
    fmt: str,
    content_type: str,
    extension: str,
) -> None:
    state, client = artifact_companion
    client.manifests = [_manifest(fmt=fmt, title="Quarterly report")]
    status, headers, body = _http(state, "GET", f"/api/artifacts/{ARTIFACT_ID}/download")
    assert status == 200
    assert body == PAYLOAD
    assert headers["content-type"] == content_type
    assert headers["content-length"] == str(len(PAYLOAD))
    assert headers["content-disposition"] == f'attachment; filename="Quarterly report.{extension}"'
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["cache-control"] == "no-store"
    assert headers["content-security-policy"].startswith("default-src 'self'")
    assert "script-src 'self'" in headers["content-security-policy"]
    assert "object-src 'none'" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY"
    assert ("read", ARTIFACT_ID) in client.calls


def test_hostile_title_is_sanitized_in_download_name(
    artifact_companion: tuple[dict, FakeArtifacts],
) -> None:
    state, client = artifact_companion
    client.manifests = [_manifest(title='../evil"\r\n<script>.csv')]
    status, headers, body = _http(state, "GET", f"/api/artifacts/{ARTIFACT_ID}/download")
    assert status == 200
    assert body == PAYLOAD
    assert headers["content-disposition"] == 'attachment; filename="evil script.csv"'


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        (None, "artifact.csv"),
        ("", "artifact.csv"),
        ("...", "artifact.csv"),
        ('../evil"\r\n<script>.csv', "evil script.csv"),
        ("Sales.CSV", "Sales.csv"),
        ("a" * 200, "a" * 80 + ".csv"),
        ("bad\\name:with*chars", "badnamewithchars.csv"),
    ],
)
def test_artifact_file_name_is_ascii_bounded_and_safe(title: Any, expected: str) -> None:
    name = _artifact_file_name(title, "csv")
    assert name == expected
    assert all(char.isascii() and char not in '"<>/\\\r\n' for char in name)


@pytest.mark.parametrize(
    "bad_id",
    [
        "A" * 32,
        "a" * 31,
        "a" * 33,
        "g" * 32,
        "..%2Fsecret",
        "%2e%2e",
        "0123456789abcdef0123456789abcdef%0d",
    ],
)
def test_bad_ids_return_400_without_calling_client(
    artifact_companion: tuple[dict, FakeArtifacts], bad_id: str
) -> None:
    state, client = artifact_companion
    status, _headers, body = _http(state, "GET", f"/api/artifacts/{bad_id}/download")
    assert status == 400
    assert _error(body) == "invalid_artifact_id"
    status, _headers, body = _http(state, "DELETE", f"/api/artifacts/{bad_id}", origin=True)
    assert status == 400
    assert _error(body) == "invalid_artifact_id"
    assert client.calls == []


def test_revoked_artifact_returns_404_with_code(
    artifact_companion: tuple[dict, FakeArtifacts],
) -> None:
    state, client = artifact_companion
    # Revoked artifacts drop out of the list, so the companion reports them as unknown.
    client.manifests = []
    status, _headers, body = _http(state, "GET", f"/api/artifacts/{ARTIFACT_ID}/download")
    assert status == 404
    assert _error(body) == "artifact_unknown"
    assert [call for call in client.calls if call[0] == "read"] == []

    # A race where the list still shows the artifact but the read reports revocation.
    client.manifests = [_manifest()]
    client.read_error = BrokerError("artifact_revoked")
    status, _headers, body = _http(state, "GET", f"/api/artifacts/{ARTIFACT_ID}/download")
    assert status == 404
    assert _error(body) == "artifact_revoked"


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (BrokerError("artifact_unavailable"), 503, "artifact_unavailable"),
        (BrokerError("operation_unauthorized"), 503, "broker_unavailable"),
        (BrokerError("companion_capability_unavailable"), 503, "broker_unavailable"),
        (OSError("pipe closed"), 503, "broker_unavailable"),
    ],
)
def test_broker_failures_map_to_503(
    artifact_companion: tuple[dict, FakeArtifacts],
    error: BaseException,
    status: int,
    code: str,
) -> None:
    state, client = artifact_companion
    client.read_error = error
    got, _headers, body = _http(state, "GET", f"/api/artifacts/{ARTIFACT_ID}/download")
    assert got == status
    assert _error(body) == code

    client.read_error = None
    client.list_error = error
    got, _headers, body = _http(state, "GET", "/api/artifacts")
    assert got == status
    assert _error(body) == code


def test_delete_requires_origin_and_reports_unknown_after_removal(
    artifact_companion: tuple[dict, FakeArtifacts],
) -> None:
    state, client = artifact_companion
    status, _headers, body = _http(state, "DELETE", f"/api/artifacts/{ARTIFACT_ID}", origin=True)
    assert status == 200
    assert json.loads(body) == {"status": "deleted", "id": ARTIFACT_ID}
    assert ("delete", ARTIFACT_ID) in client.calls
    assert client.manifests == []

    status, _headers, body = _http(state, "DELETE", f"/api/artifacts/{ARTIFACT_ID}", origin=True)
    assert status == 404
    assert _error(body) == "artifact_unknown"


def test_no_broker_config_reports_artifacts_unavailable(
    unconfigured_companion: dict,
) -> None:
    state = unconfigured_companion
    status, _headers, body = _http(state, "GET", "/api/artifacts")
    assert status == 503
    assert _error(body) == "artifacts_unavailable"
    status, _headers, body = _http(state, "GET", f"/api/artifacts/{ARTIFACT_ID}/download")
    assert status == 503
    assert _error(body) == "artifacts_unavailable"
    status, _headers, body = _http(state, "DELETE", f"/api/artifacts/{ARTIFACT_ID}", origin=True)
    assert status == 503
    assert _error(body) == "artifacts_unavailable"


def test_broker_client_is_built_lazily_from_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[object] = []

    class SentinelClient:
        def __init__(self, settings: object) -> None:
            built.append(settings)

        def list_artifacts(self) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr(broker_module, "CompanionBrokerClient", SentinelClient, raising=False)
    config = tmp_path / "settings.json"
    config.write_text('{"profile": "local"}', encoding="utf-8")
    companion = PreparationCompanion(config, None)
    assert companion.artifact_client is None
    assert built == []
    assert companion.artifact_manifests() == []
    assert len(built) == 1
    companion.artifact_manifests()
    assert len(built) == 1


def test_missing_broker_client_class_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "settings.json"
    config.write_text('{"profile": "local"}', encoding="utf-8")
    monkeypatch.delattr(broker_module, "CompanionBrokerClient", raising=False)
    companion = PreparationCompanion(config, None)
    with pytest.raises(CompanionError) as raised:
        companion.artifact_manifests()
    assert raised.value.code == "artifacts_unavailable"


def test_artifact_ui_avoids_html_injection_and_clipboard_reads() -> None:
    ui = Path(__file__).resolve().parents[2] / "horizon" / "desktop_mcp" / "companion_ui"
    script = (ui / "app.js").read_text(encoding="utf-8")
    markup = (ui / "index.html").read_text(encoding="utf-8")
    assert "innerHTML" not in script
    assert "clipboard.read" not in script
    assert 'id="artifacts-list"' in markup
    assert 'id="artifacts-refresh"' in markup
