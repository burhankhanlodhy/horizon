"""End-to-end PDF and DOCX source tests through SourceReader and LocalBackend."""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pypdf
import pytest

from horizon.desktop_mcp.local_backend import LocalBackend, LocalBackendError
from horizon.desktop_mcp.source_access import SourceReader

# --- fixtures: minimal PDF and DOCX builders (copied from the extractor tests) ---


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_pdf(pages: list[list[str]]) -> bytes:
    """Emit a minimal valid PDF: one page per inner list of text lines."""
    count = len(pages)
    kids = " ".join(f"{5 + 2 * i} 0 R" for i in range(count))
    bodies: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        4: (
            b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
            b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Length 1 >>\n"
            b"stream\n\x00\nendstream"
        ),
    }
    for i, lines in enumerate(pages):
        page_id, content_id = 5 + 2 * i, 6 + 2 * i
        resources = "<< /Font << /F1 3 0 R >> >>"
        bodies[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources {resources} /Contents {content_id} 0 R >>"
        ).encode()
        content = "".join(
            f"BT /F1 12 Tf 72 {720 - 14 * k} Td ({_escape(text)}) Tj ET\n"
            for k, text in enumerate(lines)
        ).encode("latin-1")
        bodies[content_id] = (
            f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream"
        )

    size = max(bodies) + 1
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for oid in range(1, size):
        offsets[oid] = len(out)
        out += f"{oid} 0 obj\n".encode() + bodies[oid] + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {size}\n".encode()
    out += b"0000000000 65535 f \n"
    for oid in range(1, size):
        out += f"{offsets[oid]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)


def _encrypted_pdf() -> bytes:
    writer = pypdf.PdfWriter(clone_from=io.BytesIO(build_pdf([["Secret words"]])))
    writer.encrypt("user-secret", owner_password=None)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


_NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
)
_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
_CONTENT_TYPES = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    b'<Default Extension="xml" ContentType="application/xml"/>'
    b"</Types>"
)


def _document(body: str) -> bytes:
    return f"{_XML_DECL}<w:document {_NS}><w:body>{body}</w:body></w:document>".encode()


