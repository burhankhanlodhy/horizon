"""The keep-alive liveness id in the base URL path (``/k/<id>``).

Codex's built-in provider cannot send extra headers, so ``wrap codex`` puts
the per-launch id in the path, after any ``/p/<project>`` prefix; the proxy
strips it and reads it as ``X-Horizon-Keepalive-Id``.
"""

from __future__ import annotations

from horizon.proxy.account_analytics import AUXILIARY, INFERENCE
from horizon.proxy.project_context import strip_project_path_prefix
from horizon.proxy.project_policy import (
    split_keepalive_path,
    with_keepalive_prefix,
    with_project_prefix,
)


def _scope(path: str, headers: list[tuple[bytes, bytes]] | None = None) -> dict:
    return {"type": "http", "path": path, "raw_path": path.encode(), "headers": headers or []}


def test_the_id_goes_after_the_project() -> None:
    url = with_project_prefix(with_keepalive_prefix("http://127.0.0.1:8787/v1", "ab_C-1"), "demo")
    assert url == "http://127.0.0.1:8787/p/demo/k/ab_C-1/v1"
    assert with_keepalive_prefix("http://127.0.0.1:8787/v1", None) == "http://127.0.0.1:8787/v1"
    assert with_keepalive_prefix("http://h/v1", "bad id/../x") == "http://h/v1"


def test_the_proxy_strips_it_into_the_header() -> None:
    scope = _scope("/p/demo/k/abc123/v1/responses")
    assert strip_project_path_prefix(scope) == "demo"
    assert scope["path"] == "/v1/responses"
    assert scope["raw_path"] == b"/v1/responses"
    assert (b"x-horizon-keepalive-id", b"abc123") in scope["headers"]

    scope = _scope("/k/abc123/v1/responses")
    assert strip_project_path_prefix(scope) is None
    assert scope["path"] == "/v1/responses"


def test_a_header_the_client_sent_wins() -> None:
    scope = _scope("/k/from-path/v1/responses", [(b"x-horizon-keepalive-id", b"from-header")])
    strip_project_path_prefix(scope)
    assert [v for k, v in scope["headers"] if k == b"x-horizon-keepalive-id"] == [b"from-header"]


def test_only_a_plain_id_is_taken() -> None:
    assert split_keepalive_path("/k/" + "x" * 65 + "/v1/responses")[0] is None
    assert split_keepalive_path("/k/a.b/v1/responses")[0] is None
    assert split_keepalive_path("/v1/k/abc/responses") == (None, "/v1/k/abc/responses")


def test_the_hosted_account_gate_accepts_the_path() -> None:
    # Authentication runs before the prefix is stripped.
    assert INFERENCE.fullmatch("/p/demo/k/abc123/v1/responses")
    assert INFERENCE.fullmatch("/k/abc123/v1/responses")
    assert AUXILIARY.fullmatch("/p/demo/k/abc123/v1/models")
    assert not INFERENCE.fullmatch("/k/abc123/admin")
