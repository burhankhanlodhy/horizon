from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from horizon.desktop_mcp import backend_worker, broker
from horizon.desktop_mcp.backend_worker import Runtime
from horizon.desktop_mcp.broker import BrokerClient
from horizon.desktop_mcp.broker_transport import (
    BrokerTransportError,
    _validate_operation_args,
    decode_frame,
)
from horizon.desktop_mcp.settings import load_settings

SOURCE_ID = "cs1_example"
SELECTOR = {"type": "lines", "start": 1, "end": 2}
OPERATION = {"kind": "aggregate", "group_by": ["region"]}


def _write_settings(path: Path, roots: list[Path], **updates: Any) -> None:
    data: dict[str, Any] = {
        "profile": "local",
        "approved_roots": [str(root) for root in roots],
        "max_source_bytes": 1024,
        "max_snapshot_bytes": 2048,
        "max_store_bytes": 4096,
    }
    data.update(updates)
    path.write_text(json.dumps(data), encoding="utf-8")


def _runtime(config: Path) -> Runtime:
    settings = load_settings(config)
    return Runtime(
        config,
        expected_broker_id=settings.broker_id,
        expected_max_clients=settings.max_broker_clients,
        expected_timeout=settings.broker_timeout_seconds,
    )


def _invalid(operation: str, args: Any) -> None:
    with pytest.raises(BrokerTransportError) as caught:
        _validate_operation_args(operation, args)
    assert caught.value.code == "invalid_arguments"


def _analyze_args(**updates: Any) -> dict[str, Any]:
    args: dict[str, Any] = {"source_id": SOURCE_ID, "operation": OPERATION}
    args.update(updates)
    return args


def _render_args(**updates: Any) -> dict[str, Any]:
    args: dict[str, Any] = {"source_id": SOURCE_ID, "format": "csv", "title": "Report"}
    args.update(updates)
    return args


def _batch_args(**updates: Any) -> dict[str, Any]:
    args: dict[str, Any] = {"requests": [{"source_id": SOURCE_ID, "selector": SELECTOR}]}
    args.update(updates)
    return args


# --- validation -------------------------------------------------------------


def test_compute_envelopes_accept_each_well_formed_shape() -> None:
    _validate_operation_args("analyze", _analyze_args())
    _validate_operation_args("analyze", _analyze_args(result_token_budget=128))
    _validate_operation_args("analyze", _analyze_args(result_token_budget=8000))
    for fmt in ("csv", "json", "markdown", "text"):
        _validate_operation_args("render", _render_args(format=fmt))
    _validate_operation_args("render", _render_args(operation=OPERATION))
    _validate_operation_args("render", _render_args(formula_policy="reject"))
    _validate_operation_args("render", _render_args(title="x" * 120))
    _validate_operation_args("retrieve_batch", _batch_args())
    _validate_operation_args("retrieve_batch", _batch_args(result_token_budget=2000))
    _validate_operation_args(
        "retrieve_batch",
        {
            "requests": [
                {"source_id": SOURCE_ID, "selector": {"type": "json_pointer", "pointer": ""}}
                for _ in range(16)
            ]
        },
    )


def test_compute_frames_decode_with_the_shared_closed_envelope() -> None:
    frame = (
        b'{"operation":"render","args":{"source_id":"cs1_a","format":"csv","title":"t"},'
        b'"request_id":"r-1"}'
    )
    assert decode_frame(frame)["operation"] == "render"


@pytest.mark.parametrize(
    ("operation", "args"),
    [
        ("analyze", {"source_id": SOURCE_ID}),
        ("analyze", {"operation": OPERATION}),
        ("analyze", _analyze_args(extra=1)),
        ("analyze", _analyze_args(selector=SELECTOR)),
        ("render", {"source_id": SOURCE_ID, "format": "csv"}),
        ("render", _render_args(extra=True)),
        ("render", _render_args(source="cs1_other")),
        ("retrieve_batch", {}),
        ("retrieve_batch", _batch_args(extra=1)),
    ],
)
def test_compute_envelopes_reject_missing_and_extra_keys(operation: str, args: Any) -> None:
    _invalid(operation, args)


@pytest.mark.parametrize("source_id", ["example", "cs2_example", "", "cs1_" + "x" * 4096])
def test_compute_source_ids_must_be_cs1_handles(source_id: str) -> None:
    _invalid("analyze", _analyze_args(source_id=source_id))
    _invalid("render", _render_args(source_id=source_id))
    _invalid(
        "retrieve_batch",
        {"requests": [{"source_id": source_id, "selector": SELECTOR}]},
    )


def test_compute_source_id_must_be_a_string() -> None:
    _invalid("analyze", _analyze_args(source_id=None))
    _invalid("render", _render_args(source_id=["cs1_example"]))


