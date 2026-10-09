"""Opted-in operator cohort at the HTTP boundary, before inner prefix caching.

AccountMiddleware must run outside this boundary. A trusted resolver supplies
an authenticated, attested workspace/conversation cohort; headers never create
qualifications. With no resolver, native traffic is streamed unchanged.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from horizon.proxy.anthropic_wire import is_safeguard_capable_request, render_anthropic_sse_response

from .compiler import Candidate, CompactEditError
from .controller import CompactEditController, Qualification, Scope
from .journal import ReplayJournal
from .wire import VIRTUAL_TOOL

Invoke = Callable[[dict[str, Any]], Awaitable["Reply"]]


def has_attested_read(request: dict[str, Any], candidate: Candidate) -> bool:
    """Bind a complete numbered Read result to the managed snapshot's SHA.

    This does not infer EOF from a line count: the independently attested source
    hash is required. Unknown/multi-block/partial formats conservatively fail.
    """
    latest_call = None
    complete_read = False
    ready = False
    expected_paths = {candidate.snapshot.path, candidate.snapshot.native_path}
    for message in request.get("messages", []):
        content = message.get("content", []) if isinstance(message, dict) else []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "Read":
                args = block.get("input", {})
                if isinstance(args, dict) and args.get("file_path") in expected_paths:
                    # A newer read supersedes old evidence, even if partial,
                    # pending, failed or in an unknown output format.
                    latest_call = block.get("id")
                    complete_read = (
                        isinstance(latest_call, str)
                        and bool(latest_call)
                        and not args.get("offset")
                    )
                    ready = False
            elif (
                latest_call is not None
                and block.get("type") == "tool_result"
                and block.get("tool_use_id") == latest_call
            ):
                ready = False
                if not complete_read or block.get("is_error"):
                    continue
                text = block.get("content")
                if not isinstance(text, str):
                    continue
                lines = []
                for index, line in enumerate(text.splitlines(), 1):
                    match = re.fullmatch(r"(\d+)\t(.*)", line)
                    if not match or int(match[1]) != index:
                        break
                    lines.append(match[2])
                else:
                    decoded = "\n".join(lines)
                    # Native Read renderers may omit the final empty numbered
                    # line. The managed complete snapshot (which requires LF
                    # termination) decides the match, not an inferred EOF.
                    if any(
                        hashlib.sha256(value.encode("utf-8")).hexdigest()
                        == candidate.snapshot.sha256
                        for value in (decoded, decoded + "\n")
                    ):
                        ready = True
    return ready


@dataclass(frozen=True)
class Reply:
    status: int
    headers: list[tuple[bytes, bytes]]
    body: bytes


class ClaudeCohort:
    """One bounded operator-approved conversation, not a customer-wide switch."""

    def __init__(
        self,
        *,
        controller: CompactEditController,
        scope: Scope,
        candidate: Candidate,
        qualification: Qualification,
        journal: ReplayJournal,
        cold_boundary_ready: bool = False,
        resume_required: bool = False,
        queue_timeout_seconds: float = 2.0,
        request_timeout_seconds: float = 120.0,
        max_waiters: int = 4,
    ):
        if qualification.contract.kind != "claude_edit":
            raise CompactEditError("this cohort handles only Claude Messages")
        qualification.check(scope, candidate)
        self.controller, self.scope, self.candidate = controller, scope, candidate
        self.qualification, self.journal = qualification, journal
        # Only the managed session registry can establish a cold boundary or a
        # required continuation. Missing journal state proves neither.
        self.cold_boundary_ready, self.resume_required = cold_boundary_ready, resume_required
        self.lock = asyncio.Lock()
        if (
            any(
                not math.isfinite(v) or v <= 0
                for v in (queue_timeout_seconds, request_timeout_seconds)
            )
            or max_waiters < 1
        ):
            raise CompactEditError("cohort requires bounded positive queue/request limits")
        self.queue_timeout_seconds = queue_timeout_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.max_waiters, self.waiters = max_waiters, 0
        self.attempt_usage: list[dict[str, Any]] = []
        self.recoveries = 0

    async def respond(self, request: dict[str, Any], invoke: Invoke) -> Reply:
        """Buffer upstream JSON, publish native JSON/SSE; at most one recovery.

        Each invocation goes through the inner proxy's ordinary usage settlement.
        A discarded preview is never inserted as a fictitious client tool result.
        """
        if self.waiters >= self.max_waiters:
            raise CompactEditError("compact conversation queue is full")
        self.waiters += 1
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=self.queue_timeout_seconds)
        finally:
            self.waiters -= 1
        try:
            return await asyncio.wait_for(
                self._respond_locked(request, invoke), timeout=self.request_timeout_seconds
            )
        finally:
            self.lock.release()

    @staticmethod
    async def _durable_work(function: Any, *args: Any, **kwargs: Any) -> Any:
        # Cancellation does not stop a thread. Retain the conversation lock
        # until an in-flight journal mutation settles, before reporting timeout
        # or disconnect. The journal itself has a bounded SQLite busy timeout.
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
                # Repeated cancellation must not release the lock while the
                # thread can still commit a publication/reservation.
                if task.cancelled():
                    raise
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _respond_locked(self, request: dict[str, Any], invoke: Invoke) -> Reply:
        choice = request.get("tool_choice")
        if request.get("model") != self.scope.model or (
            choice is not None and (not isinstance(choice, dict) or choice.get("type") != "auto")
        ):
            if self.journal.get(self.scope.key, "admission") is not None:
                raise CompactEditError("active cohort changed route/tool selection; drain required")
            return await invoke(request)
        provider = copy.deepcopy(request)
        stream = bool(provider.get("stream"))
        provider["stream"] = False
        provider["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
        saved = self.journal.get(self.scope.key, "admission")
        admission = await self._durable_work(
            self.controller.prepare,
            provider,
            scope=self.scope,
            candidate=self.candidate,
            qualification=self.qualification,
            journal=self.journal,
            cold_boundary=saved is None and self.cold_boundary_ready,
            execution_guard_ready=True,
            resume_required=self.resume_required,
        )
        if admission.turn is None:
            return await invoke(request)
        state = self.journal.get(self.scope.key, "admission")
        if state and state.get("reserved_call"):
            record = self.journal.get(self.scope.key, "call:" + state["reserved_call"])
            if record is None or record.get("native_result") is None:
                raise CompactEditError(
                    "published call has no client result; reconcile before another request"
                )
        reply = await invoke(admission.request)
        if reply.status != 200:
            return reply
        message = self._decode(reply)
        read_ready = has_attested_read(request, self.candidate)
        try:
            if not read_ready and any(
                block.get("name") == VIRTUAL_TOOL
                for block in message.get("content", [])
                if isinstance(block, dict)
            ):
                raise CompactEditError("compact edit needs an attested native Read first")
            client = await self._durable_work(
                self.controller.translate_response, message, admission.turn
            )
        except CompactEditError:
            # A reserved call might already have escaped before a restart.
            # Do not retry it. Reconcile uncertain native execution instead.
            state = self.journal.get(self.scope.key, "admission")
            if state and state.get("reserved_call"):
                raise CompactEditError(
                    "reserved operation requires delivery reconciliation"
                ) from None
            retry = copy.deepcopy(admission.request)
            recovery_tool = "Edit" if read_ready else "Read"
            retry["tool_choice"] = {
                "type": "tool",
                "name": recovery_tool,
                "disable_parallel_tool_use": True,
            }
            self.recoveries += 1
            reply = await invoke(retry)
            if reply.status != 200:
                return reply
            message = self._decode(reply)
            if any(
                block.get("name") == VIRTUAL_TOOL
                for block in message.get("content", [])
                if isinstance(block, dict)
            ):
                raise CompactEditError("native recovery emitted a virtual tool") from None
            if any(
                block.get("type") == "tool_use" and block.get("name") != recovery_tool
                for block in message.get("content", [])
                if isinstance(block, dict)
            ):
                raise CompactEditError("native recovery ignored forced tool selection") from None
            client = await self._durable_work(
                self.controller.translate_response, message, admission.turn
            )
        data = (
            b"".join(render_anthropic_sse_response(client))
            if stream
            else json.dumps(client, ensure_ascii=False).encode()
        )
        headers = [
            (key, value)
            for key, value in reply.headers
            if key.lower()
            not in {
                b"content-length",
                b"content-type",
                b"content-encoding",
                b"transfer-encoding",
                b"etag",
            }
        ]
        headers.extend(
            [
                (b"content-type", b"text/event-stream" if stream else b"application/json"),
                (b"content-length", str(len(data)).encode()),
            ]
        )
        return Reply(200, headers, data)

    def _decode(self, reply: Reply) -> dict[str, Any]:
        try:
            message = json.loads(reply.body)
        except (ValueError, UnicodeError) as exc:
            raise CompactEditError("cohort requires complete upstream JSON") from exc
        if not isinstance(message, dict):
            raise CompactEditError("cohort requires a message object")
        usage = message.get("usage")
        if not isinstance(usage, dict) or any(
            not isinstance(usage.get(key), int)
            or isinstance(usage.get(key), bool)
            or usage[key] < 0
            for key in ("input_tokens", "output_tokens")
        ):
            raise CompactEditError("qualified response requires measured input/output usage")
        if isinstance(usage, dict):
            # Bounded counters only; no source or model text retained here.
            self.attempt_usage.append(
                {
                    key: value
                    for key, value in usage.items()
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                }
            )
            self.attempt_usage = self.attempt_usage[-32:]
        if message.get("model") != self.scope.model:
            raise CompactEditError("upstream model does not match the qualified route")
        return message


class CompactCohortMiddleware:
    """Install inside account authentication and outside compression/prefix state."""

    def __init__(
        self,
        app: Any,
        *,
        resolve: Callable[[dict[str, Any]], Awaitable[ClaudeCohort | None]],
        policy_allowed: Callable[[dict[str, Any]], bool] = lambda scope: True,
    ):
        self.app, self.resolve = app, resolve
        self.policy_allowed = policy_allowed

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or not scope.get("path", "").rstrip("/").endswith("/messages")
        ):
            return await self.app(scope, receive, send)
        try:
            cohort = await self.resolve(scope)
        except (CompactEditError, OSError, sqlite3.Error):
            return await self._failure(send)
        if cohort is None:
            return await self.app(scope, receive, send)
        if scope.get("state", {}).get("account_user_id") != cohort.scope.account:
            # Authenticated account mismatch never selects another tenant's state.
            return await self.app(scope, receive, send)

        async def native_or_stop(input_receive: Any) -> None:
            # Unsupported new admissions are native. An active provider catalog
            # cannot disappear even when policy or transport changes.
            try:
                active = cohort.journal.get(cohort.scope.key, "admission") is not None
            except (CompactEditError, OSError, sqlite3.Error):
                return await self._failure(send)
            if active or cohort.resume_required:
                return await self._failure(send)
            return await self.app(scope, input_receive, send)

        if not self.policy_allowed(scope):
            return await native_or_stop(receive)
        headers = dict(scope.get("headers", []))
        if headers.get(b"content-encoding", b"identity").lower() != b"identity":
            return await native_or_stop(receive)
        frames, data = [], bytearray()
        deadline = asyncio.get_running_loop().time() + cohort.request_timeout_seconds
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return await self._failure(send, status=504)
            try:
                frame = await asyncio.wait_for(receive(), timeout=remaining)
            except TimeoutError:
                return await self._failure(send, status=504)
            frames.append(frame)
            if len(frames) > 1024:
                return await self._failure(send)
            if frame["type"] == "http.disconnect":
                return
            data.extend(frame.get("body", b""))
            if len(data) > 2_000_000 or not frame.get("more_body"):
                break

        async def replay() -> dict[str, Any]:
            return frames.pop(0) if frames else await receive()

        try:
            body = json.loads(data)
        except (ValueError, UnicodeError):
            return await native_or_stop(replay)
        if len(data) > 2_000_000 or not isinstance(body, dict):
            return await native_or_stop(replay)
        if is_safeguard_capable_request(
            body, headers.get(b"anthropic-beta", b"").decode("latin-1")
        ):
            # Auto-mode classifier payloads/proofs are separate capabilities.
            # Preserve native forwarding; never expand a signed compact verdict.
            return await native_or_stop(replay)

        async def invoke(provider: dict[str, Any]) -> Reply:
            encoded = json.dumps(provider, ensure_ascii=False).encode()
            inner = dict(scope)
            inner["headers"] = [
                (key, value)
                for key, value in scope.get("headers", [])
                if key.lower() != b"content-length"
            ] + [(b"content-length", str(len(encoded)).encode())]
            sent = False
            response_status, response_headers, chunks = 500, [], bytearray()
            started, finished = False, False

            async def input_frame() -> dict[str, Any]:
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": encoded, "more_body": False}
                return await receive()

            async def capture(frame: dict[str, Any]) -> None:
                nonlocal response_status, response_headers, started, finished
                if frame["type"] == "http.response.start":
                    if started:
                        raise CompactEditError("duplicate inner response start")
                    started = True
                    response_status, response_headers = frame["status"], frame.get("headers", [])
                elif frame["type"] == "http.response.body":
                    if not started or finished:
                        raise CompactEditError("invalid inner response framing")
                    chunks.extend(frame.get("body", b""))
                    finished = not frame.get("more_body", False)
                    if len(chunks) > 4_000_000:
                        raise CompactEditError("cohort response exceeds buffer bound")

            await self.app(inner, input_frame, capture)
            if not started or not finished:
                raise CompactEditError("incomplete inner response")
            return Reply(response_status, response_headers, bytes(chunks))

        try:
            reply = await cohort.respond(body, invoke)
        except TimeoutError:
            return await self._failure(send, status=504)
        except (CompactEditError, OSError, sqlite3.Error):
            return await self._failure(send)
        await send(
            {"type": "http.response.start", "status": reply.status, "headers": reply.headers}
        )
        await send({"type": "http.response.body", "body": reply.body, "more_body": False})

    @staticmethod
    async def _failure(send: Any, *, status: int = 502) -> None:
        encoded = json.dumps(
            {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": "Compact edit could not be completed; no tool call was published. Delivery reconciliation may be required.",
                },
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(encoded)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": encoded, "more_body": False})