def _make(body: str, parts: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    entries = {"word/document.xml": _document(body)}
    entries.update(parts or {})
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _run(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def _p(text: str = "", style: str | None = None) -> str:
    props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{props}{_run(text) if text else ''}</w:p>"


def _cell(*paragraphs: str) -> str:
    return f"<w:tc>{''.join(paragraphs)}</w:tc>"


def _table(rows: list[list[str]]) -> str:
    columns = max(len(row) for row in rows)
    grid = "".join("<w:gridCol/>" for _ in range(columns))
    body = "".join(f"<w:tr>{''.join(row)}</w:tr>" for row in rows)
    return f"<w:tbl><w:tblGrid>{grid}</w:tblGrid>{body}</w:tbl>"


# --- helpers ---------------------------------------------------------------------


def _backend(root: Path) -> LocalBackend:
    return LocalBackend(SourceReader((root,)))


def _approved(tmp_path: Path) -> Path:
    root = tmp_path / "approved"
    root.mkdir()
    return root


def _pdf_fixture(root: Path, name: str = "report.pdf", pages=None) -> Path:
    path = root / name
    path.write_bytes(
        build_pdf(
            pages
            if pages is not None
            else [["Page one alpha"], ["Page two beta", "second line"], ["Page three gamma"]]
        )
    )
    return path


def _docx_fixture(root: Path, name: str = "memo.docx", body: str | None = None, parts=None):
    path = root / name
    path.write_bytes(_make(body if body is not None else _default_docx_body(), parts))
    return path


def _default_docx_body() -> str:
    return (
        _p("Doc title", "Title")
        + _p("Intro")
        + _table([[_cell(_p("A")), _cell(_p("B"))], [_cell(_p("1")), _cell(_p("2"))]])
        + _p("Section", "Heading1")
        + _p("After")
    )


def _open(backend: LocalBackend, path: Path, **kwargs) -> dict:
    payload = backend.open(str(path), **kwargs)
    assert payload is not None
    return payload


def _code_for_open(backend: LocalBackend, path: Path) -> str:
    with pytest.raises(LocalBackendError) as error:
        backend.open(str(path))
    return error.value.code


# --- search, exact open and retrieval -------------------------------------------


def test_pdf_and_docx_are_discovered_by_the_catalog(tmp_path: Path) -> None:
    root = _approved(tmp_path)
    _pdf_fixture(root)
    _docx_fixture(root)
    result = _backend(root).search("")
    kinds = {hit["kind"] for hit in result["hits"]}
    assert kinds == {"pdf", "docx"}


def test_pdf_open_exact_returns_extracted_text_with_extraction_metadata(tmp_path: Path) -> None:
    root = _approved(tmp_path)
    path = _pdf_fixture(root)
    payload = _open(_backend(root), path)

    assert payload["kind"] == "pdf"
    assert payload["text"] == "Page one alpha\nPage two beta\nsecond line\nPage three gamma\n"
    assert re.fullmatch(r"sha256:[0-9a-f]{64}:pdf-pypdf-[\w.]+-v1", payload["version"])
    assert payload["extractor_version"].startswith("pdf-pypdf-")
    assert payload["extraction"] == {
        "extractor_version": payload["extractor_version"],
        "warnings": [],
        "block_counts": {"page": 3},
    }
    assert payload["coverage"] == "exact selected extracted text at immutable snapshot version"
    assert payload["digest"] == re.search(r"sha256:([0-9a-f]{64})", payload["version"]).group(1)


def test_docx_open_exact_returns_extracted_text_with_extraction_metadata(tmp_path: Path) -> None:
    root = _approved(tmp_path)
    path = _docx_fixture(root)
    payload = _open(_backend(root), path)

    assert payload["kind"] == "docx"
    assert payload["text"].startswith("Doc title\nIntro\n")
    assert payload["version"].endswith(":docx-stdlib-v1")
    assert payload["extraction"]["extractor_version"] == "docx-stdlib-v1"
    assert payload["extraction"]["warnings"] == []
    assert payload["extraction"]["block_counts"] == {
        "heading": 2,
        "paragraph": 2,
        "table": 1,
    }


def test_retrieve_pdf_page_returns_exact_block_text(tmp_path: Path) -> None:
    root = _approved(tmp_path)
    path = _pdf_fixture(root)
    backend = _backend(root)
    source_id = _open(backend, path)["source_id"]

    result = backend.retrieve(source_id, {"type": "page", "start": 2, "end": 2})
    assert result["text"] == "Page two beta\nsecond line\n"
    assert result["selected_lines"] == {"start": 2, "end": 3}
    assert result["coverage"] == (
        "exact extracted PDF text for selected pages at immutable snapshot version"
    )
    assert result["extraction"]["block_counts"] == {"page": 3}


def test_retrieve_docx_paragraph_range_and_table_return_exact_text(tmp_path: Path) -> None:
    root = _approved(tmp_path)
    path = _docx_fixture(root)
    backend = _backend(root)
    source_id = _open(backend, path)["source_id"]

    paragraphs = backend.retrieve(source_id, {"type": "paragraph", "start": 2, "end": 3})
    assert paragraphs["text"] == "Intro\nSection\n"
    table = backend.retrieve(source_id, {"type": "table", "index": 1})
    assert table["text"] == "A\tB\n1\t2\n"


def test_retrieve_batch_fuses_pdf_page_and_docx_paragraph(tmp_path: Path) -> None:
    root = _approved(tmp_path)
    pdf = _pdf_fixture(root)
    docx = _docx_fixture(root)
    backend = _backend(root)
    pdf_id = _open(backend, pdf)["source_id"]
    docx_id = _open(backend, docx)["source_id"]

    payload = backend.retrieve_batch(
        [
            {"source_id": pdf_id, "selector": {"type": "page", "start": 2, "end": 2}},
            {"source_id": docx_id, "selector": {"type": "paragraph", "start": 1, "end": 1}},
        ]
    )
    assert payload["errors"] == []
    texts = {block["source_id"]: block["text"] for block in payload["blocks"]}
    assert texts[pdf_id] == "Page two beta\nsecond line\n"
    assert texts[docx_id] == "Doc title\n"


def test_selector_kind_mismatch_is_rejected_for_documents(tmp_path: Path) -> None:
    root = _approved(tmp_path)
    backend = _backend(root)
    pdf_id = _open(backend, _pdf_fixture(root))["source_id"]
    with pytest.raises(LocalBackendError) as error:
        backend.retrieve(pdf_id, {"type": "paragraph", "start": 1, "end": 1})
    assert error.value.code == "selector_format_unsupported"


def test_compact_on_document_falls_back_to_exact(tmp_path: Path) -> None:
    root = _approved(tmp_path)
    path = _pdf_fixture(root)
    payload = _open(_backend(root), path, mode="compact")
    assert payload["mode"] == "exact"
    assert payload["compression_applied"] is False
    assert payload["text"] == "Page one alpha\nPage two beta\nsecond line\nPage three gamma\n"


def test_document_stats_count_extracted_text_bytes(tmp_path: Path) -> None:
    root = _approved(tmp_path)
    path = _pdf_fixture(root)
    backend = _backend(root)
    payload = _open(backend, path)
    assert backend.stats()["snapshot_source_bytes"] == len(payload["text"].encode("utf-8"))


# --- overview ---------------------------------------------------------------------


def test_pdf_overview_lists_pages_with_line_spans_and_text_state(tmp_path: Path) -> None:
    root = _approved(tmp_path)
    path = _pdf_fixture(root, pages=[["Page one alpha"], [], ["Page three gamma"]])
    payload = _open(_backend(root), path, mode="overview")
    assert payload["headings"] == [
        {"page": 1, "line": 1, "has_text": True},
        {"page": 2, "line": None, "has_text": False},
        {"page": 3, "line": 2, "has_text": True},
    ]
    assert payload["coverage"] == (
        "partial (page inventory only; not a document summary); omitted: pages_without_text"
    )


def test_docx_overview_lists_headings_in_document_order(tmp_path: Path) -> None:
    root = _approved(tmp_path)
    path = _docx_fixture(root)
    payload = _open(_backend(root), path, mode="overview")
    assert payload["headings"] == [
        {"line": 1, "heading": "Doc title", "heading_level": 0},
        {"line": 5, "heading": "Section", "heading_level": 1},
    ]
    assert payload["coverage"] == "partial (heading inventory only; not a document summary)"


# --- warnings, coverage and error propagation -------------------------------------


def test_docx_comments_part_adds_warning_and_coverage_note(tmp_path: Path) -> None:
    root = _approved(tmp_path)
    path = _docx_fixture(
        root,
        body=_p("Body text"),
        parts={"word/comments.xml": b"<w:comments>hidden note</w:comments>"},
    )
    payload = _open(_backend(root), path)
    assert payload["extraction"]["warnings"] == ["comments_omitted"]
    assert payload["coverage"] == (
        "exact selected extracted text at immutable snapshot version; omitted: comments_omitted"
    )
    assert "hidden note" not in payload["text"]


def test_encrypted_pdf_propagates_pdf_encrypted(tmp_path: Path) -> None:
    root = _approved(tmp_path)
    path = root / "locked.pdf"
    path.write_bytes(_encrypted_pdf())
    assert _code_for_open(_backend(root), path) == "pdf_encrypted"


def test_garbage_pdf_propagates_pdf_invalid(tmp_path: Path) -> None:
    root = _approved(tmp_path)
    path = root / "broken.pdf"
    path.write_bytes(b"%PDF-1.4\nnot really a document\n")
    assert _code_for_open(_backend(root), path) == "pdf_invalid"


def test_tracked_changes_docx_propagates_docx_tracked_changes(tmp_path: Path) -> None:
    root = _approved(tmp_path)
    change = f'<w:ins w:id="1" w:author="a">{_run("x")}</w:ins>'
    path = _docx_fixture(root, body=f"<w:p>{change}</w:p>")
    assert _code_for_open(_backend(root), path) == "docx_tracked_changes"


def test_pdf_renamed_to_txt_still_fails_as_invalid_text(tmp_path: Path) -> None:
    root = _approved(tmp_path)
    path = root / "renamed.txt"
    path.write_bytes(build_pdf([["Looks like text"]]))
    assert _code_for_open(_backend(root), path) == "source_invalid_text"


@pytest.mark.parametrize("name", ["report.pdf", "memo.docx"])
def test_table_operations_are_unsupported_for_documents(tmp_path: Path, name: str) -> None:
    root = _approved(tmp_path)
    path = _pdf_fixture(root) if name.endswith(".pdf") else _docx_fixture(root)
    backend = _backend(root)
    source_id = _open(backend, path)["source_id"]
    with pytest.raises(LocalBackendError) as error:
        backend.analyze(source_id, {"type": "count"})
    assert error.value.code == "table_kind_unsupported"


def test_reopening_a_changed_document_yields_a_new_version(tmp_path: Path) -> None:
    root = _approved(tmp_path)
    path = _pdf_fixture(root, pages=[["First version"]])
    backend = _backend(root)
    first = _open(backend, path)

    path.write_bytes(build_pdf([["Second version with more text"], ["Another page"]]))
    second = _open(backend, path)

    assert second["version"] != first["version"]
    assert second["text"] == "Second version with more text\nAnother page\n"
    # The earlier handle still addresses the immutable first version.
    old = backend.retrieve(first["source_id"], {"type": "page", "start": 1, "end": 1})
    assert old["text"] == "First version\n"