@pytest.mark.parametrize("budget", [True, False, 127, 8001, 1.5, "2000", None])
def test_compute_result_token_budget_must_be_an_int_in_range(budget: Any) -> None:
    _invalid("analyze", _analyze_args(result_token_budget=budget))
    _invalid("retrieve_batch", _batch_args(result_token_budget=budget))


@pytest.mark.parametrize("operation", ["", "string", ["kind"], None])
def test_analyze_operation_must_be_a_dict(operation: Any) -> None:
    _invalid("analyze", _analyze_args(operation=operation))
    _invalid("render", _render_args(operation=operation))


@pytest.mark.parametrize("fmt", ["pdf", "CSV", "", None, ["csv"]])
def test_render_format_is_closed(fmt: Any) -> None:
    _invalid("render", _render_args(format=fmt))


@pytest.mark.parametrize("policy", ["raw", "", "ESCAPE", None, ["escape"]])
def test_render_formula_policy_is_closed(policy: Any) -> None:
    _invalid("render", _render_args(formula_policy=policy))


@pytest.mark.parametrize("title", ["", "x" * 121, None, 7])
def test_render_title_is_a_bounded_string(title: Any) -> None:
    _invalid("render", _render_args(title=title))


@pytest.mark.parametrize("count", [0, 17])
def test_batch_has_between_one_and_sixteen_requests(count: int) -> None:
    requests = [{"source_id": SOURCE_ID, "selector": SELECTOR} for _ in range(count)]
    _invalid("retrieve_batch", {"requests": requests})


def test_batch_requests_must_be_a_list() -> None:
    _invalid("retrieve_batch", {"requests": {"source_id": SOURCE_ID, "selector": SELECTOR}})
    _invalid("retrieve_batch", {"requests": None})


@pytest.mark.parametrize(
    "item",
    [
        {"source_id": SOURCE_ID},
        {"source_id": SOURCE_ID, "selector": SELECTOR, "cursor": "abc"},
        {"source_id": SOURCE_ID, "selector": SELECTOR, "result_token_budget": 500},
        {"source_id": "example", "selector": SELECTOR},
        "cs1_example",
    ],
)
def test_batch_items_are_closed_and_source_checked(item: Any) -> None:
    _invalid("retrieve_batch", {"requests": [item]})


@pytest.mark.parametrize(
    "selector",
    [
        {"type": "lines", "start": 2, "end": 1},
        {"type": "lines", "start": 1, "end": 1, "extra": 1},
        {"type": "lines", "start": True, "end": 2},
        {"type": "lines", "start": 1, "end": 2002},
        {"type": "rows", "start": 0, "end": 1},
        {"type": "markdown_section", "heading": " "},
        {"type": "json_pointer", "pointer": "no-slash"},
        {"type": "unknown"},
        "lines",
        None,
    ],
)
def test_batch_selectors_reuse_the_exact_recovery_validator(selector: Any) -> None:
    _invalid("retrieve_batch", {"requests": [{"source_id": SOURCE_ID, "selector": selector}]})


def test_unknown_compute_args_are_not_accepted_as_legacy_shapes() -> None:
    _invalid("analyze", "not-a-dict")
    _invalid("render", None)
    _invalid("retrieve_batch", ["requests"])


def test_serve_allowlist_contains_compute_operations() -> None:
    from horizon.desktop_mcp import broker_transport

    assert {"analyze", "render", "retrieve_batch"} <= set(broker_transport._COMPUTE_OPERATIONS)


# --- worker dispatch --------------------------------------------------------


class _RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.closed = 0

    def analyze(self, source_id: str, operation: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("analyze", (source_id, operation), kwargs))
        return {"tool": "analyze", "source_id": source_id}

    def render(self, source_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("render", (source_id,), kwargs))
        return {"tool": "render"}

    def retrieve_batch(self, requests: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("retrieve_batch", (requests,), kwargs))
        return {"tool": "retrieve_batch"}

    def stats(self) -> dict[str, Any]:
        return {}

    def close(self) -> None:
        self.closed += 1


def _runtime_with_fake(tmp_path: Path) -> tuple[Runtime, _RecordingBackend]:
    config = tmp_path / "settings.json"
    _write_settings(config, [])
    runtime = _runtime(config)
    backend = _RecordingBackend()
    runtime.backend = backend
    return runtime, backend


def test_worker_dispatches_analyze_with_budget(tmp_path: Path) -> None:
    runtime, backend = _runtime_with_fake(tmp_path)
    result = runtime.request("analyze", _analyze_args(result_token_budget=512))
    assert result == {"tool": "analyze", "source_id": SOURCE_ID}
    assert backend.calls == [
        ("analyze", (SOURCE_ID, OPERATION), {"result_token_budget": 512}),
    ]


