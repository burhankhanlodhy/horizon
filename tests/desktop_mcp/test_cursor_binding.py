"""Negative and positive tests for paging-cursor binding in the local backend.

A continuation cursor is an opaque capability. Every continuation must be
rejected unless it is presented with the exact source version, mode, query,
token budget, selector, policy version, and signer that issued it.

Continuations pass the issuing snapshot's ``source_id`` (the form a client
uses after a first page), so each rejection is caused by the binding field
under test rather than by a source-path mismatch.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import secrets
from dataclasses import dataclass

import pytest

from horizon.desktop_mcp import policies
from horizon.desktop_mcp.local_backend import LocalBackend, LocalBackendError
from horizon.desktop_mcp.policies import CursorSigner, PolicyError, present_snapshot
from horizon.desktop_mcp.source_access import OpenedSource, SourceAccessError


@dataclass
class _Record:
    source: str
    text: str
    root_key: str = "approved-root"
    identity: str = "volume:file-id"
    digest: str = "digest-v1"

    def opened(self) -> OpenedSource:
        return OpenedSource(
            root_key=self.root_key,
            relative_path="notes.txt",
            text=self.text,
            digest=self.digest,
            byte_count=len(self.text.encode("utf-8")),
            identity=self.identity,
        )


class _Reader:
    def __init__(self, records: list[_Record]) -> None:
        self.records = {record.source: record for record in records}
        self.catalog_truncated = False
        self.denied: set[str] = set()

    def catalog(self, query: str = "", limit: int = 5) -> list[dict[str, str]]:
        values = list(self.records.values())
        if query:
            values = [item for item in values if query.casefold() in item.source.casefold()]
        self.catalog_truncated = len(values) > limit
        return [
            {"source": item.source, "name": item.source.rsplit("/", 1)[-1], "kind": "txt"}
            for item in values[:limit]
        ]

    def read(self, source: str) -> OpenedSource:
        if source in self.denied:
            raise SourceAccessError("source_unavailable")
        try:
            return self.records[source].opened()
        except KeyError:
            raise SourceAccessError("source_unapproved") from None

    def check_access(self, source: str) -> str:
        if source in self.denied or source not in self.records:
            raise SourceAccessError("source_unavailable")
        return self.records[source].root_key


def _backend(*records: _Record) -> tuple[LocalBackend, _Reader]:
    reader = _Reader(list(records))
    backend = LocalBackend(reader, result_token_budget=512, max_result_bytes=4096)
    return backend, reader


def _rows(count: int = 120) -> str:
    # 120 lines of 81 bytes each: several exact pages under a 512-token budget.
    return "".join(f"row {index:04d}: " + ("x" * 70) + "\n" for index in range(count))


def _focused_text() -> str:
    return "".join(
        f"# Section {index:02d}\nneedle evidence " + (f"detail-{index:02d} " * 8) + "\n\n"
        for index in range(60)
    )


def _sign(secret: bytes, nonce: str) -> str:
    """Mirror CursorSigner's MAC so a test can build a valid-looking token."""
    digest = hmac.new(secret, nonce.encode("ascii"), hashlib.sha256).digest()[:18]
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _first_page(backend: LocalBackend, source: str, **kwargs: object) -> dict:
    page = backend.open(source, **kwargs)  # type: ignore[arg-type]
    assert page is not None
    assert page["more_available"] is True
    assert isinstance(page["next_cursor"], str)
    return page


def _assert_open_rejected(
    backend: LocalBackend, code: str, source_id: str, cursor: object, **kwargs: object
) -> None:
    with pytest.raises(LocalBackendError) as caught:
        backend.open(source_id, cursor=cursor, **kwargs)  # type: ignore[arg-type]
    assert caught.value.code == code


# --- Positive controls ------------------------------------------------------


def test_untouched_exact_cursor_continues_at_the_next_line() -> None:
    source = "C:/approved/large.txt"
    backend, _reader = _backend(_Record(source, _rows()))
    first = _first_page(backend, source)

    second = backend.open(first["source_id"], cursor=first["next_cursor"])
    assert second is not None
    assert second["selected_lines"]["start"] == first["selected_lines"]["end"] + 1
    assert second["text"].startswith("row ")


