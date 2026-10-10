"""Pre-warm a Codex Responses WebSocket session in place (GPT-5.6+).

Codex talks to ``/v1/responses`` over one WebSocket and, with ``store: false``,
chains each turn to the previous one by ``previous_response_id``. That chain
lives only on the open upstream connection, so the HTTP pre-warm the cache
keeper uses for other OpenAI traffic cannot reach it (an unstored id is "not
found" over HTTP).

Measured 2026-10-10 on gpt-6.1-sol: a ``response.create`` sent on the same
connection with ``previous_response_id`` set to the last response, empty
``input`` and ``prompt_cache_options.prewarm: true`` bills only cache reads (no
writes, no output). The connection then holds only the pre-warm's response:
chaining to the earlier id fails with ``previous_response_not_found``, while
chaining to the pre-warm's id reads the cache as the real turn would have.

So a :class:`WsPrewarmChannel` sits on one relayed connection and

* sends the pre-warm frame when the keeper asks (never while the client's own
  turn is being prepared or answered),
* swallows the pre-warm's events so Codex never sees a response it did not ask
  for, and
* rewrites the ``previous_response_id`` of Codex's next turn from the id it
  knows to the pre-warm's id. The pre-warm adds no input or output, so the
  context the turn continues is the same.

The cache keeper decides when, how often and who pays (``OPENAI_RESPONSES_WS``
flavor); this class only does the wire work.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

#: Events that end a response on the wire.
TERMINAL_EVENTS = frozenset(
    {
        "response.completed",
        "response.failed",
        "response.incomplete",
        "response.cancelled",
        "error",
    }
)


class PrewarmUnavailable(Exception):
    """Nothing was sent: the connection is closed (``closed``) or in use."""

    def __init__(self, closed: bool) -> None:
        super().__init__("closed" if closed else "busy")
        self.closed = closed


def _inner(frame: dict[str, Any]) -> dict[str, Any]:
    inner = frame.get("response")
    return inner if isinstance(inner, dict) else frame


class WsPrewarmChannel:
    """Wire side of in-connection pre-warming for one upstream WebSocket."""

    def __init__(
        self,
        send: Callable[[str], Awaitable[None]],
        *,
        client_wait_seconds: float = 30.0,
    ) -> None:
        self._send = send
        self._client_wait = client_wait_seconds
        self.closed = False
        # A client turn is being prepared or answered: never pre-warm then.
        self._client_busy = False
        # The last response id the client received, and the id upstream holds
        # for the same context (they differ after a pre-warm).
        self._client_id: str | None = None
        self._upstream_id: str | None = None
        self._prewarm: asyncio.Future[tuple[int, dict[str, Any], str | None]] | None = None

    # -- client side ------------------------------------------------------

    def client_turn_started(self) -> None:
        """A ``response.create`` arrived from the client (before any rewriting)."""
        self._client_busy = True

    async def prepare_client_frame(self, msg: str) -> str:
        """Hold the turn while a pre-warm finishes, then chain it to what upstream holds."""
        prewarm = self._prewarm
        if prewarm is not None and not prewarm.done():
            try:
                await asyncio.wait_for(asyncio.shield(prewarm), self._client_wait)
            except Exception:  # never hold a turn for long
                pass
            if not prewarm.done():
                # Give up on it so the turn's own events reach the client; the
                # keeper books an unanswered pre-warm as a miss and stops.
                prewarm.set_result((0, {}, None))
        if not self._client_id or self._upstream_id in (None, self._client_id):
            return msg
        try:
            frame = json.loads(msg)
        except json.JSONDecodeError:
            return msg
        if not isinstance(frame, dict) or frame.get("type") != "response.create":
            return msg
        inner = _inner(frame)
        if inner.get("previous_response_id") != self._client_id:
            return msg
        inner["previous_response_id"] = self._upstream_id
        return json.dumps(frame)

    # -- upstream side ----------------------------------------------------

    def observe_upstream_event(self, event: dict[str, Any]) -> bool:
        """True when ``event`` belongs to a pre-warm and must not reach the client."""
        event_type = event.get("type")
        prewarm = self._prewarm
        if prewarm is not None and not prewarm.done():
            if event_type in TERMINAL_EVENTS:
                response = event.get("response")
                response = response if isinstance(response, dict) else {}
                usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
                status = 200 if event_type == "response.completed" else 400
                new_id = str(response["id"]) if status == 200 and response.get("id") else None
                if new_id:
                    self._upstream_id = new_id
                prewarm.set_result((status, usage, new_id))
            return True
        if event_type in TERMINAL_EVENTS:
            self._client_busy = False
            response = event.get("response")
            if event_type == "response.completed" and isinstance(response, dict):
                if response.get("id"):
                    self._client_id = self._upstream_id = str(response["id"])
        return False

    # -- keeper side ------------------------------------------------------

    async def prewarm(self, template: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Send one pre-warm on this connection; ``(status, usage)`` as the keeper expects.

        ``template`` is the last turn as sent upstream; everything the cache is
        keyed by stays as it was.
        """
        if self.closed:
            raise PrewarmUnavailable(closed=True)
        if self._client_busy or self._upstream_id is None:
            raise PrewarmUnavailable(closed=False)
        if self._prewarm is not None and not self._prewarm.done():
            raise PrewarmUnavailable(closed=False)
        body = copy.deepcopy(template)
        body.pop("stream", None)
        body["previous_response_id"] = self._upstream_id
        body["input"] = []
        options = body.get("prompt_cache_options")
        body["prompt_cache_options"] = {
            **(options if isinstance(options, dict) else {}),
            "prewarm": True,
        }
        future: asyncio.Future[tuple[int, dict[str, Any], str | None]] = (
            asyncio.get_running_loop().create_future()
        )
        self._prewarm = future
        try:
            await self._send(json.dumps({"type": "response.create", **body}))
        except Exception:
            self._prewarm = None
            self.close()
            raise PrewarmUnavailable(closed=True) from None
        status, usage, _ = await future
        return status, usage

    def close(self) -> None:
        self.closed = True
        prewarm = self._prewarm
        if prewarm is not None and not prewarm.done():
            prewarm.set_exception(ConnectionError("upstream websocket closed"))
            # Retrieved here so an unawaited failure is not logged as lost.
            prewarm.exception()