def test_worker_dispatches_analyze_without_budget(tmp_path: Path) -> None:
    runtime, backend = _runtime_with_fake(tmp_path)
    runtime.request("analyze", _analyze_args())
    assert backend.calls == [("analyze", (SOURCE_ID, OPERATION), {"result_token_budget": None})]


def test_worker_dispatches_render_with_default_formula_policy(tmp_path: Path) -> None:
    runtime, backend = _runtime_with_fake(tmp_path)
    assert runtime.request("render", _render_args()) == {"tool": "render"}
    assert backend.calls == [
        (
            "render",
            (SOURCE_ID,),
            {"fmt": "csv", "title": "Report", "operation": None, "formula_policy": "escape"},
        ),
    ]


def test_worker_dispatches_render_with_operation_and_policy(tmp_path: Path) -> None:
    runtime, backend = _runtime_with_fake(tmp_path)
    runtime.request(
        "render",
        _render_args(format="markdown", operation=OPERATION, formula_policy="reject"),
    )
    assert backend.calls == [
        (
            "render",
            (SOURCE_ID,),
            {
                "fmt": "markdown",
                "title": "Report",
                "operation": OPERATION,
                "formula_policy": "reject",
            },
        ),
    ]


def test_worker_dispatches_retrieve_batch(tmp_path: Path) -> None:
    runtime, backend = _runtime_with_fake(tmp_path)
    requests = [{"source_id": SOURCE_ID, "selector": SELECTOR}]
    assert runtime.request("retrieve_batch", _batch_args(result_token_budget=900)) == {
        "tool": "retrieve_batch"
    }
    assert backend.calls == [("retrieve_batch", (requests,), {"result_token_budget": 900})]


def test_worker_rejects_invalid_compute_args_before_the_backend(tmp_path: Path) -> None:
    from horizon.desktop_mcp.broker import BrokerError

    runtime, backend = _runtime_with_fake(tmp_path)
    with pytest.raises(BrokerError) as caught:
        runtime.request("render", _render_args(format="pdf"))
    assert caught.value.code == "invalid_request"
    assert backend.calls == []


def test_worker_closes_backend_when_settings_rebuild_replaces_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "settings.json"
    _write_settings(config, [])
    runtime = _runtime(config)
    old = _RecordingBackend()
    new = _RecordingBackend()
    runtime.backend = old
    monkeypatch.setattr(backend_worker, "_make_backend", lambda settings: new)
    _write_settings(config, [], max_source_bytes=2048)
    runtime._reload()
    assert old.closed == 1
    assert runtime.backend is new
    assert new.closed == 0


def test_worker_closes_backend_on_shutdown_and_tolerates_missing_close(
    tmp_path: Path,
) -> None:
    runtime, backend = _runtime_with_fake(tmp_path)
    runtime.close()
    assert backend.closed == 1
    assert runtime.backend is None
    runtime.close()
    assert backend.closed == 1

    class NoClose:
        pass

    runtime.backend = NoClose()
    runtime.close()
    assert runtime.backend is None


def test_make_backend_passes_the_configured_artifact_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, Any]] = []

    class CapturingBackend:
        def __init__(self, reader: Any, **kwargs: Any) -> None:
            captured.append(kwargs)

    import horizon.desktop_mcp.local_backend as local_backend

    monkeypatch.setattr(local_backend, "LocalBackend", CapturingBackend)
    approved = tmp_path / "approved"
    approved.mkdir()
    artifacts = tmp_path / "artifacts"
    _write_settings(tmp_path / "plain.json", [approved])
    _write_settings(
        tmp_path / "compute.json",
        [approved],
        tool_profile="compute",
        artifact_root=str(artifacts),
    )
    backend_worker._make_backend(load_settings(tmp_path / "plain.json"))
    backend_worker._make_backend(load_settings(tmp_path / "compute.json"))
    assert captured[0]["artifact_root"] is None
    assert captured[1]["artifact_root"] == artifacts


# --- BrokerClient wire args -------------------------------------------------