def test_untouched_cursor_with_identical_range_and_budget_still_continues() -> None:
    source = "C:/approved/large.txt"
    backend, _reader = _backend(_Record(source, _rows()))
    first = _first_page(backend, source, start_line=1, end_line=120, result_token_budget=512)

    second = backend.open(
        first["source_id"],
        start_line=1,
        end_line=120,
        result_token_budget=512,
        cursor=first["next_cursor"],
    )
    assert second is not None
    assert second["selected_lines"]["start"] == first["selected_lines"]["end"] + 1


# --- 1. Signature and token shape ------------------------------------------


@pytest.mark.parametrize("position", ["first", "last"])
def test_tampered_signature_is_rejected(position: str) -> None:
    source = "C:/approved/large.txt"
    backend, _reader = _backend(_Record(source, _rows()))
    first = _first_page(backend, source)
    nonce, signature = first["next_cursor"].split(".")
    index = 0 if position == "first" else len(signature) - 1
    flipped = "A" if signature[index] != "A" else "B"
    tampered = f"{nonce}.{signature[:index]}{flipped}{signature[index + 1 :]}"
    assert tampered != first["next_cursor"]

    _assert_open_rejected(backend, "cursor_invalid", first["source_id"], tampered)


def test_signature_replaced_with_constant_bytes_is_rejected() -> None:
    source = "C:/approved/large.txt"
    backend, _reader = _backend(_Record(source, _rows()))
    first = _first_page(backend, source)
    nonce = first["next_cursor"].split(".")[0]

    _assert_open_rejected(backend, "cursor_invalid", first["source_id"], f"{nonce}.{'A' * 24}")


@pytest.mark.parametrize(
    "cursor",
    [
        pytest.param("nodothere", id="no-dot"),
        pytest.param("a.b.c", id="two-dots"),
        pytest.param("." + "A" * 24, id="empty-nonce"),
        pytest.param("n" * 65 + "." + "A" * 24, id="nonce-65-chars"),
        pytest.param("nonce." + "A" * 23, id="signature-23-chars"),
        pytest.param("nonce." + "A" * 25, id="signature-25-chars"),
        pytest.param("nonce." + "!" * 24, id="signature-non-base64"),
        pytest.param("a" * 129, id="over-128-chars"),
        pytest.param(123, id="non-str-int"),
        pytest.param(b"nonce.sig", id="non-str-bytes"),
        pytest.param(["nonce.sig"], id="non-str-list"),
    ],
)
def test_malformed_cursor_shapes_are_rejected(cursor: object) -> None:
    source = "C:/approved/large.txt"
    backend, _reader = _backend(_Record(source, _rows()))
    first = _first_page(backend, source)

    _assert_open_rejected(backend, "cursor_invalid", first["source_id"], cursor)


def test_well_formed_token_with_unknown_nonce_is_rejected() -> None:
    source = "C:/approved/large.txt"
    backend, _reader = _backend(_Record(source, _rows()))
    first = _first_page(backend, source)
    # Valid shape and a correct MAC under this backend's secret, but no state
    # was ever issued for the nonce.
    nonce = secrets.token_urlsafe(18)
    forged = f"{nonce}.{_sign(backend._cursor_signer._secret, nonce)}"

    _assert_open_rejected(backend, "cursor_invalid", first["source_id"], forged)


# --- 2. Different backend or signer ----------------------------------------


def test_cursor_issued_by_another_backend_secret_is_rejected() -> None:
    source = "C:/approved/large.txt"
    issuer, _ = _backend(_Record(source, _rows()))
    other, _ = _backend(_Record(source, _rows()))
    assert issuer._cursor_signer._secret != other._cursor_signer._secret
    first = _first_page(issuer, source)

    _assert_open_rejected(other, "cursor_invalid", first["source_id"], first["next_cursor"])


