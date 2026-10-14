"""Synthetic, diagnostic-only backend for the desktop MCP spike.

This module deliberately has no filesystem, Horizon proxy, model, or persistence
dependencies.  The fixtures below are inert examples used to exercise the MCP
contract; they are not discovered from the user's machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Fixture:
    source_id: str
    kind: str
    text: str
    version: str = "fixture-v1"


class FixtureRangeError(ValueError):
    """A line selector that falls outside the immutable synthetic record."""


_FIXTURES = (
    Fixture(
        "fixture:note",
        "note",
        "# Synthetic note\n\nThis note exists only to exercise the diagnostic desktop MCP.\n",
    ),
    Fixture(
        "fixture:log",
        "log",
        "2026-01-01T00:00:00Z INFO synthetic fixture started\n"
        "2026-01-01T00:00:01Z WARN example event for search\n"
        "2026-01-01T00:00:02Z INFO synthetic fixture finished\n",
    ),
    Fixture(
        "fixture:json",
        "json",
        '{\n  "fixture": true,\n  "purpose": "diagnostic only",\n  "count": 3\n}\n',
    ),
)


class FixtureBackend:
    """Read-only access to a fixed set of tiny synthetic records."""

    def __init__(self) -> None:
        self._by_id = {fixture.source_id: fixture for fixture in _FIXTURES}

    def list_fixtures(self) -> tuple[Fixture, ...]:
        return _FIXTURES

    def get_fixture(self, source_id: str) -> Fixture | None:
        # Unknown identifiers are never interpreted as paths or opened.
        return self._by_id.get(source_id)

    @staticmethod
    def _matches(fixture: Fixture, query: str) -> list[dict[str, Any]]:
        """Return deterministic line matches without modifying fixture text."""
        if not query:
            return []
        matches = []
        for line_number, line in enumerate(fixture.text.splitlines(), start=1):
            if query.casefold() in line.casefold():
                matches.append({"line": line_number, "text": line})
        return matches

    def search(self, query: str, source_id: str | None = None) -> dict[str, Any]:
        candidates = (self._by_id.get(source_id),) if source_id else _FIXTURES
        hits = []
        for fixture in candidates:
            if fixture is None:
                continue
            if not query:
                # An empty query is a bounded source catalog so callers can
                # discover the synthetic opaque IDs before opening a fixture.
                hits.append(
                    {
                        "source_id": fixture.source_id,
                        "version": fixture.version,
                        "kind": fixture.kind,
                        "preview": fixture.text.splitlines()[0][:120],
                    }
                )
            else:
                for match in self._matches(fixture, query):
                    hits.append(
                        {
                            "source_id": fixture.source_id,
                            "version": fixture.version,
                            **match,
                        }
                    )
        return {
            "mode": "exact",
            "diagnostic_fixture": True,
            "hits": hits,
            "hit_count": len(hits),
            "coverage": "complete",
            "coverage_scope": "all synthetic fixtures"
            if source_id is None
            else "one synthetic fixture",
            "token_count": None,
            "token_count_status": "unknown; no tokenizer was used",
            "compression_ratio": None,
            "baseline_tokens": None,
        }

    def open(
        self,
        source_id: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> dict[str, Any] | None:
        fixture = self.get_fixture(source_id)
        if fixture is None:
            return None
        # Line selection is exact and bound to this immutable fixture version.
        if start_line is None and end_line is None:
            selected_text = fixture.text
            coverage = "complete fixture"
            selected_start = 1
            selected_end = max(1, len(fixture.text.splitlines()))
        else:
            if start_line is None or end_line is None:
                raise FixtureRangeError("both line bounds are required")
            lines = fixture.text.splitlines(keepends=True)
            first = start_line
            last = end_line
            if first < 1 or last < first or last > len(lines):
                raise FixtureRangeError("line range is outside fixture")
            selected_text = "".join(lines[first - 1 : last])
            coverage = "exact selected lines"
            selected_start, selected_end = first, last
        # The raw text is returned verbatim on every call, including repeats.
        return {
            "source_id": fixture.source_id,
            "version": fixture.version,
            "kind": fixture.kind,
            "text": selected_text,
            "mode": "exact",
            "coverage": coverage,
            "selected_lines": {"start": selected_start, "end": selected_end},
            "diagnostic_fixture": True,
            "token_count": None,
            "token_count_status": "unknown; no tokenizer was used",
            "baseline_tokens": None,
            "compression_ratio": None,
        }

    def retrieve(self, query: str, source_id: str | None = None) -> dict[str, Any]:
        """Return raw matching fixture lines; no summary or compression is done."""
        result = self.search(query, source_id)
        result["tool"] = "retrieve"
        result["mode"] = "exact"
        result["returned_text_is_raw"] = True
        return result

    def stats(self) -> dict[str, Any]:
        return {
            "diagnostic_fixture": True,
            "mode": "exact",
            "backend": "synthetic-fixtures",
            "fixture_count": len(_FIXTURES),
            "compression_applied": False,
            "compression_ratio": None,
            "baseline_tokens": None,
            "token_count_status": "unknown; no tokenizer was used",
            "claude_compatibility": "unverified",
            "phase_broker": "deferred",
            "production_sources_accessible": False,
        }
