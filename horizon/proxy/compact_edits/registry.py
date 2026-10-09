"""Explicit trusted bindings for a managed, single-worker Claude cohort.

There is no endpoint, filesystem discovery or default installation. The managed
launcher/source collector supplies identities, complete bytes, intent and an
independent digest. A request header can look up a registered account/session;
it cannot create one or change its workspace/source/qualification.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from typing import Any

from .cohort import ClaudeCohort
from .compiler import Candidate, CompactEditError, SourceSnapshot, compile_candidate
from .controller import CompactEditController, Qualification, Scope
from .journal import ReplayJournal


def managed_candidate(
    *,
    source_bytes: bytes,
    expected_sha256: str,
    provenance: str,
    path: str,
    native_path: str,
    table: str,
    template: str,
    keys: tuple[str, ...],
) -> Candidate:
    """Bridge independently captured bytes; never accept model/request evidence."""
    if (
        not isinstance(source_bytes, bytes)
        or len(source_bytes) > 512_000
        or not isinstance(provenance, str)
        or not provenance
        or len(provenance) > 512
        or hashlib.sha256(source_bytes).hexdigest() != expected_sha256
    ):
        raise CompactEditError("managed source provenance/digest required")
    try:
        snapshot = SourceSnapshot(path, native_path, source_bytes.decode("utf-8"))
    except UnicodeError as exc:
        raise CompactEditError("managed source must be UTF-8") from exc
    candidate = Candidate(snapshot, table, template, keys)
    compile_candidate(candidate)
    return candidate


class ManagedClaudeRegistry:
    """Operator API for isolated validation; not an economic certification API.

    This implementation requires one proxy worker and one owning registry. A
    per-conversation asyncio lock is not a distributed generation lease. Reject
    unsupported worker counts instead of advertising cross-worker safety.
    """

    def __init__(
        self,
        *,
        controller: CompactEditController,
        journal: ReplayJournal,
        worker_processes: int,
        max_sessions: int = 16,
    ):
        if worker_processes != 1 or max_sessions <= 0:
            raise CompactEditError(
                "managed Claude registry requires one worker and bounded sessions"
            )
        self.controller, self.journal = controller, journal
        self.max_sessions = max_sessions
        self._sessions: dict[tuple[str, str], tuple[ClaudeCohort, str]] = {}

    def install(self, app: Any) -> None:
        """Attach only via the operator API, checking the actual worker count."""
        if app.state.proxy.config.worker_processes != 1:
            raise CompactEditError("managed Claude registry requires one actual proxy worker")
        if app.state.compact_edit_cohort_resolver is not None:
            raise CompactEditError("do not replace an existing cohort registry")
        app.state.compact_edit_cohort_resolver = self.resolve

    def stop_admissions(self, reason: str) -> None:
        self.journal.stop_admissions(reason)

    def bind(
        self,
        *,
        scope: Scope,
        candidate: Candidate,
        qualification: Qualification,
        request_path: str = "/v1/messages",
        cold_boundary_ready: bool = False,
        resume_required: bool = False,
    ) -> ClaudeCohort:
        if self.journal.admissions_stopped() and self.journal.get(scope.key, "admission") is None:
            raise CompactEditError("operator stopped new managed admissions")
        if scope.provider != "anthropic" or qualification.contract.kind != "claude_edit":
            raise CompactEditError("registry handles only qualified Claude Messages")
        qualification.check(scope, candidate)
        compile_candidate(candidate)
        key = (scope.account, scope.conversation)
        if key in self._sessions or len(self._sessions) >= self.max_sessions:
            raise CompactEditError("session already bound or registry capacity reached")
        if not request_path.endswith("/v1/messages"):
            raise CompactEditError("exact managed Messages route required")
        binding = {
            "scope": asdict(scope),
            "request_path": request_path,
            "source_sha256": candidate.snapshot.sha256,
        }
        previous = self.journal.get(scope.key, "registry_binding")
        if previous is None:
            if resume_required:
                raise CompactEditError("managed binding missing on resume")
            self.journal.put(scope.key, "registry_binding", binding)
        elif previous != binding:
            raise CompactEditError("managed binding cannot change on resume")
        if resume_required and self.controller.restore_admission(scope, self.journal) is None:
            raise CompactEditError("admitted session lost its replay journal")
        cohort = ClaudeCohort(
            controller=self.controller,
            scope=scope,
            candidate=candidate,
            qualification=qualification,
            journal=self.journal,
            cold_boundary_ready=cold_boundary_ready,
            resume_required=resume_required,
        )
        self._sessions[key] = cohort, request_path
        return cohort

    def restore(self, *, scope: Scope, qualification: Qualification) -> ClaudeCohort:
        binding = self.journal.get(scope.key, "registry_binding")
        restored = self.controller.restore_admission(scope, self.journal)
        if binding is None or restored is None:
            raise CompactEditError("managed continuation state missing")
        return self.bind(
            scope=scope,
            candidate=restored.candidate,
            qualification=qualification,
            request_path=binding["request_path"],
            resume_required=True,
        )

    async def resolve(self, scope: dict[str, Any]) -> ClaudeCohort | None:
        account = scope.get("state", {}).get("account_user_id")
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])
        }
        entry = self._sessions.get((account, headers.get("x-horizon-session-id")))
        if entry is None:
            return None
        cohort, request_path = entry
        # Arbitrary provider overrides have not been qualified on this route.
        if scope.get("path") != request_path or any(
            name in headers
            for name in ("x-horizon-base-url", "x-horizon-provider", "x-horizon-model")
        ):
            if (
                self.journal.get(cohort.scope.key, "admission") is not None
                or cohort.resume_required
            ):
                raise CompactEditError("active managed route changed; reconcile required")
            return None
        return cohort
