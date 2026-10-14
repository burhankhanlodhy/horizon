from __future__ import annotations

import io
import logging

import pypdf
import pytest

from horizon.desktop_mcp.extractors import ExtractionError
from horizon.desktop_mcp.extractors.pdf import extract_pdf
from horizon.desktop_mcp.text_lines import split_physical_lines


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_pdf(
    pages: list[list[str]],
    *,
    image_on: frozenset[int] = frozenset(),
    raw_resources: str | None = None,
) -> bytes:
    """Emit a minimal valid PDF: one page per inner list of text lines.

    Object layout: 1 catalog, 2 page tree, 3 Helvetica font, 4 1x1 image,
    then page ``i`` is object ``5 + 2i`` and its content stream ``6 + 2i``.
    """
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
        if raw_resources is not None:
            resources = raw_resources
        else:
            xobject = " /XObject << /Im1 4 0 R >>" if i in image_on else ""
            resources = f"<< /Font << /F1 3 0 R >>{xobject} >>"
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


def _encrypted(user_password: str, owner_password: str | None) -> bytes:
    writer = pypdf.PdfWriter(clone_from=io.BytesIO(build_pdf([["Secret fixture words"]])))
    writer.encrypt(user_password, owner_password=owner_password)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _assert_spans_match(doc, expected_pages: list[str]) -> None:
    lines = split_physical_lines(doc.text)
    assert len(doc.blocks) == len(expected_pages)
    for block, page_text in zip(doc.blocks, expected_pages, strict=True):
        assert block.kind == "page"
        block_lines = lines[block.line_start - 1 : block.line_end]
        assert "".join(block_lines) == page_text
        if page_text:
            assert block.line_end >= block.line_start
        else:
            assert block.line_start > block.line_end


def test_multi_page_text_has_page_blocks_and_matching_spans() -> None:
    data = build_pdf([["Alpha one", "Alpha two"], ["Beta"], ["Gamma", "Delta"]])
    doc = extract_pdf(data)
    assert doc.text == "Alpha one\nAlpha two\nBeta\nGamma\nDelta\n"
    assert [(b.index, b.line_start, b.line_end) for b in doc.blocks] == [
        (1, 1, 2),
        (2, 3, 3),
        (3, 4, 5),
    ]
    assert all(block.attributes == {} for block in doc.blocks)
    assert doc.warnings == ()
    _assert_spans_match(doc, ["Alpha one\nAlpha two\n", "Beta\n", "Gamma\nDelta\n"])


def test_empty_page_in_middle_warns_and_gets_empty_span() -> None:
    doc = extract_pdf(build_pdf([["One"], [], ["Three"]]))
    assert doc.text == "One\nThree\n"
    assert doc.warnings == ("pages_without_text",)
    assert doc.blocks[0].attributes == {"pages_without_text": [2]}
    assert doc.blocks[1].attributes == {}
    assert doc.blocks[1].line_start > doc.blocks[1].line_end
    assert (doc.blocks[2].line_start, doc.blocks[2].line_end) == (2, 2)
    _assert_spans_match(doc, ["One\n", "", "Three\n"])


def test_all_empty_pages_raise_pdf_no_text() -> None:
    with pytest.raises(ExtractionError) as info:
        extract_pdf(build_pdf([[], []]))
    assert info.value.code == "pdf_no_text"


def test_garbage_bytes_raise_pdf_invalid() -> None:
    with pytest.raises(ExtractionError) as info:
        extract_pdf(b"this is definitely not a pdf")
    assert info.value.code == "pdf_invalid"


def test_truncated_pdf_raises_pdf_invalid() -> None:
    data = build_pdf([["Truncated fixture words"]])
    with pytest.raises(ExtractionError) as info:
        extract_pdf(data[: len(data) // 4])
    assert info.value.code == "pdf_invalid"


def test_too_many_pages_raises() -> None:
    with pytest.raises(ExtractionError) as info:
        extract_pdf(build_pdf([["one"], ["two"]]), max_pages=1)
    assert info.value.code == "pdf_too_many_pages"


def test_text_over_max_chars_raises() -> None:
    with pytest.raises(ExtractionError) as info:
        extract_pdf(build_pdf([["Alpha one"]]), max_chars=3)
    assert info.value.code == "pdf_text_too_large"


def test_image_xobject_adds_images_present_warning() -> None:
    doc = extract_pdf(build_pdf([["With picture"], ["Plain"]], image_on=frozenset({0})))
    assert doc.warnings == ("images_present",)
    assert doc.text == "With picture\nPlain\n"


def test_malformed_resources_are_treated_as_no_image() -> None:
    data = build_pdf(
        [["Still readable"]],
        raw_resources="<< /Font << /F1 3 0 R >> /XObject 99 0 R >>",
    )
    doc = extract_pdf(data)
    assert doc.warnings == ()
    assert doc.text == "Still readable\n"


def test_user_password_encrypted_pdf_raises_pdf_encrypted() -> None:
    with pytest.raises(ExtractionError) as info:
        extract_pdf(_encrypted("userpw", "ownerpw"))
    assert info.value.code == "pdf_encrypted"


def test_owner_password_only_pdf_extracts() -> None:
    doc = extract_pdf(_encrypted("", "ownerpw"))
    assert doc.text == "Secret fixture words\n"
    assert doc.blocks[0].line_start == 1


def test_extractor_version_names_pypdf_release() -> None:
    doc = extract_pdf(build_pdf([["Version"]]))
    assert doc.extractor_version == f"pdf-pypdf-{pypdf.__version__}-v1"


def test_pypdf_logger_level_restored_on_success_and_failure() -> None:
    logger = logging.getLogger("pypdf")
    previous = logger.level
    try:
        logger.setLevel(logging.WARNING)
        extract_pdf(build_pdf([["Logged"]]))
        assert logger.level == logging.WARNING
        with pytest.raises(ExtractionError):
            extract_pdf(b"not a pdf at all")
        assert logger.level == logging.WARNING
    finally:
        logger.setLevel(previous)


def test_crlf_and_cr_are_normalized_to_newlines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pypdf.PageObject, "extract_text", lambda self, *a, **k: "a\r\nb\rc")
    doc = extract_pdf(build_pdf([["unused"]]))
    assert doc.text == "a\nb\nc\n"
    assert (doc.blocks[0].line_start, doc.blocks[0].line_end) == (1, 3)


def test_error_messages_never_contain_fixture_text() -> None:
    secret = "Truncated fixture words"
    data = build_pdf([[secret]])
    cases = [
        (lambda: extract_pdf(data[: len(data) // 4]), "pdf_invalid"),
        (lambda: extract_pdf(data, max_chars=3), "pdf_text_too_large"),
        (lambda: extract_pdf(build_pdf([[secret], [secret]]), max_pages=1), "pdf_too_many_pages"),
        (lambda: extract_pdf(_encrypted("userpw", None)), "pdf_encrypted"),
    ]
    for call, code in cases:
        with pytest.raises(ExtractionError) as info:
            call()
        assert info.value.code == code
        assert secret not in str(info.value)
        assert secret not in info.value.code
