"""Exact gateway metering: the ``gateway_usage.jsonl`` sink, pricing, report, CLI.

Round trips go through the real proxy app (``create_app``). The Anthropic
upstream is the gateway ``FakeProvider`` served by uvicorn on loopback, reached
through ``ProxyConfig.anthropic_api_url``. Every test points the workspace at
``tmp_path`` so the sink never touches ``~/.horizon``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastapi")

from click.testing import CliRunner  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from horizon import gateway_usage as gu  # noqa: E402
from horizon.proxy.server import ProxyConfig, create_app  # noqa: E402
from tests.gateway.fake_provider import FakeProvider, ScriptedResponse  # noqa: E402

MODEL = "claude-opus-4-8"  # in the LiteLLM catalog: $5 in, $25 out, 1h write $10
SESSION_UUID = "11111111-2222-3333-4444-555555555555"
HEADER_SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _sink(tmp_path: Path) -> Path:
    return tmp_path / gu.SINK_FILENAME


def _rows(tmp_path: Path) -> list[dict[str, Any]]:
    path = _sink(tmp_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HORIZON_WORKSPACE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def provider() -> Iterator[FakeProvider]:
    """The fake Anthropic upstream, on a loopback port, for one test."""
    fake = FakeProvider()
    fake.base_url = fake.start(0)
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture
def client(workspace: Path, provider: FakeProvider) -> Any:
    app = create_app(
        ProxyConfig(
            optimize=False,
            cache_enabled=False,
            rate_limit_enabled=False,
            cost_tracking_enabled=False,
            log_requests=False,
            anthropic_api_url=provider.base_url,
        )
    )
    with TestClient(app) as test_client:
        yield test_client


def _json_body(
    text: str = "hi",
    *,
    stream: bool = False,
    metadata: dict[str, Any] | None = None,
    system: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": MODEL,
        "max_tokens": 256,
        "messages": [{"role": "user", "content": text}],
    }
    if stream:
        body["stream"] = True
    if metadata is not None:
        body["metadata"] = metadata
    if system is not None:
        body["system"] = system
    return body


def _post(client: TestClient, body: dict[str, Any], headers: dict[str, str] | None = None):
    merged = {"x-api-key": "sk-ant-test", **(headers or {})}
    return client.post("/v1/messages", json=body, headers=merged)


_USAGE_SPLIT = {
    "input_tokens": 300,
    "output_tokens": 120,
    "cache_read_input_tokens": 46893,
    "cache_creation_input_tokens": 8197,
    "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 8197},
}

_RESPONSE = {
    "id": "msg_meter_1",
    "type": "message",
    "role": "assistant",
    "model": MODEL,
    "content": [{"type": "text", "text": "done"}],
    "stop_reason": "end_turn",
    "usage": _USAGE_SPLIT,
}


_OPUS_RATES = {  # USD per token, from the catalog row for claude-opus-4-8
    "input": 5.0e-6,
    "cache_read": 0.5e-6,
    "write_5m": 6.25e-6,
    "write_1h": 10.0e-6,
    "output": 25.0e-6,
}


# --------------------------------------------------------------------------- #
# Session extraction                                                           #
# --------------------------------------------------------------------------- #


def test_session_header_is_taken_when_it_is_a_plain_token() -> None:
    assert gu.session_from_header(f" {HEADER_SESSION} ") == HEADER_SESSION
    assert gu.session_from_header("has spaces and/slashes") is None
    assert gu.session_from_header("") is None
    assert gu.session_from_header(None) is None


@pytest.mark.parametrize(
    "user_id, expected",
    [
        (f"user_abc_account_xyz_session_{SESSION_UUID}", SESSION_UUID),
        (json.dumps({"device_id": "d", "session_id": SESSION_UUID}), SESSION_UUID),
        (json.dumps({"sessionId": "sess-42"}), "sess-42"),
        ("just-a-plain-user-id-without-a-session", None),
        ("{not json", None),
        ("", None),
        (None, None),
        (12345, None),
        ("x_session_has spaces", None),
    ],
)
def test_session_from_user_id_forms(user_id: Any, expected: str | None) -> None:
    assert gu.session_from_user_id(user_id) == expected


def test_resolve_session_prefers_header_then_metadata_then_none() -> None:
    body = {"metadata": {"user_id": f"u_session_{SESSION_UUID}"}}
    assert gu.resolve_session({gu.SESSION_HEADER: HEADER_SESSION}, body) == HEADER_SESSION
    assert gu.resolve_session({}, body) == SESSION_UUID
    assert gu.resolve_session({}, {"messages": []}) is None
    assert gu.resolve_session({}, None) is None


# --------------------------------------------------------------------------- #
# Pricing arithmetic                                                           #
# --------------------------------------------------------------------------- #


def test_price_splits_5m_and_1h_writes_at_their_own_rates() -> None:
    usage = gu.Usage(
        input=1_000_000,
        cache_read=1_000_000,
        cache_write_5m=1_000_000,
        cache_write_1h=1_000_000,
        output=1_000_000,
    )
    quote = gu.price_usage(MODEL, usage)
    expected = (
        1_000_000 * _OPUS_RATES["input"]
        + 1_000_000 * _OPUS_RATES["cache_read"]
        + 1_000_000 * _OPUS_RATES["write_5m"]
        + 1_000_000 * _OPUS_RATES["write_1h"]
        + 1_000_000 * _OPUS_RATES["output"]
    )
    assert quote.cost_usd == pytest.approx(expected, rel=1e-9)
    assert quote.breakdown == pytest.approx(
        {
            "input": 5.0,
            "cache_read": 0.5,
            "cache_write": 6.25 + 10.0,
            "output": 25.0,
        },
        rel=1e-9,
    )
    assert quote.basis == "catalog"


def test_one_hour_write_costs_more_than_five_minute_write() -> None:
    five = gu.price_usage(MODEL, gu.Usage(cache_write_5m=1000)).cost_usd
    one_hour = gu.price_usage(MODEL, gu.Usage(cache_write_1h=1000)).cost_usd
    assert one_hour == pytest.approx(five * 10.0 / 6.25, rel=1e-9)


def test_zero_usage_is_free_and_marked_no_usage() -> None:
    quote = gu.price_usage(MODEL, gu.Usage())
    assert quote.cost_usd == 0.0
    assert quote.basis == gu.BASIS_NONE


def test_unknown_model_is_unpriced_not_zero() -> None:
    quote = gu.price_usage("claude-not-a-real-model-9", gu.Usage(input=10, output=10))
    assert quote.cost_usd is None
    assert quote.breakdown is None
    assert quote.basis == gu.BASIS_UNPRICED


def test_unsplit_cache_write_is_billed_at_the_default_five_minute_rate() -> None:
    outcome = SimpleNamespace(
        uncached_input_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=500,
        cache_write_5m_tokens=0,
        cache_write_1h_tokens=0,
        output_tokens=0,
    )
    usage = gu.usage_from_outcome(outcome)
    assert usage.cache_write_5m == 500
    assert usage.cache_write_1h == 0
    assert gu.price_usage(MODEL, usage).cost_usd == pytest.approx(500 * _OPUS_RATES["write_5m"])


# --------------------------------------------------------------------------- #
# Sink lines                                                                   #
# --------------------------------------------------------------------------- #


def test_sink_line_has_the_documented_fields_in_order(workspace: Path) -> None:
    line = gu.build_line(
        request_id="req_1",
        status=200,
        stream=False,
        provider="anthropic",
        model=MODEL,
        client="claude-code",
        session="s1",
        turn_id="t1",
        usage=gu.Usage(input=1, cache_read=2, cache_write_5m=3, cache_write_1h=4, output=5),
        usage_source="provider",
        quote=gu.price_usage(MODEL, gu.Usage(input=1, output=5)),
        passthrough=False,
        transforms=["x"],
    )
    assert list(line) == [
        "v",
        "ts",
        "request_id",
        "status",
        "stream",
        "provider",
        "model",
        "client",
        "auth",
        "session",
        "turn_id",
        "usage",
        "usage_source",
        "cost_usd",
        "cost_breakdown_usd",
        "pricing_basis",
        "passthrough",
        "transforms",
    ]
    assert list(line["usage"]) == [
        "input",
        "cache_read",
        "cache_write_5m",
        "cache_write_1h",
        "output",
    ]


def test_append_line_never_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file", encoding="utf-8")
    # The parent is a regular file, so the write fails. The sink must report it.
    assert gu.append_line({"v": 1}, path=blocker / "gateway_usage.jsonl") is False


# --------------------------------------------------------------------------- #
# End to end through the real proxy app                                        #
# --------------------------------------------------------------------------- #


def test_non_stream_round_trip_writes_exact_usage(
    client: TestClient, workspace: Path, provider: FakeProvider
) -> None:
    provider.push(_RESPONSE)

    r = _post(client, _json_body(), headers={gu.SESSION_HEADER: HEADER_SESSION})

    assert r.status_code == 200
    rows = _rows(workspace)
    assert len(rows) == 1
    row = rows[0]
    assert row["v"] == 1
    assert row["status"] == 200
    assert row["stream"] is False
    assert row["model"] == MODEL
    assert row["session"] == HEADER_SESSION
    assert row["request_id"]
    assert row["usage"] == {
        "input": 300,
        "cache_read": 46893,
        "cache_write_5m": 0,
        "cache_write_1h": 8197,
        "output": 120,
    }
    assert row["usage_source"] == "provider"
    assert row["pricing_basis"] == "catalog"
    expected_cost = (
        300 * _OPUS_RATES["input"]
        + 46893 * _OPUS_RATES["cache_read"]
        + 8197 * _OPUS_RATES["write_1h"]
        + 120 * _OPUS_RATES["output"]
    )
    assert row["cost_usd"] == pytest.approx(expected_cost, rel=1e-9)
    assert row["cost_breakdown_usd"]["cache_write"] == pytest.approx(8197 * _OPUS_RATES["write_1h"])
    assert row["passthrough"] is False
    assert row["transforms"] == [] or isinstance(row["transforms"], list)


def test_stream_round_trip_writes_exact_usage(
    client: TestClient, workspace: Path, provider: FakeProvider
) -> None:
    # The fake provider renders a Messages body as SSE when the request streams:
    # the usage block goes on message_start (input side), output on message_delta.
    provider.push(
        {
            **_RESPONSE,
            "usage": {
                "input_tokens": 12,
                "cache_read_input_tokens": 46893,
                "cache_creation_input_tokens": 8197,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": 8197,
                },
                "output_tokens": 120,
            },
        }
    )

    r = _post(client, _json_body(stream=True), headers={gu.SESSION_HEADER: HEADER_SESSION})
    assert r.status_code == 200
    assert b"message_stop" in r.content

    assert r.status_code == 200
    rows = _rows(workspace)
    assert len(rows) == 1
    row = rows[0]
    assert row["stream"] is True
    assert row["status"] == 200
    assert row["session"] == HEADER_SESSION
    assert row["usage"]["cache_read"] == 46893
    assert row["usage"]["cache_write_1h"] == 8197
    assert row["usage"]["cache_write_5m"] == 0
    assert row["usage"]["input"] == 12
    assert row["usage"]["output"] == 120


def test_error_status_is_still_recorded(
    client: TestClient, workspace: Path, provider: FakeProvider
) -> None:
    error = {"type": "error", "error": {"type": "invalid_request_error", "message": "nope"}}
    provider.push(ScriptedResponse(body=error, status=400))

    r = _post(client, _json_body(), headers={gu.SESSION_HEADER: HEADER_SESSION})

    assert r.status_code == 400
    rows = _rows(workspace)
    assert len(rows) == 1
    assert rows[0]["status"] == 400
    assert rows[0]["usage"]["output"] == 0
    assert rows[0]["session"] == HEADER_SESSION


def test_pre_upstream_rejection_is_recorded_with_no_usage(
    client: TestClient, workspace: Path, provider: FakeProvider
) -> None:
    # A body that is not JSON is refused before any upstream call.
    r = client.post(
        "/v1/messages",
        content=b"{not json",
        headers={"content-type": "application/json", "x-api-key": "sk-ant-test"},
    )

    assert r.status_code >= 400
    rows = _rows(workspace)
    assert len(rows) == 1
    assert rows[0]["status"] == r.status_code
    assert rows[0]["usage_source"] == "none"
    assert rows[0]["cost_usd"] == 0.0


def test_cache_read_only_turn_is_recorded(
    client: TestClient, workspace: Path, provider: FakeProvider
) -> None:
    cache_only = {
        "id": "msg_cache_only",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": [{"type": "text", "text": "."}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 0, "output_tokens": 1, "cache_read_input_tokens": 5000},
    }
    provider.push(cache_only)

    _post(client, _json_body(), headers={gu.SESSION_HEADER: HEADER_SESSION})

    rows = _rows(workspace)
    assert len(rows) == 1
    assert rows[0]["usage"]["input"] == 0
    assert rows[0]["usage"]["cache_read"] == 5000
    assert rows[0]["usage"]["cache_write_5m"] == 0
    assert rows[0]["usage"]["cache_write_1h"] == 0
    assert rows[0]["usage_source"] == "provider"


def test_session_from_metadata_when_header_is_absent(
    client: TestClient, workspace: Path, provider: FakeProvider
) -> None:
    provider.push(_RESPONSE)
    user_id = f"user_abc_account_xyz_session_{SESSION_UUID}"

    _post(client, _json_body(metadata={"user_id": user_id}))

    assert _rows(workspace)[0]["session"] == SESSION_UUID


def test_session_is_null_when_neither_source_is_present(
    client: TestClient, workspace: Path, provider: FakeProvider
) -> None:
    provider.push(_RESPONSE)

    _post(client, _json_body())

    row = _rows(workspace)[0]
    assert row["session"] is None
    assert row["passthrough"] is False


def test_one_line_per_request(client: TestClient, workspace: Path, provider: FakeProvider) -> None:
    provider.push(_RESPONSE)

    for _ in range(3):
        _post(client, _json_body())

    assert len(_rows(workspace)) == 3


SENTINEL = "ZZ-SENTINEL-7f3a9c-DO-NOT-LOG"


@pytest.mark.parametrize(
    "placement",
    ["user_message", "system_prompt", "tool_result", "assistant_reply", "metadata_free_text"],
)
def test_no_message_content_reaches_the_sink(
    client: TestClient, workspace: Path, provider: FakeProvider, placement: str
) -> None:
    reply = {**_RESPONSE, "content": [{"type": "text", "text": f"reply {SENTINEL}"}]}
    provider.push(reply)

    body: dict[str, Any] = {
        "model": MODEL,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hello"}],
    }
    if placement == "user_message":
        body["messages"] = [{"role": "user", "content": f"hello {SENTINEL}"}]
    elif placement == "system_prompt":
        body["system"] = f"you are helpful {SENTINEL}"
    elif placement == "tool_result":
        body["messages"] = [
            {"role": "user", "content": "run it"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": f"out {SENTINEL}"}
                ],
            },
        ]
    elif placement == "metadata_free_text":
        body["metadata"] = {"user_id": f"not-a-session {SENTINEL}"}

    r = _post(client, body, headers={gu.SESSION_HEADER: HEADER_SESSION})
    assert r.status_code == 200
    assert SENTINEL in r.text, (
        "the sentinel must have reached the proxy for the test to mean anything"
    )

    raw = _sink(workspace).read_text(encoding="utf-8")
    assert SENTINEL not in raw
    assert "hello" not in raw
    assert _rows(workspace), "the request still produced a line"


def test_non_messages_route_writes_nothing(
    client: TestClient, workspace: Path, provider: FakeProvider
) -> None:
    client.get("/health")
    assert not _sink(workspace).exists()


# --------------------------------------------------------------------------- #
# Auth classification (seat OAuth vs API key). Never the credential itself.     #
# --------------------------------------------------------------------------- #

FAKE_BEARER = "sk-ant-oat01-ZZ-FAKE-BEARER-7e1d4c-DO-NOT-LOG"


@pytest.mark.parametrize(
    "headers, expected",
    [
        ({"x-api-key": "sk-ant-api03-whatever"}, gu.AUTH_API_KEY),
        ({"authorization": "Bearer sk-ant-api03-whatever"}, gu.AUTH_API_KEY),
        ({"authorization": f"Bearer {FAKE_BEARER}"}, gu.AUTH_OAUTH),
        ({"authorization": "bearer lowercase-token"}, gu.AUTH_OAUTH),
        ({"x-api-key": "k", "authorization": f"Bearer {FAKE_BEARER}"}, gu.AUTH_API_KEY),
        ({"authorization": "Basic dXNlcjpwYXNz"}, gu.AUTH_OTHER),
        ({"authorization": "Bearer   "}, gu.AUTH_OTHER),
        ({}, gu.AUTH_OTHER),
        (None, gu.AUTH_OTHER),
    ],
)
def test_auth_class_from_headers(headers: Any, expected: str) -> None:
    assert gu.auth_class(headers) == expected


def test_oauth_request_is_labelled_estimate_and_token_never_reaches_sink(
    client: TestClient, workspace: Path, provider: FakeProvider
) -> None:
    provider.push(_RESPONSE)
    # Sent directly, not through _post: _post adds an x-api-key, and this request is OAuth only.
    r = client.post(
        "/v1/messages",
        json=_json_body(),
        headers={
            "authorization": f"Bearer {FAKE_BEARER}",
            gu.SESSION_HEADER: HEADER_SESSION,
        },
    )
    assert r.status_code == 200

    # The credential really went upstream, so its absence from the sink means something.
    upstream_auth = provider.calls[-1].headers.get("authorization", "")
    assert upstream_auth == f"Bearer {FAKE_BEARER}"

    row = _rows(workspace)[0]
    assert row["auth"] == gu.AUTH_OAUTH
    assert row["pricing_basis"] == gu.BASIS_LIST_ESTIMATE
    assert row["cost_usd"] is not None and row["cost_usd"] > 0
    assert row["usage"]["output"] == 120

    raw = _sink(workspace).read_text(encoding="utf-8")
    assert FAKE_BEARER not in raw
    assert "sk-ant-oat" not in raw
    assert "ZZ-FAKE-BEARER" not in raw


def test_api_key_request_keeps_catalog_basis(
    client: TestClient, workspace: Path, provider: FakeProvider
) -> None:
    provider.push(_RESPONSE)
    _post(client, _json_body())

    row = _rows(workspace)[0]
    assert row["auth"] == gu.AUTH_API_KEY
    assert row["pricing_basis"] == "catalog"


def test_oauth_pre_upstream_rejection_is_recorded_without_token(
    client: TestClient, workspace: Path, provider: FakeProvider
) -> None:
    r = client.post(
        "/v1/messages",
        content=b"{not json",
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {FAKE_BEARER}",
        },
    )
    assert r.status_code >= 400
    row = _rows(workspace)[0]
    assert row["auth"] == gu.AUTH_OAUTH
    assert FAKE_BEARER not in _sink(workspace).read_text(encoding="utf-8")


def test_passthrough_flag_is_read_from_config(workspace: Path) -> None:
    app = create_app(
        ProxyConfig(
            optimize=False,
            cache_enabled=False,
            rate_limit_enabled=False,
            cost_tracking_enabled=False,
            log_requests=False,
        )
    )
    with TestClient(app) as test_client:
        # Not a W1 field yet: the default must read as False, and a value the
        # config does carry must come through.
        app.state.proxy.config.passthrough = True
        r = test_client.post(
            "/v1/messages",
            content=b"{not json",
            headers={"content-type": "application/json", "x-api-key": "sk-ant-test"},
        )
    assert r.status_code >= 400
    assert _rows(workspace)[0]["passthrough"] is True


# --------------------------------------------------------------------------- #
# Report                                                                       #
# --------------------------------------------------------------------------- #


def _line(
    *,
    ts: str,
    session: str | None,
    input_: int = 0,
    read: int = 0,
    w5: int = 0,
    w1: int = 0,
    output: int = 0,
    cost: float | None = 0.0,
    basis: str = "catalog",
) -> dict[str, Any]:
    return {
        "v": 1,
        "ts": ts,
        "request_id": "r",
        "status": 200,
        "stream": False,
        "provider": "anthropic",
        "model": MODEL,
        "client": None,
        "session": session,
        "turn_id": None,
        "usage": {
            "input": input_,
            "cache_read": read,
            "cache_write_5m": w5,
            "cache_write_1h": w1,
            "output": output,
        },
        "usage_source": "provider",
        "cost_usd": cost,
        "cost_breakdown_usd": None
        if cost is None
        else {"input": cost / 2, "cache_read": 0.0, "cache_write": cost / 2, "output": 0.0},
        "pricing_basis": basis,
        "passthrough": False,
        "transforms": [],
    }


def _write_rows(path: Path, rows: list[Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write((row if isinstance(row, str) else json.dumps(row)) + "\n")


def test_report_groups_by_session_and_totals_correctly(tmp_path: Path) -> None:
    path = tmp_path / gu.SINK_FILENAME
    _write_rows(
        path,
        [
            _line(
                ts="2026-10-15T09:00:00+00:00",
                session="s-a",
                input_=100,
                read=900,
                w1=0,
                output=10,
                cost=0.5,
            ),
            _line(
                ts="2026-10-15T09:05:00+00:00",
                session="s-a",
                input_=50,
                read=950,
                w5=100,
                output=20,
                cost=0.25,
            ),
            _line(
                ts="2026-10-15T10:00:00+00:00",
                session="s-b",
                input_=200,
                read=0,
                w1=800,
                output=5,
                cost=1.0,
            ),
            _line(
                ts="2026-10-15T11:00:00+00:00",
                session=None,
                input_=1,
                output=1,
                cost=None,
                basis="unpriced",
            ),
            "this is not json",
            {"v": 2, "ts": "2026-10-15T11:00:00+00:00"},
        ],
    )

    report = gu.build_report(path)

    assert report["lines"] == 4
    assert report["skipped"] == 2
    by_session = {item["session"]: item for item in report["sessions"]}
    a = by_session["s-a"]
    assert a["requests"] == 2
    assert a["tokens"]["input"] == 150
    assert a["tokens"]["cache_read"] == 1850
    assert a["tokens"]["cache_write"] == 100
    assert a["tokens"]["output"] == 30
    assert a["cache_hit_rate"] == pytest.approx(1850 / (150 + 1850 + 100), rel=1e-3)
    assert a["cost_usd"] == pytest.approx(0.75)
    assert a["first"] == "2026-10-15T09:00:00+00:00"
    assert a["last"] == "2026-10-15T09:05:00+00:00"

    b = by_session["s-b"]
    assert b["cache_hit_rate"] == 0.0
    assert b["tokens"]["cache_write_1h"] == 800

    none_group = by_session[None]
    assert none_group["unpriced_requests"] == 1
    assert none_group["cost_usd"] == 0.0

    totals = report["totals"]
    assert totals["requests"] == 4
    assert totals["cost_usd"] == pytest.approx(1.75)
    assert totals["tokens"]["input"] == 351
    assert totals["unpriced_requests"] == 1


def test_report_filters_by_session_and_since(tmp_path: Path) -> None:
    path = tmp_path / gu.SINK_FILENAME
    _write_rows(
        path,
        [
            _line(ts="2026-10-15T08:00:00+00:00", session="s-a", input_=1, cost=0.1),
            _line(ts="2026-10-15T12:00:00+00:00", session="s-a", input_=2, cost=0.2),
            _line(ts="2026-10-15T12:00:00+00:00", session="s-b", input_=4, cost=0.4),
        ],
    )

    since = datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)
    report = gu.build_report(path, since=since)
    assert report["totals"]["requests"] == 2

    only_a = gu.build_report(path, session="s-a")
    assert [item["session"] for item in only_a["sessions"]] == ["s-a"]
    assert only_a["totals"]["requests"] == 2


def test_report_on_missing_file_is_empty_not_an_error(tmp_path: Path) -> None:
    report = gu.build_report(tmp_path / "absent.jsonl")
    assert report["lines"] == 0
    assert "No gateway usage recorded" in gu.render_report(report)


def test_cli_report_json_and_text(tmp_path: Path) -> None:
    from horizon.cli.main import main

    _write_rows(
        tmp_path / gu.SINK_FILENAME,
        [
            _line(
                ts="2026-10-15T09:00:00+00:00",
                session="cli-s",
                input_=10,
                read=90,
                output=5,
                cost=0.42,
            )
        ],
    )
    runner = CliRunner()

    as_json = runner.invoke(main, ["gateway", "report", "--workspace", str(tmp_path), "--json"])
    assert as_json.exit_code == 0, as_json.output
    data = json.loads(as_json.output)
    assert data["totals"]["requests"] == 1
    assert data["sessions"][0]["session"] == "cli-s"
    assert data["sessions"][0]["cache_hit_rate"] == pytest.approx(0.9)

    text = runner.invoke(main, ["gateway", "report", "--workspace", str(tmp_path)])
    assert text.exit_code == 0, text.output
    assert "session cli-s" in text.output
    assert "TOTAL" in text.output

    bad = runner.invoke(
        main, ["gateway", "report", "--workspace", str(tmp_path), "--since", "yesterday"]
    )
    assert bad.exit_code != 0