@pytest.fixture
def captured_exchange(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_exchange(
        pipe_name: str, operation: str, args: dict[str, Any], timeout_seconds: float = 10
    ) -> dict[str, Any]:
        calls.append((operation, args))
        return {"tool": operation}

    monkeypatch.setattr(broker, "current_user_sid", lambda: "S-1-5-21-test")
    monkeypatch.setattr(broker, "exchange", fake_exchange)
    return calls


@pytest.fixture
def client(tmp_path: Path, captured_exchange: Any) -> BrokerClient:
    config = tmp_path / "settings.json"
    _write_settings(config, [])
    return BrokerClient(load_settings(config))


def test_client_analyze_sends_exact_wire_args(client: BrokerClient, captured_exchange: Any) -> None:
    client.analyze(SOURCE_ID, OPERATION)
    client.analyze(SOURCE_ID, OPERATION, result_token_budget=256)
    assert captured_exchange == [
        ("analyze", {"source_id": SOURCE_ID, "operation": OPERATION}),
        (
            "analyze",
            {"source_id": SOURCE_ID, "operation": OPERATION, "result_token_budget": 256},
        ),
    ]


def test_client_render_sends_format_on_the_wire(
    client: BrokerClient, captured_exchange: Any
) -> None:
    client.render(SOURCE_ID, fmt="json", title="Quarterly")
    client.render(
        SOURCE_ID,
        fmt="text",
        title="Quarterly",
        operation=OPERATION,
        formula_policy="reject",
    )
    assert captured_exchange == [
        (
            "render",
            {
                "source_id": SOURCE_ID,
                "format": "json",
                "title": "Quarterly",
                "formula_policy": "escape",
            },
        ),
        (
            "render",
            {
                "source_id": SOURCE_ID,
                "format": "text",
                "title": "Quarterly",
                "operation": OPERATION,
                "formula_policy": "reject",
            },
        ),
    ]


def test_client_retrieve_batch_sends_exact_wire_args(
    client: BrokerClient, captured_exchange: Any
) -> None:
    requests = [{"source_id": SOURCE_ID, "selector": SELECTOR}]
    client.retrieve_batch(requests)
    client.retrieve_batch(requests, result_token_budget=4000)
    assert captured_exchange == [
        ("retrieve_batch", {"requests": requests}),
        ("retrieve_batch", {"requests": requests, "result_token_budget": 4000}),
    ]


# --- real backend round trips -----------------------------------------------

REAL_OPERATION = {
    "columns": {"amount": "decimal"},
    "group_by": ["region"],
    "aggregates": [{"op": "sum", "column": "amount", "as": "total"}],
    "order_by": [{"column": "region"}],
}


def _sales_runtime(tmp_path: Path, **updates: Any) -> tuple[Runtime, Path, Path, str]:
    approved = tmp_path / "approved"
    approved.mkdir()
    (approved / "sales.csv").write_bytes(b"region,amount\nnorth,3.10\nsouth,4\nnorth,0.90\n")
    config = tmp_path / "settings.json"
    _write_settings(config, [approved], **updates)
    runtime = _runtime(config)
    hits = runtime.request("search", {"query": "sales", "limit": 5})["hits"]
    return runtime, approved, config, hits[0]["source_id"]


def test_analyze_round_trip_through_worker_with_real_backend(tmp_path: Path) -> None:
    runtime, _approved, _config, source_id = _sales_runtime(tmp_path)
    try:
        result = runtime.request("analyze", {"source_id": source_id, "operation": REAL_OPERATION})
        assert result["tool"] == "analyze"
        assert result["source_id"] == source_id
        assert result["columns"] == ["region", "total"]
        assert result["rows"] == [["north", "4.00"], ["south", "4"]]
        assert result["lineage"]["scanned"] == 3
    finally:
        runtime.close()


def test_render_round_trip_and_revocation_when_root_is_removed(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    runtime, _approved, config, source_id = _sales_runtime(
        tmp_path, tool_profile="compute", artifact_root=str(artifacts)
    )
    try:
        result = runtime.request(
            "render",
            {
                "source_id": source_id,
                "format": "csv",
                "title": "Totals",
                "operation": REAL_OPERATION,
            },
        )
        manifest = result["artifact"]
        assert result["delivery"] == "stored_locally_not_in_result"
        assert str(artifacts) not in json.dumps(result)
        store = runtime.backend.artifact_store
        assert store.read(manifest["artifact_id"]) == b"region,total\r\nnorth,4.00\r\nsouth,4\r\n"
        written = list(artifacts.rglob("artifact-*.csv"))
        assert len(written) == 1

        # Removing the grant rebuilds the backend, which closes the old store
        # and removes its owned files.
        _write_settings(config, [], tool_profile="compute", artifact_root=str(artifacts))
        runtime.request("stats", {})
        assert not written[0].exists()
    finally:
        runtime.close()


def test_too_large_analysis_keeps_its_metadata_across_the_worker(tmp_path: Path) -> None:
    from horizon.desktop_mcp.broker import BrokerError

    runtime, _approved, _config, source_id = _sales_runtime(tmp_path)
    runtime.backend.max_result_bytes = 1024
    try:
        with pytest.raises(BrokerError) as caught:
            runtime.request(
                "analyze",
                {
                    "source_id": source_id,
                    "operation": {"select": ["region"]},
                    "result_token_budget": 128,
                },
            )
        assert caught.value.code == "analysis_result_too_large"
        assert caught.value.metadata["source_id"] == source_id
        assert caught.value.metadata["output_rows"] == 3
    finally:
        runtime.close()
