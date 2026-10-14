"""Tests for the stdlib DOCX extractor (zip limits, XML safety, block anchors)."""

from __future__ import annotations

import io
import time
import zipfile
from xml.sax.saxutils import escape

import pytest

from horizon.desktop_mcp.extractors import ExtractedDocument, ExtractionError, extract_document
from horizon.desktop_mcp.extractors.docx import extract_docx

_NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"'
)
_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
_CONTENT_TYPES = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    b'<Default Extension="xml" ContentType="application/xml"/>'
    b"</Types>"
)


def _document(body: str, prolog: str = _XML_DECL) -> bytes:
    return f"{prolog}<w:document {_NS}><w:body>{body}</w:body></w:document>".encode()


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _make(body: str, parts: dict[str, bytes] | None = None) -> bytes:
    entries = {"word/document.xml": _document(body)}
    entries.update(parts or {})
    return _zip(entries)


def _run(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def _p(text: str = "", style: str | None = None) -> str:
    props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{props}{_run(text) if text else ''}</w:p>"


def _para(*inline: str, style: str | None = None) -> str:
    props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{props}{''.join(inline)}</w:p>"


def _cell(*paragraphs: str) -> str:
    return f"<w:tc>{''.join(paragraphs)}</w:tc>"


def _table(rows: list[list[str]]) -> str:
    columns = max(len(row) for row in rows)
    grid = "".join("<w:gridCol/>" for _ in range(columns))
    body = "".join(f"<w:tr>{''.join(row)}</w:tr>" for row in rows)
    return f"<w:tbl><w:tblGrid>{grid}</w:tblGrid>{body}</w:tbl>"


def _spans(doc: ExtractedDocument) -> list[tuple]:
    return [
        (block.kind, block.index, block.line_start, block.line_end, dict(block.attributes))
        for block in doc.blocks
    ]


def _code(data: bytes, **kwargs: int) -> str:
    with pytest.raises(ExtractionError) as excinfo:
        extract_docx(data, **kwargs)
    return excinfo.value.code


# --- paragraphs, headings and spans -----------------------------------------


def test_paragraphs_headings_and_title_numbering_and_spans() -> None:
    body = (
        _p("Doc title", "Title")
        + _p("Intro", "Heading1")
        + _p("Plain body")
        + _p("Sub", "Heading2")
        + _p("Deep", "heading 9")
    )
    doc = extract_docx(_make(body))
    assert doc.text == "Doc title\nIntro\nPlain body\nSub\nDeep\n"
    assert _spans(doc) == [
        ("heading", 1, 1, 1, {"heading_level": 0}),
        ("heading", 2, 2, 2, {"heading_level": 1}),
        ("paragraph", 3, 3, 3, {}),
        ("heading", 4, 4, 4, {"heading_level": 2}),
        ("heading", 5, 5, 5, {"heading_level": 9}),
    ]
    assert doc.extractor_version == "docx-stdlib-v1"
    assert doc.warnings == ()


@pytest.mark.parametrize("style", ["Heading3", "HEADING3", "heading3", "heading 3"])
def test_heading_style_ids_match_case_insensitively(style: str) -> None:
    doc = extract_docx(_make(_p("Section", style) + _p("text")))
    assert _spans(doc)[0] == ("heading", 1, 1, 1, {"heading_level": 3})
    assert _spans(doc)[1][0] == "paragraph"


def test_unknown_style_is_plain_paragraph() -> None:
    doc = extract_docx(_make(_p("Quote", "Quote") + _p("Heading10x", "Heading10")))
    assert [block.kind for block in doc.blocks] == ["paragraph", "paragraph"]


def test_empty_paragraphs_are_skipped_and_not_numbered() -> None:
    empty_heading = '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr></w:p>'
    body = "<w:p/>" + _p("") + _p("   ") + _p("first") + empty_heading + _p("second")
    doc = extract_docx(_make(body))
    assert doc.text == "first\nsecond\n"
    assert _spans(doc) == [
        ("paragraph", 1, 1, 1, {}),
        ("paragraph", 2, 2, 2, {}),
    ]


def test_tabs_and_line_breaks_stay_inside_one_block() -> None:
    inline = (
        _run("a")
        + "<w:r><w:tab/><w:t>b</w:t></w:r>"
        + "<w:r><w:br/><w:t>c</w:t></w:r>"
        + "<w:r><w:cr/></w:r>"
    )
    doc = extract_docx(_make(_p("x") + _para(inline)))
    assert doc.text == "x\na\tb\nc\n\n"
    assert _spans(doc) == [
        ("paragraph", 1, 1, 1, {}),
        ("paragraph", 2, 2, 4, {}),
    ]


def test_unicode_is_preserved() -> None:
    text = "Café — 日本語 ✓ 𝔘 naïve"
    doc = extract_docx(_make(_p(text)))
    assert doc.text == text + "\n"


def test_plain_document_without_parts_has_no_warnings() -> None:
    doc = extract_docx(_make(_p("hello")))
    assert doc.warnings == ()


def test_dispatch_through_extract_document() -> None:
    doc = extract_document(_make(_p("hello")), ".DOCX")
    assert doc.text == "hello\n"


# --- tables -------------------------------------------------------------------


def test_table_rendering_cells_spans_and_numbering() -> None:
    first = _table(
        [
            [_cell(_p("Name")), _cell(_p("Qty"))],
            [
                _cell(_p("Alpha"), _p("Beta")),
                _cell(_para(_run("line1"), "<w:r><w:br/></w:r>", _run("line2"))),
            ],
            [
                _cell(_para(_run("a"), "<w:r><w:tab/></w:r>", _run("b"))),
                _cell("<w:p/>"),
            ],
        ]
    )
    second = _table([[_cell(_p("only"))], [_cell(_p("row"))]])
    body = _p("Before") + first + _p("Between") + second + _p("After")
    doc = extract_docx(_make(body))
    assert doc.text == (
        "Before\nName\tQty\nAlpha Beta\tline1 line2\na b\t\nBetween\nonly\nrow\nAfter\n"
    )
    assert _spans(doc) == [
        ("paragraph", 1, 1, 1, {}),
        ("table", 1, 2, 4, {"rows": 3, "columns": 2}),
        ("paragraph", 2, 5, 5, {}),
        ("table", 2, 6, 7, {"rows": 2, "columns": 1}),
        ("paragraph", 3, 8, 8, {}),
    ]


def test_nested_table_is_flattened_with_warning() -> None:
    inner = _table([[_cell(_p("inner"))]])
    outer_cell = f"<w:tc>{_p('outer')}{inner}<w:p/></w:tc>"
    doc = extract_docx(_make(_table([[outer_cell, _cell(_p("right"))]])))
    assert doc.text == "outer inner\tright\n"
    assert doc.warnings == ("nested_tables_flattened",)
    assert _spans(doc) == [("table", 1, 1, 1, {"rows": 1, "columns": 2})]


def test_table_with_no_text_is_skipped() -> None:
    body = _table([[_cell("<w:p/>"), _cell("<w:p/>")]]) + _p("kept")
    doc = extract_docx(_make(body))
    assert doc.text == "kept\n"
    assert _spans(doc) == [("paragraph", 1, 1, 1, {})]


# --- inline structure --------------------------------------------------------


def test_field_code_is_excluded_and_result_kept() -> None:
    inline = (
        _run("see ")
        + '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        + '<w:r><w:instrText xml:space="preserve"> HYPERLINK "https://example.test/x" </w:instrText></w:r>'
        + '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        + _run("Click")
        + '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
    )
    doc = extract_docx(_make(_para(inline)))
    assert doc.text == "see Click\n"
    assert "HYPERLINK" not in doc.text


def test_simple_field_result_kept() -> None:
    inline = '<w:fldSimple w:instr=" PAGE "><w:r><w:t>7</w:t></w:r></w:fldSimple>'
    doc = extract_docx(_make(_para(_run("page "), inline)))
    assert doc.text == "page 7\n"


def test_hyperlink_text_is_kept() -> None:
    link = f'<w:hyperlink r:id="rId9">{_run("site")}</w:hyperlink>'
    doc = extract_docx(_make(_para(link, _run(" now"))))
    assert doc.text == "site now\n"


def test_sdt_block_and_inline_content_is_kept() -> None:
    block_sdt = f"<w:sdt><w:sdtPr/><w:sdtContent>{_p('In block')}</w:sdtContent></w:sdt>"
    inline_sdt = f"<w:sdt><w:sdtPr/><w:sdtContent>{_run('B')}</w:sdtContent></w:sdt>"
    doc = extract_docx(_make(block_sdt + _para(_run("A "), inline_sdt)))
    assert doc.text == "In block\nA B\n"
    assert _spans(doc) == [("paragraph", 1, 1, 1, {}), ("paragraph", 2, 2, 2, {})]


@pytest.mark.parametrize("tag", ["ins", "del", "moveFrom", "moveTo"])
def test_tracked_changes_are_rejected(tag: str) -> None:
    change = f'<w:{tag} w:id="1" w:author="a">{_run("x")}</w:{tag}>'
    assert _code(_make(_para(change))) == "docx_tracked_changes"


def test_tracked_change_inside_table_is_rejected() -> None:
    change = f'<w:ins w:id="2" w:author="a">{_run("x")}</w:ins>'
    assert _code(_make(_table([[_cell(_para(change))]]))) == "docx_tracked_changes"


def test_textbox_text_is_omitted_with_warning() -> None:
    textbox = (
        "<w:r><w:drawing><wp:inline><a:graphic><a:graphicData>"
        "<wps:wsp><wps:txbx><w:txbxContent>"
        f"{_p('SECRETBOX')}"
        "</w:txbxContent></wps:txbx></wps:wsp>"
        "</a:graphicData></a:graphic></wp:inline></w:drawing></w:r>"
    )
    doc = extract_docx(_make(_para(textbox) + _p("Visible")))
    assert doc.text == "Visible\n"
    assert "SECRETBOX" not in doc.text
    assert "textbox_content_omitted" in doc.warnings
    assert "images_present" in doc.warnings


def test_drawing_adds_images_present_warning() -> None:
    drawing = (
        "<w:r><w:drawing><wp:inline><a:graphic><a:graphicData/>"
        "</a:graphic></wp:inline></w:drawing></w:r>"
    )
    doc = extract_docx(_make(_p("Caption") + _para(drawing)))
    assert doc.text == "Caption\n"
    assert doc.warnings == ("images_present",)


def test_comment_footnote_endnote_and_header_parts_add_warnings() -> None:
    parts = {
        "word/comments.xml": b"<w:comments>SECRETCOMMENT</w:comments>",
        "word/footnotes.xml": b"<w:footnotes>SECRETFOOT</w:footnotes>",
        "word/endnotes.xml": b"<w:endnotes>SECRETEND</w:endnotes>",
        "word/header1.xml": b"<w:hdr>SECRETHEAD</w:hdr>",
        "word/footer1.xml": b"<w:ftr>SECRETFOOTER</w:ftr>",
    }
    doc = extract_docx(_make(_p("body"), parts))
    assert doc.text == "body\n"
    assert set(doc.warnings) == {
        "comments_omitted",
        "footnotes_omitted",
        "endnotes_omitted",
        "headers_footers_omitted",
    }
    assert "SECRET" not in doc.text


# --- rejected or unsafe packages --------------------------------------------


def test_doctype_with_entity_expansion_is_rejected_without_expansion() -> None:
    # Each level expands the previous one ten times: a small "billion laughs" shape.
    declarations = ['<!ENTITY e0 "xxxxxxxxxx">']
    for level in range(1, 5):
        references = "".join(f"&e{level - 1};" for _ in range(10))
        declarations.append(f'<!ENTITY e{level} "{references}">')
    prolog = f"{_XML_DECL}<!DOCTYPE w:document [{''.join(declarations)}]>"
    data = _zip({"word/document.xml": _document("&e4;", prolog=prolog)})
    assert _code(data) == "docx_invalid"


def test_entity_declaration_in_utf16_is_rejected() -> None:
    xml = f'<!DOCTYPE w:document [<!ENTITY a "b">]><w:document {_NS}><w:body/></w:document>'
    data = _zip({"word/document.xml": xml.encode("utf-16")})
    assert _code(data) == "docx_invalid"


def test_non_zip_bytes_are_invalid() -> None:
    assert _code(b"this is not a zip file") == "docx_invalid"


def test_zip_without_document_xml_is_invalid() -> None:
    assert _code(_zip({"word/other.xml": b"<x/>"})) == "docx_invalid"


def test_malformed_xml_is_invalid_without_message() -> None:
    assert _code(_zip({"word/document.xml": b"<w:document"})) == "docx_invalid"


def test_wrong_root_element_is_invalid() -> None:
    assert _code(_zip({"word/document.xml": b"<root/>"})) == "docx_invalid"


def test_too_many_entries_fail_fast() -> None:
    entries = {f"word/part{i}.txt": b"x" for i in range(2001)}
    entries["word/document.xml"] = _document(_p("hi"))
    assert _code(_zip(entries)) == "docx_too_large"


def test_high_ratio_zip_bomb_fails_fast() -> None:
    started = time.monotonic()
    bomb = _zip({"word/document.xml": b"\0" * (50 * 1024 * 1024)})
    assert _code(bomb) == "docx_too_large"
    assert time.monotonic() - started < 10


def test_high_ratio_non_document_entry_over_one_mib_is_rejected() -> None:
    data = _zip(
        {
            "word/document.xml": _document(_p("hi")),
            "word/media/big.bin": b"\0" * (2 * 1024 * 1024),
        }
    )
    assert _code(data) == "docx_too_large"


def test_small_high_ratio_entry_under_one_mib_is_allowed() -> None:
    data = _zip(
        {
            "word/document.xml": _document(_p("hi")),
            "word/media/small.bin": b"\0" * (512 * 1024),
        }
    )
    assert extract_docx(data).text == "hi\n"


# --- output limits ----------------------------------------------------------


def test_document_with_no_text_is_rejected() -> None:
    body = "<w:p/>" + _table([[_cell("<w:p/>")]])
    assert _code(_make(body)) == "docx_no_text"


def test_text_over_max_chars_is_rejected() -> None:
    data = _make(_p("hello world"))
    assert _code(data, max_chars=5) == "docx_text_too_large"
    assert extract_docx(data, max_chars=len("hello world\n")).text == "hello world\n"
