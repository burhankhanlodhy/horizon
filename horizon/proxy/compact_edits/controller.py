"""Always-present admission controller and qualified buffered adapter API.

Qualification is trusted operator configuration, never request headers or model
arguments. Stock forwarding has no qualified route yet; observation does not
rewrite requests, report savings, execute files or create a journal.
"""

from __future__ import annotations

import copy
import hashlib
import math
import sqlite3
import time
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

from .compiler import Candidate, CompactEditError, SourceSnapshot, compile_candidate
from .journal import ReplayJournal
from .wire import (
    KINDS,
    VIRTUAL_TOOL,
    NativeContract,
    call_id,
    canonical,
    expand_call,
    fingerprint,
    is_virtual,
    response_items,
    restore_call,
    signature,
    virtual_arguments,
    virtual_definition,
)


@dataclass(frozen=True)
class Scope:
    account: str
    conversation: str
    workspace: str
    provider: str
    model: str

    @property
    def key(self) -> str:
        values = asdict(self)
        if any(
            not isinstance(value, str) or not value or len(value) > 512 for value in values.values()
        ):
            raise CompactEditError(
                "authenticated account/conversation/workspace/route scope required"
            )
        return hashlib.sha256(canonical(values).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CostBounds:
    # Full-session bounds include schema/receipt, unused admission, catalog
    # lifetime/cache changes, expansion guards, and bounded recovery costs.
    native_lower_usd: float
    compact_upper_usd: float

    def profitable(self) -> bool:
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            for value in (self.native_lower_usd, self.compact_upper_usd)
        ):
            return False
        saving = self.native_lower_usd - self.compact_upper_usd
        return (
            self.native_lower_usd > 0 and saving >= 0.003 and saving / self.native_lower_usd >= 0.15
        )


@dataclass(frozen=True)
class Qualification:
    """Attestation from a completed native-client integration benchmark.

    Must include permission, exact source decoding, guarded execution, replay,
    failure recovery and whole-session cost controls. Not created by the proxy
    from a tool name, model family or the experimental MCP pilot.
    """

    proof_id: str
    provider: str
    model: str
    contract: NativeContract
    workspace: str
    source_path: str
    native_path: str
    module_contract: str
    version_guard: str
    expires_at: float
    costs: CostBounds

    def check(self, scope: Scope, candidate: Candidate) -> None:
        required_guard = (
            "exact_full_source_match"
            if self.contract.kind == "claude_edit"
            else "atomic_client_sha256"
        )
        if (
            not self.proof_id
            or self.contract.kind not in KINDS
            or self.provider != scope.provider
            or self.model != scope.model
            or self.workspace != scope.workspace
            or self.source_path != candidate.snapshot.path
            or self.native_path != candidate.snapshot.native_path
            or self.module_contract != "horizon-model-pricing-literals-v1"
            or self.version_guard != required_guard
            or not math.isfinite(self.expires_at)
            or self.expires_at <= time.time()
        ):
            raise CompactEditError("route/source/native guard has no current qualification")
        if not self.costs.profitable():
            raise CompactEditError("full-session cost bounds do not meet admission thresholds")


@dataclass(frozen=True)
class PreparedTurn:
    scope: Scope
    candidate: Candidate
    contract: NativeContract
    receipt: str
    journal: ReplayJournal
    compact_available: bool


@dataclass(frozen=True)
class Admission:
    request: dict[str, Any]
    turn: PreparedTurn | None
    reason: str


@dataclass(frozen=True)
class RestoredAdmission:
    candidate: Candidate
    contract: NativeContract
    reserved_call: str | None


