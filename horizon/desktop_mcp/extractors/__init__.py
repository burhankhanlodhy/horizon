"""Bounded text extraction for approved PDF and DOCX sources.

"Exact" for these formats means exact text in the extracted representation,
with a block reference back to the page, paragraph or table it came from. It
is not rendered layout, OCR, or a promise that every image, chart or text box
was captured; anything known to be omitted is listed in ``warnings``.
Extractors never execute macros, resolve external links, or open embedded
objects, and their errors never contain document text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAX_EXTRACTED_CHARS = 16 * 1024 * 1024
MAX_PDF_PAGES = 2000


class ExtractionError(ValueError):
    """Sanitized extraction failure with a stable code and no document text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DocumentBlock:
    # "page" (PDF), or "paragraph", "heading" and "table" (DOCX).
    kind: str
    # 1-based. PDF pages use the page number. DOCX paragraphs and headings
    # share one document-order numbering; tables are numbered separately.
    index: int
    # 1-based inclusive physical line span in ExtractedDocument.text. A block
    # with no extracted text (an empty PDF page) has line_start > line_end.
    line_start: int
    line_end: int
    # Small, JSON-safe facts such as {"heading_level": 2} or {"rows": 3,
    # "columns": 4}. Never document text.
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractedDocument:
    # Extracted text; every non-empty block's text ends with "\n".
    text: str
    blocks: tuple[DocumentBlock, ...]
    # e.g. "pdf-pypdf-6.20.1-v1" or "docx-stdlib-v1"; part of the snapshot version.
    extractor_version: str
    # Stable codes for content known to be omitted or unreliable, e.g.
    # "pages_without_text", "images_present", "comments_omitted".
    warnings: tuple[str, ...] = ()


DOCUMENT_KINDS = frozenset({"pdf", "docx"})


def extract_document(data: bytes, kind: str) -> ExtractedDocument:
    """Dispatch to the extractor for ``kind`` ("pdf" or "docx")."""
    normalized = kind.lower().lstrip(".")
    if normalized == "pdf":
        from .pdf import extract_pdf

        return extract_pdf(data)
    if normalized == "docx":
        from .docx import extract_docx

        return extract_docx(data)
    raise ExtractionError("document_kind_unsupported")


__all__ = [
    "DOCUMENT_KINDS",
    "MAX_EXTRACTED_CHARS",
    "MAX_PDF_PAGES",
    "DocumentBlock",
    "ExtractedDocument",
    "ExtractionError",
    "extract_document",
]
