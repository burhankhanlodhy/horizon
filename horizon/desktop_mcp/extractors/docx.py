"""Bounded DOCX extraction using only the standard library.

``word/document.xml`` is read by exact name and its body is walked in document
order. Paragraphs and headings share one numbering, tables another. Zip limits,
rejected XML declarations and the omissions reported as warnings follow the
DOCX section of the Phase F plan. Errors carry stable codes and no document
text.
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator
from typing import Any

from ..text_lines import split_physical_lines
from . import MAX_EXTRACTED_CHARS, DocumentBlock, ExtractedDocument, ExtractionError

EXTRACTOR_VERSION = "docx-stdlib-v1"
MAX_ZIP_ENTRIES = 2000
MAX_TOTAL_DECLARED_BYTES = 64 * 1024 * 1024
MAX_DOCUMENT_XML_BYTES = 32 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
RATIO_CHECK_MIN_BYTES = 1024 * 1024

_DOCUMENT_PART = "word/document.xml"
_COMMENTS_PART = "word/comments.xml"
_FOOTNOTES_PART = "word/footnotes.xml"
_ENDNOTES_PART = "word/endnotes.xml"
_HEADER_FOOTER_PART = re.compile(r"word/(?:header|footer)[^/]*\.xml")

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _w(name: str) -> str:
    return f"{{{_W_NS}}}{name}"


_DOCUMENT = _w("document")
_BODY = _w("body")
_P = _w("p")
_R = _w("r")
_T = _w("t")
_TAB = _w("tab")
_BR = _w("br")
_CR = _w("cr")
_PPR = _w("pPr")
_PSTYLE = _w("pStyle")
_VAL = _w("val")
_TBL = _w("tbl")
_TBLGRID = _w("tblGrid")
_GRIDCOL = _w("gridCol")
_TR = _w("tr")
_TC = _w("tc")
_SDT = _w("sdt")
_SDT_CONTENT = _w("sdtContent")
_CUSTOM_XML = _w("customXml")
_HYPERLINK = _w("hyperlink")
_SMART_TAG = _w("smartTag")
_FLD_SIMPLE = _w("fldSimple")
_DRAWING = _w("drawing")
_PICT = _w("pict")
_TXBX_CONTENT = _w("txbxContent")

_TRACKED_TAGS = frozenset(_w(name) for name in ("ins", "del", "moveFrom", "moveTo"))
_BLOCK_TAGS = frozenset({_P, _TBL})
_ROW_TAGS = frozenset({_TR})
_CELL_TAGS = frozenset({_TC})
_WRAPPER_TAGS = frozenset({_SDT, _CUSTOM_XML})
_INLINE_WRAPPER_TAGS = frozenset({_HYPERLINK, _SMART_TAG, _FLD_SIMPLE, _CUSTOM_XML})

_CELL_WHITESPACE = re.compile(r"[\t\n\r\x0b\x0c]")
_HEADING_STYLE = re.compile(r"heading([1-9])")
# Checked on the raw bytes before parsing, for UTF-8 and both UTF-16 byte orders.
_FORBIDDEN_DECLARATIONS = tuple(
    re.compile(re.escape(f"<!{keyword}".encode(encoding)), re.IGNORECASE)
    for keyword in ("DOCTYPE", "ENTITY")
    for encoding in ("utf-8", "utf-16-le", "utf-16-be")
)


def _check_zip_limits(infos: list[zipfile.ZipInfo]) -> None:
    if len(infos) > MAX_ZIP_ENTRIES:
        raise ExtractionError("docx_too_large")
    declared = 0
    for info in infos:
        declared += info.file_size
        if declared > MAX_TOTAL_DECLARED_BYTES:
            raise ExtractionError("docx_too_large")
        if info.file_size >= RATIO_CHECK_MIN_BYTES and info.file_size > MAX_COMPRESSION_RATIO * max(
            info.compress_size, 1
        ):
            raise ExtractionError("docx_too_large")


def _flatten(container: ET.Element, tags: frozenset[str]) -> Iterator[ET.Element]:
    """Yield children with a tag in ``tags``, descending through sdt and customXml."""
    for child in container:
        if child.tag in tags:
            yield child
        elif child.tag in _WRAPPER_TAGS:
            inner = child.find(_SDT_CONTENT) if child.tag == _SDT else child
            if inner is not None:
                yield from _flatten(inner, tags)


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _run_text(run: ET.Element, out: list[str]) -> None:
    # Only these children carry text. Drawings, text boxes, field
    # instructions and tracked-change deletions are never reached.
    for child in run:
        if child.tag == _T:
            out.append(child.text or "")
        elif child.tag == _TAB:
            out.append("\t")
        elif child.tag in (_BR, _CR):
            out.append("\n")


def _inline_text(parent: ET.Element, out: list[str]) -> None:
    for child in parent:
        if child.tag == _R:
            _run_text(child, out)
        elif child.tag in _INLINE_WRAPPER_TAGS:
            _inline_text(child, out)
        elif child.tag == _SDT:
            content = child.find(_SDT_CONTENT)
            if content is not None:
                _inline_text(content, out)


def _paragraph_text(paragraph: ET.Element) -> str:
    parts: list[str] = []
    _inline_text(paragraph, parts)
    return _normalize_newlines("".join(parts))


def _heading_level(paragraph: ET.Element) -> int | None:
    props = paragraph.find(_PPR)
    if props is None:
        return None
    style = props.find(_PSTYLE)
    if style is None:
        return None
    key = "".join((style.get(_VAL) or "").split()).lower()
    if key == "title":
        return 0
    match = _HEADING_STYLE.fullmatch(key)
    return int(match.group(1)) if match else None


class _BodyWalker:
    def __init__(self) -> None:
        self.blocks: list[tuple[str, int, str, dict[str, Any]]] = []
        self.warnings: list[str] = []
        self._paragraphs = 0
        self._tables = 0

    def warn(self, code: str) -> None:
        if code not in self.warnings:
            self.warnings.append(code)

    def walk(self, body: ET.Element) -> None:
        for item in _flatten(body, _BLOCK_TAGS):
            if item.tag == _P:
                self._paragraph(item)
            else:
                self._table(item)

    def _paragraph(self, paragraph: ET.Element) -> None:
        text = _paragraph_text(paragraph)
        if not text.strip():
            return
        self._paragraphs += 1
        level = _heading_level(paragraph)
        if level is None:
            self.blocks.append(("paragraph", self._paragraphs, text + "\n", {}))
        else:
            self.blocks.append(("heading", self._paragraphs, text + "\n", {"heading_level": level}))

    def _table(self, table: ET.Element) -> None:
        rows: list[str] = []
        grid = table.find(_TBLGRID)
        columns = len(grid.findall(_GRIDCOL)) if grid is not None else 0
        has_text = False
        for row in _flatten(table, _ROW_TAGS):
            cells: list[str] = []
            for cell in _flatten(row, _CELL_TAGS):
                units: list[str] = []
                self._cell(cell, units)
                cell_text = _CELL_WHITESPACE.sub(" ", " ".join(units))
                has_text = has_text or bool(cell_text.strip())
                cells.append(cell_text)
            rows.append("\t".join(cells))
            columns = max(columns, len(cells))
        if not has_text:
            return
        self._tables += 1
        text = "".join(f"{row}\n" for row in rows)
        self.blocks.append(("table", self._tables, text, {"rows": len(rows), "columns": columns}))

    def _cell(self, cell: ET.Element, units: list[str]) -> None:
        for item in _flatten(cell, _BLOCK_TAGS):
            if item.tag == _P:
                text = _paragraph_text(item)
                if text.strip():
                    units.append(text)
            else:
                self.warn("nested_tables_flattened")
                for row in _flatten(item, _ROW_TAGS):
                    for inner in _flatten(row, _CELL_TAGS):
                        self._cell(inner, units)


def _extract(data: bytes, max_chars: int) -> ExtractedDocument:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        _check_zip_limits(infos)
        names = {info.filename for info in infos}
        if _DOCUMENT_PART not in names:
            raise ExtractionError("docx_invalid")
        if archive.getinfo(_DOCUMENT_PART).file_size > MAX_DOCUMENT_XML_BYTES:
            raise ExtractionError("docx_too_large")
        with archive.open(_DOCUMENT_PART) as handle:
            raw = handle.read(MAX_DOCUMENT_XML_BYTES + 1)
    if len(raw) > MAX_DOCUMENT_XML_BYTES:
        raise ExtractionError("docx_too_large")
    if any(pattern.search(raw) for pattern in _FORBIDDEN_DECLARATIONS):
        raise ExtractionError("docx_invalid")

    root = ET.fromstring(raw)
    if root.tag != _DOCUMENT:
        raise ExtractionError("docx_invalid")
    body = root.find(_BODY)
    if body is None:
        raise ExtractionError("docx_invalid")

    textbox = False
    images = False
    for element in root.iter():
        if element.tag in _TRACKED_TAGS:
            raise ExtractionError("docx_tracked_changes")
        if element.tag in (_DRAWING, _PICT):
            images = True
        elif element.tag == _TXBX_CONTENT:
            textbox = True

    walker = _BodyWalker()
    walker.walk(body)
    if textbox:
        walker.warn("textbox_content_omitted")
    if images:
        walker.warn("images_present")
    if _COMMENTS_PART in names:
        walker.warn("comments_omitted")
    if _FOOTNOTES_PART in names:
        walker.warn("footnotes_omitted")
    if _ENDNOTES_PART in names:
        walker.warn("endnotes_omitted")
    if any(_HEADER_FOOTER_PART.fullmatch(name) for name in names):
        walker.warn("headers_footers_omitted")

    if not walker.blocks:
        raise ExtractionError("docx_no_text")

    pieces: list[str] = []
    blocks: list[DocumentBlock] = []
    line = 0
    total = 0
    for kind, index, text, attributes in walker.blocks:
        count = len(split_physical_lines(text))
        blocks.append(
            DocumentBlock(
                kind=kind,
                index=index,
                line_start=line + 1,
                line_end=line + count,
                attributes=attributes,
            )
        )
        line += count
        total += len(text)
        if total > max_chars:
            raise ExtractionError("docx_text_too_large")
        pieces.append(text)

    return ExtractedDocument(
        text="".join(pieces),
        blocks=tuple(blocks),
        extractor_version=EXTRACTOR_VERSION,
        warnings=tuple(walker.warnings),
    )


def extract_docx(data: bytes, *, max_chars: int = MAX_EXTRACTED_CHARS) -> ExtractedDocument:
    """Extract exact text and block anchors from a DOCX file.

    Every failure is an ``ExtractionError`` with a stable code. Source
    exceptions (bad zip, XML, decoding) are mapped to ``docx_invalid`` without
    their messages.
    """
    try:
        return _extract(data, max_chars)
    except ExtractionError:
        raise
    except Exception:
        raise ExtractionError("docx_invalid") from None


__all__ = ["EXTRACTOR_VERSION", "extract_docx"]