def test_same_secret_but_different_signer_state_is_rejected() -> None:
    source = "C:/approved/large.txt"
    backend, _reader = _backend(_Record(source, _rows()))
    first = _first_page(backend, source)
    snapshot = backend.store.get(first["source_id"])
    twin = CursorSigner(secret=backend._cursor_signer._secret)

    with pytest.raises(PolicyError) as caught:
        present_snapshot(
            snapshot,
            cursor=first["next_cursor"],
            result_token_budget=512,
            max_result_bytes=4096,
            estimator=backend.estimator,
            cursor_signer=twin,
        )
    assert caught.value.code == "cursor_invalid"


# --- 3. Mode changes --------------------------------------------------------


def test_exact_cursor_is_rejected_by_focused_mode() -> None:
    source = "C:/approved/large.txt"
    backend, _reader = _backend(_Record(source, _rows()))
    first = _first_page(backend, source)

    with pytest.raises(LocalBackendError) as caught:
        backend.open(first["source_id"], mode="focused", query="row", cursor=first["next_cursor"])
    assert caught.value.code == "cursor_mismatch"


def test_exact_cursor_is_rejected_by_overview_mode() -> None:
    source = "C:/approved/large.txt"
    backend, _reader = _backend(_Record(source, _rows()))
    first = _first_page(backend, source)

    _assert_open_rejected(
        backend, "cursor_mismatch", first["source_id"], first["next_cursor"], mode="overview"
    )


def test_focused_cursor_is_rejected_by_exact_mode() -> None:
    source = "C:/approved/focused.md"
    backend, _reader = _backend(_Record(source, _focused_text()))
    focused = _first_page(backend, source, mode="focused", query="needle evidence")

    _assert_open_rejected(backend, "cursor_mismatch", focused["source_id"], focused["next_cursor"])


# --- 4. Query changes -------------------------------------------------------


@pytest.mark.parametrize(
    "changed_query",
    ["needle", "needle evidence ", "NEEDLE EVIDENCE", "needle evidence other"],
)
def test_focused_cursor_is_rejected_when_query_changes(changed_query: str) -> None:
    source = "C:/approved/focused.md"
    backend, _reader = _backend(_Record(source, _focused_text()))
    focused = _first_page(backend, source, mode="focused", query="needle evidence")

    _assert_open_rejected(
        backend,
        "cursor_mismatch",
        focused["source_id"],
        focused["next_cursor"],
        mode="focused",
        query=changed_query,
    )


def test_exact_cursor_is_rejected_when_a_query_is_supplied() -> None:
    source = "C:/approved/large.txt"
    backend, _reader = _backend(_Record(source, _rows()))
    first = _first_page(backend, source)

    _assert_open_rejected(
        backend, "cursor_mismatch", first["source_id"], first["next_cursor"], query="row"
    )


# --- 5. Token budget changes ------------------------------------------------


@pytest.mark.parametrize("budget", [128, 256, 384])
def test_exact_cursor_is_rejected_when_token_budget_changes(budget: int) -> None:
    source = "C:/approved/large.txt"
    backend, _reader = _backend(_Record(source, _rows()))
    first = _first_page(backend, source)

    _assert_open_rejected(
        backend,
        "cursor_mismatch",
        first["source_id"],
        first["next_cursor"],
        result_token_budget=budget,
    )


def test_focused_cursor_is_rejected_when_token_budget_changes() -> None:
    source = "C:/approved/focused.md"
    backend, _reader = _backend(_Record(source, _focused_text()))
    focused = _first_page(backend, source, mode="focused", query="needle evidence")

    _assert_open_rejected(
        backend,
        "cursor_mismatch",
        focused["source_id"],
        focused["next_cursor"],
        mode="focused",
        query="needle evidence",
        result_token_budget=256,
    )


# --- 6. Explicit line range changes -----------------------------------------


@pytest.mark.parametrize(
    ("start_line", "end_line"),
    [(1, 99), (2, 120), (1, 60), (5, 5)],
)
def test_exact_cursor_is_rejected_when_explicit_range_differs(
    start_line: int, end_line: int
) -> None:
    source = "C:/approved/large.txt"
    backend, _reader = _backend(_Record(source, _rows()))
    first = _first_page(backend, source, start_line=1, end_line=120)

    _assert_open_rejected(
        backend,
        "cursor_mismatch",
        first["source_id"],
        first["next_cursor"],
        start_line=start_line,
        end_line=end_line,
    )


