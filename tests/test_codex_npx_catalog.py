"""Native Codex catalog negotiation reuses the authenticated /v1/models route."""
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from starlette.requests import Request

from horizon.providers.codex.model_metadata import handle_chatgpt_model_metadata
from horizon.proxy.account_analytics import AccountMiddleware


@pytest.mark.asyncio
async def test_native_catalog_preserves_model_metadata_and_keeps_gateway_secrets_local():
    calls = []

    class Client:
        async def get(self, url, **kwargs):
            calls.append((url, kwargs["headers"]))
            return httpx.Response(200, json={"models": [{"slug": "test-codex-model", "context_window": 123456}]})

    request = Request({"type": "http", "method": "GET", "path": "/v1/models", "query_string": b"client_version=0.163.0",
                       "headers": [(b"authorization", b"Bearer upstream-token"), (b"chatgpt-account-id", b"account"),
                                   (b"x-horizon-proxy-token", b"cs_live_test"), (b"x-horizon-model-catalog", b"codex")]})
    response = await handle_chatgpt_model_metadata(Client(), request, "/backend-api/models")
    assert response.status_code == 200
    models = json.loads(response.body)["models"]
    assert models[0]["slug"] == "test-codex-model" and models[0]["context_window"] == 123456
    assert "supports_search_tool" not in models[0]  # Do not invent native capabilities.
    [(url, headers)] = calls
    assert url.endswith("/backend-api/codex/models?client_version=0.163.0")
    assert headers["authorization"] == "Bearer upstream-token"
    assert not any(name.lower().startswith("x-horizon-") for name in headers)


@pytest.mark.asyncio
async def test_native_catalog_never_advertises_guessed_models_on_upstream_failure():
    class Client:
        async def get(self, url, **kwargs):
            return httpx.Response(503, text="unavailable")

    request = Request({"type": "http", "method": "GET", "path": "/v1/models", "query_string": b"",
                       "headers": [(b"chatgpt-account-id", b"account"), (b"x-horizon-model-catalog", b"codex")]})
    response = await handle_chatgpt_model_metadata(Client(), request, "/backend-api/models")
    assert response.status_code == 503
    assert "models" not in json.loads(response.body)


@pytest.mark.asyncio
@pytest.mark.parametrize("path,native,status,permission", [
    ("/k/test/v1/models", True, 200, "proxy:responses"),
    ("/v1/models", False, 403, "proxy:messages"),
    ("/v1/messages", True, 403, "proxy:messages"),
    ("/v1/messages/count_tokens", True, 403, "proxy:messages"),
])
async def test_responses_key_can_read_its_catalog_without_gaining_messages_access(path, native, status, permission):
    calls, sent = [], []

    async def authorize(key, scope):
        calls.append(scope)
        if scope != "proxy:responses":
            return 403, None
        return 200, {"user_id": str(uuid4()), "key_id": str(uuid4())}

    async def app(scope, receive, send):
        assert (b"x-horizon-proxy-token", b"cs_live_test") not in scope["headers"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    headers = [(b"x-horizon-proxy-token", b"cs_live_test")]
    if native:
        headers.append((b"x-horizon-model-catalog", b"codex"))
    scope = {"type": "http", "method": "POST" if path == "/v1/messages" else "GET", "path": path, "headers": headers}
    await AccountMiddleware(app, SimpleNamespace(enabled=True, authorize=authorize))(scope, receive, send)
    assert calls == [permission]
    assert sent[0]["status"] == status
