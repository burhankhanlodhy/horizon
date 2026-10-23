"""Team authorization follows immutable key ownership, including long-lived sockets."""
import asyncio
from uuid import uuid4

from horizon.proxy.account_analytics import AccountMiddleware, account_id


class Service:
    enabled = True
    runtime_id = str(uuid4())
    def __init__(self, owners):
        self.owners = iter(owners)
        self.events = []
    async def authorize(self, *_):
        owner = next(self.owners)
        return (200, owner) if owner else (403, None)
    def _enqueue(self, event):
        self.events.append(event)


def identity():
    return {"user_id": str(uuid4()), "key_id": str(uuid4()), "team_id": str(uuid4()),
            "membership_id": str(uuid4()), "compression_allowed": True}


def test_proxy_namespaces_team_learning_and_attributes_outcomes():
    owner = identity()
    service = Service([owner])
    observed = {}
    async def app(scope, receive, send):
        observed["namespace"] = account_id()
        observed["headers"] = scope["headers"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})
    async def receive():
        return {"type": "http.request", "body": b"{}"}
    async def send(_):
        pass
    scope = {"type": "http", "method": "POST", "path": "/v1/responses",
             "headers": [(b"x-horizon-proxy-token", b"cs_live_fake"),
                         (b"x-contextshrink-team", b"spoofed-team")]}
    asyncio.run(AccountMiddleware(app, service)(scope, receive, send))
    assert observed["namespace"] == "team_" + owner["membership_id"]
    assert not any(k in (b"x-contextshrink-team", b"x-horizon-proxy-token") for k, v in observed["headers"])
    assert service.events
    for event in service.events:
        assert event["team_id"] == owner["team_id"] and event["membership_id"] == owner["membership_id"]


def test_websocket_closes_when_membership_is_removed_or_changed():
    for replacement in (None, {**identity()}):
        owner = identity()
        service = Service([owner, replacement])
        sent, received = [], []
        async def app(scope, receive, send, received=received):
            await send({"type": "websocket.accept"})
            received.append(await receive())
        async def receive():
            return {"type": "websocket.receive", "text": "{}"}
        async def send(message, sent=sent):
            sent.append(message)
        scope = {"type": "websocket", "path": "/v1/responses", "headers": [(b"x-horizon-proxy-token", b"cs_live_fake")]}
        asyncio.run(AccountMiddleware(app, service)(scope, receive, send))
        assert received == [{"type": "websocket.disconnect", "code": 1008}]
        assert {"type": "websocket.close", "code": 1008} in sent