def test_whole_document_cursor_is_rejected_when_an_explicit_range_is_added() -> None:
    source = "C:/approved/large.txt"
    backend, _reader = _backend(_Record(source, _rows()))
    first = _first_page(backend, source)

    _assert_open_rejected(
        backend,
        "cursor_mismatch",
        first["source_id"],
        first["next_cursor"],
        start_line=1,
        end_line=120,
    )


# --- 7. Source and version changes -----------------------------------------


def test_cursor_is_rejected_against_a_different_source() -> None:
    first_source = "C:/approved/a.txt"
    second_source = "C:/approved/b.txt"
    backend, _reader = _backend(
        _Record(first_source, _rows()),
        _Record(second_source, _rows(), digest="digest-b"),
    )
    first = _first_page(backend, first_source)
    second = backend.open(second_source)
    assert second is not None

    _assert_open_rejected(backend, "cursor_mismatch", second["source_id"], first["next_cursor"])
    # Passing the second file's path with the first file's cursor is also refused.
    with pytest.raises(LocalBackendError) as caught:
        backend.open(second_source, cursor=first["next_cursor"])
    assert caught.value.code == "cursor_mismatch"


def test_cursor_is_rejected_after_the_same_path_changes_content() -> None:
    source = "C:/approved/a.txt"
    backend, reader = _backend(_Record(source, _rows()))
    first = _first_page(backend, source)

    reader.records[source] = _Record(source, _rows(count=119) + "changed\n", digest="digest-v2")
    refreshed = backend.open(source)
    assert refreshed is not None
    assert refreshed["version"] != first["version"]

    # The new read gets a new snapshot id and version; the old cursor must
    # not continue against it, nor against the same path.
    _assert_open_rejected(backend, "cursor_mismatch", refreshed["source_id"], first["next_cursor"])
    with pytest.raises(LocalBackendError) as caught:
        backend.open(source, cursor=first["next_cursor"])
    assert caught.value.code == "cursor_mismatch"


def test_cursor_is_rejected_when_the_snapshot_version_is_bumped_under_the_same_id() -> None:
    # The store never reuses a source_id for new content, so this exercises the
    # version binding directly: same id, new digest and version string.
    source = "C:/approved/a.txt"
    backend, _reader = _backend(_Record(source, _rows()))
    first = _first_page(backend, source)
    original = backend.store.get(first["source_id"])
    new_text = _rows(count=119) + "changed\n"
    bumped = dataclasses.replace(
        original,
        text=new_text,
        digest="digest-v2",
        version="sha256:digest-v2:text-v1",
        byte_count=len(new_text.encode("utf-8")),
    )

    with pytest.raises(PolicyError) as caught:
        present_snapshot(
            bumped,
            cursor=first["next_cursor"],
            result_token_budget=512,
            max_result_bytes=4096,
            estimator=backend.estimator,
            cursor_signer=backend._cursor_signer,
        )
    assert caught.value.code == "cursor_mismatch"

    # The untouched version still continues, which shows the rejection above
    # is about the version and not the cursor itself.
    result = present_snapshot(
        original,
        cursor=first["next_cursor"],
        result_token_budget=512,
        max_result_bytes=4096,
        estimator=backend.estimator,
        cursor_signer=backend._cursor_signer,
    )
    assert result["selected_lines"]["start"] == first["selected_lines"]["end"] + 1


# --- 8. retrieve() selector cursors ----------------------------------------


def test_retrieve_selector_cursor_continues_with_the_same_selector() -> None:
    source = "C:/approved/large.txt"
    backend, _reader = _backend(_Record(source, _rows()))
    opened = backend.open(source)
    assert opened is not None
    selector = {"type": "lines", "start": 1, "end": 120}
    first = backend.retrieve(opened["source_id"], selector)
    assert first["more_available"] is True

    second = backend.retrieve(opened["source_id"], selector, cursor=first["next_cursor"])
    assert second["selected_lines"]["start"] == first["selected_lines"]["end"] + 1