class CompactEditController:
    def __init__(self) -> None:
        self._decisions: Counter[str] = Counter()
        self._clients: Counter[str] = Counter()
        self._translated = 0

    def observe_outcome(self, outcome: Any) -> None:
        client = str(getattr(outcome, "client", "") or "").lower()
        family = "codex" if "codex" in client else "claude" if "claude" in client else "other"
        self._clients[family] += 1
        self._decisions["observed_native_forwarding"] += 1

    def stats(self) -> dict[str, Any]:
        return {
            "controller_active": True,
            "forwarding_stage": "qualified_adapter_api"
            if self._translated
            else "native_only_pending_qualification",
            "adapter_formats": list(KINDS),
            "supported_transport": "buffered_json",
            "credited_savings_usd": 0.0,
            "decisions": dict(self._decisions),
            "observed_clients": dict(self._clients),
        }

    def restore_admission(self, scope: Scope, journal: ReplayJournal) -> RestoredAdmission | None:
        """Recover the original snapshot/catalog; this is not permission to retry."""
        saved = journal.get(scope.key, "admission")
        if saved is None:
            return None
        try:
            value = saved["candidate"]
            candidate = Candidate(
                SourceSnapshot(**value["snapshot"]),
                value["table"],
                value["template"],
                tuple(value["keys"]),
            )
            contract = NativeContract(**saved["contract"])
            expected = fingerprint(
                {"scope": scope.key, "candidate": asdict(candidate), "contract": asdict(contract)}
            )
            if saved["receipt"] != expected or contract.kind not in KINDS:
                raise CompactEditError("durable admission fingerprint mismatch")
            compile_candidate(candidate)
            reserved = saved["reserved_call"]
            if reserved is not None and (not isinstance(reserved, str) or not reserved):
                raise CompactEditError("invalid durable reservation")
            return RestoredAdmission(candidate, contract, reserved)
        except (KeyError, TypeError, ValueError) as exc:
            raise CompactEditError("invalid durable admission") from exc

    def prepare(
        self,
        request: dict[str, Any],
        *,
        scope: Scope,
        candidate: Candidate,
        qualification: Qualification | None,
        journal: ReplayJournal | None,
        cold_boundary: bool,
        execution_guard_ready: bool,
        resume_required: bool = False,
    ) -> Admission:
        """Call before prefix freezing/cache optimization or upstream generation.

        ``execution_guard_ready`` is supplied only by a trusted managed route.
        For patches it asserts a client-local compare-and-apply SHA fence; stock
        fuzzy apply_patch alone does not meet it. Keep native tools available.
        """
        reason = "unqualified_route"
        saved = None
        try:
            scope_key = scope.key
            if journal is not None:
                saved = journal.get(scope_key, "admission")
            if resume_required and saved is None:
                raise CompactEditError("admitted session lost its replay journal")
            if qualification is None or journal is None:
                raise CompactEditError(reason)
            reason = "transport_or_guard"
            if request.get("stream") is not False or not execution_guard_ready:
                raise CompactEditError(reason)
            if any(
                request.get(key) for key in ("previous_response_id", "conversation", "background")
            ):
                raise CompactEditError("incremental/background Responses are not qualified")
            if request.get("model") != scope.model:
                raise CompactEditError("scope must identify the actual routed model")
            reason = "qualification_or_economics"
            qualification.check(scope, candidate)
            contract = qualification.contract
            reason = "native_contract"
            contract.validate(request.get("tools"))
            if any(tool.get("name") == VIRTUAL_TOOL for tool in request["tools"]):
                raise CompactEditError("reserved tool name collision")
            # Prevent simultaneous native edits from racing the source snapshot.
            if contract.kind == "claude_edit":
                choice = request.get("tool_choice") or {}
                if (
                    not isinstance(choice, dict)
                    or choice.get("type") != "auto"
                    or choice.get("disable_parallel_tool_use") is not True
                    or set(choice) != {"type", "disable_parallel_tool_use"}
                ):
                    raise CompactEditError("Claude route must disable parallel tool use")
            else:
                if request.get("tool_choice") not in (None, "auto", {"type": "auto"}):
                    raise CompactEditError("forced/disabled tool selection is not qualified")
                if request.get("parallel_tool_calls") is not False:
                    raise CompactEditError("Responses route must disable parallel tool calls")
            reason = "unsupported_candidate"
            compile_candidate(candidate)
            candidate_data = asdict(candidate)
            if saved is None:
                reason = "warm_catalog"
                if not cold_boundary:
                    raise CompactEditError("admit only at a certified cold boundary")
                receipt = fingerprint(
                    {"scope": scope_key, "candidate": candidate_data, "contract": asdict(contract)}
                )
                saved = {
                    "candidate": candidate_data,
                    "contract": asdict(contract),
                    "receipt": receipt,
                    "reserved_call": None,
                }
                reason = "journal_unavailable"
                journal.put(scope_key, "admission", saved)
            else:
                # A receipt-bound tool catalog remains byte-stable for the session.
                # A new candidate requires a new certified conversation/boundary.
                if canonical(saved["candidate"]) != canonical(candidate_data) or saved[
                    "contract"
                ] != asdict(contract):
                    raise CompactEditError("active catalog cannot change mid-session")
                receipt = saved["receipt"]
            definition = virtual_definition(
                contract.kind,
                receipt,
                candidate.table,
                candidate.template,
                candidate.keys,
                path=candidate.snapshot.path,
                source_sha256=candidate.snapshot.sha256,
            )
            reason = "replay_mismatch"
            body = self.normalize_replay(request, scope=scope, kind=contract.kind, journal=journal)
            body["tools"].append(definition)
            turn = PreparedTurn(
                scope, candidate, contract, receipt, journal, not bool(saved["reserved_call"])
            )
            self._decisions["admitted"] += 1
            return Admission(body, turn, "admitted")
        except (CompactEditError, OSError, sqlite3.Error) as exc:
            self._decisions[reason] += 1
            if saved is not None or resume_required:
                # A failed continuation cannot silently remove its admitted
                # catalog/replay state. The caller must drain/recover the route.
                raise CompactEditError(
                    "active compact session requires recovery: " + str(exc)
                ) from exc
            return Admission(copy.deepcopy(request), None, reason + ": " + str(exc))

    def translate_response(self, response: dict[str, Any], turn: PreparedTurn) -> dict[str, Any]:
        """Buffer completely, validate, persist correspondence, then publish.

        Exceptions require bounded *internal* native recovery before publishing
        anything. Never forward the original virtual call or invent success.
        This method does not retry calls, run a model, or execute a client tool.
        """
        body = copy.deepcopy(response)
        items = response_items(body, turn.contract.kind)
        compact = [item for item in items if is_virtual(item, turn.contract.kind)]
        if any(
            item.get("name") == VIRTUAL_TOOL and not is_virtual(item, turn.contract.kind)
            for item in items
        ):
            raise CompactEditError("unexpected virtual call envelope")
        if not compact:
            return body
        other_calls = [
            item
            for item in items
            if (
                item.get("type") in {"tool_use", "server_tool_use"}
                or (isinstance(item.get("type"), str) and item["type"].endswith("_call"))
            )
            and item not in compact
        ]
        if len(compact) != 1 or other_calls or not turn.compact_available:
            raise CompactEditError(
                "mixed, repeated or parallel compact operations require native recovery"
            )
        item = compact[0]
        if turn.contract.kind == "claude_edit":
            if body.get("stop_reason") != "tool_use":
                raise CompactEditError("compact Claude call requires a tool_use stop")
        elif (
            item.get("status", "completed") != "completed"
            or not isinstance(item.get("id"), str)
            or not item["id"]
        ):
            raise CompactEditError("compact Responses call is incomplete")
        identifier = call_id(item, turn.contract.kind)
        arguments = virtual_arguments(item, turn.contract.kind)
        expected = {
            "receipt": turn.receipt,
            "table": turn.candidate.table,
            "template": turn.candidate.template,
            "keys": list(turn.candidate.keys),
        }
        if arguments != expected:
            raise CompactEditError("operation is not bound to the admitted intent/source receipt")
        edit = compile_candidate(turn.candidate)
        native = expand_call(item, turn.contract, edit)
        record = {
            "kind": turn.contract.kind,
            "source_sha256": edit.snapshot.sha256,
            "contract_sha256": turn.contract.definition_sha256,
            "provider": item,
            "client": native,
            "client_signature": signature(native, turn.contract.kind),
        }
        # Reservation and mapping commit atomically before the native call can
        # escape. An uncertain delivery must be reconciled, never blindly retried.
        turn.journal.record_call(turn.scope.key, identifier, record)
        items[items.index(item)] = native
        self._decisions["translated"] += 1
        self._translated += 1
        return body

    def normalize_replay(
        self, request: dict[str, Any], *, scope: Scope, kind: str, journal: ReplayJournal
    ) -> dict[str, Any]:
        """Restore provider calls/results before prefix comparison or forwarding.

        Full-history Messages and Responses input arrays only. IDs and native
        result content are unchanged. Results remain actual client reports.
        """
        if kind not in KINDS:
            raise CompactEditError("unsupported replay format")
        body = copy.deepcopy(request)
        scope_key = scope.key
        if kind == "claude_edit":
            messages = body.get("messages")
            if not isinstance(messages, list):
                raise CompactEditError("expected full Claude messages history")
            lists = [
                message["content"]
                for message in messages
                if isinstance(message, dict) and isinstance(message.get("content"), list)
            ]
        else:
            items = body.get("input")
            if not isinstance(items, list):
                raise CompactEditError("expected a Responses input item array")
            lists = [items]
        for items in lists:
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")
                if kind == "claude_edit" and item_type == "tool_use":
                    identifier = item.get("id")
                elif kind != "claude_edit" and item_type in {"function_call", "custom_tool_call"}:
                    identifier = item.get("call_id")
                elif kind == "claude_edit" and item_type == "tool_result":
                    identifier = item.get("tool_use_id")
                elif kind != "claude_edit" and item_type in {
                    "function_call_output",
                    "custom_tool_call_output",
                }:
                    identifier = item.get("call_id")
                else:
                    continue
                if not isinstance(identifier, str):
                    continue
                record = journal.get(scope_key, "call:" + identifier)
                if record is None:
                    continue
                if record["kind"] != kind:
                    raise CompactEditError("replay contract mismatch")
                if item_type in {"tool_use", "function_call", "custom_tool_call"}:
                    if signature(item, kind) != record["client_signature"]:
                        raise CompactEditError(
                            "replayed native arguments differ from published call"
                        )
                    items[index] = restore_call(item, record["provider"], kind)
                elif kind == "codex_custom_patch":
                    if item_type != "custom_tool_call_output":
                        raise CompactEditError("expected actual custom patch result")
                    items[index] = dict(item, type="function_call_output")
        return body