@pytest.mark.parametrize(
    "other_selector",
    [
        {"type": "lines", "start": 1, "end": 60},
        {"type": "lines", "start": 2, "end": 120},
        {"type": "lines", "start": 1, "end": 119},
    ],
)
def test_retrieve_cursor_is_rejected_with_a_different_selector(other_selector: dict) -> None:
    source = "C:/approved/large.txt"
    backend, _reader = _backend(_Record(source, _rows()))
    opened = backend.open(source)
    assert opened is not None
    first = backend.retrieve(opened["source_id"], {"type": "lines", "start": 1, "end": 120})
    assert first["more_available"] is True

    with pytest.raises(LocalBackendError) as caught:
        backend.retrieve(opened["source_id"], other_selector, cursor=first["next_cursor"])
    assert caught.value.code == "cursor_mismatch"


def test_retrieve_cursor_is_rejected_against_a_different_source() -> None:
    first_source = "C:/approved/a.txt"
    second_source = "C:/approved/b.txt"
    backend, _reader = _backend(
        _Record(first_source, _rows()),
        _Record(second_source, _rows(), digest="digest-b"),
    )
    first_open = backend.open(first_source)
    second_open = backend.open(second_source)
    assert first_open is not None and second_open is not None
    selector = {"type": "lines", "start": 1, "end": 120}
    first = backend.retrieve(first_open["source_id"], selector)

    with pytest.raises(LocalBackendError) as caught:
        backend.retrieve(second_open["source_id"], selector, cursor=first["next_cursor"])
    assert caught.value.code == "cursor_mismatch"


def test_retrieve_cursor_is_not_accepted_by_open() -> None:
    # Regression: open() once accepted a retrieve() cursor bound to lines
    # 50-120 and returned lines outside that selection.
    source = "C:/approved/large.txt"
    backend, _reader = _backend(_Record(source, _rows()))
    opened = backend.open(source)
    assert opened is not None
    first = backend.retrieve(opened["source_id"], {"type": "lines", "start": 50, "end": 120})
    assert first["more_available"] is True

    _assert_open_rejected(backend, "cursor_mismatch", opened["source_id"], first["next_cursor"])


def test_open_cursor_is_not_accepted_by_retrieve() -> None:
    # Regression: open() offsets are line numbers and retrieve() offsets are
    # unit indices, so a cross-tool cursor once silently skipped a line.
    source = "C:/approved/large.txt"
    backend, _reader = _backend(_Record(source, _rows()))
    first = _first_page(backend, source, start_line=1, end_line=120)

    with pytest.raises(LocalBackendError) as caught:
        backend.retrieve(
            first["source_id"],
            {"type": "lines", "start": 1, "end": 120},
            cursor=first["next_cursor"],
        )
    assert caught.value.code == "cursor_mismatch"


# --- 9. Policy version changes ---------------------------------------------


def test_cursor_is_rejected_after_policy_version_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    source = "C:/approved/large.txt"
    backend, _reader = _backend(_Record(source, _rows()))
    first = _first_page(backend, source)

    monkeypatch.setattr(policies, "POLICY_VERSION", "desktop-presentation-v2")

    _assert_open_rejected(backend, "cursor_mismatch", first["source_id"], first["next_cursor"])


def test_retrieve_cursor_is_rejected_after_policy_version_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "C:/approved/large.txt"
    backend, _reader = _backend(_Record(source, _rows()))
    opened = backend.open(source)
    assert opened is not None
    selector = {"type": "lines", "start": 1, "end": 120}
    first = backend.retrieve(opened["source_id"], selector)

    monkeypatch.setattr(policies, "POLICY_VERSION", "desktop-presentation-v2")

    with pytest.raises(LocalBackendError) as caught:
        backend.retrieve(opened["source_id"], selector, cursor=first["next_cursor"])
    assert caught.value.code == "cursor_mismatch"


def test_empty_cursor_is_rejected_instead_of_restarting_at_page_one() -> None:
    source = "C:/approved/large.txt"
    backend, _reader = _backend(_Record(source, _rows()))
    first = _first_page(backend, source)

    _assert_open_rejected(backend, "cursor_invalid", first["source_id"], "")
    with pytest.raises(LocalBackendError) as caught:
        backend.retrieve(first["source_id"], {"type": "lines", "start": 1, "end": 120}, cursor="")
    assert caught.value.code == "cursor_invalid"
