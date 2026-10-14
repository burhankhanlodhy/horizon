"""Bounded PDF text extraction with pypdf, one ``page`` block per page.

The contract is documented in ``horizon.desktop_mcp.extractors``. pypdf's
exceptions and messages never leave this module: every failure is reported as
a sanitized ``ExtractionError`` code.
"""

from __future__ import annotations

import io
import logging
from typing import Any

import pypdf

from ..text_lines import split_physical_lines
from . import (
    MAX_EXTRACTED_CHARS,
    MAX_PDF_PAGES,
    DocumentBlock,
    ExtractedDocument,
    ExtractionError,
)

_PYPDF_LOGGER = "pypdf"


def extract_pdf(
    data: bytes,
    *,
    max_pages: int = MAX_PDF_PAGES,
    max_chars: int = MAX_EXTRACTED_CHARS,
) -> ExtractedDocument:
    """Extract per-page text from PDF ``data`` without touching the filesystem."""
    logger = logging.getLogger(_PYPDF_LOGGER)
    previous_level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        return _extract(data, max_pages=max_pages, max_chars=max_chars)
    except ExtractionError:
        raise
    except Exception:
        # pypdf messages may quote document content; drop them and the chain.
        raise ExtractionError("pdf_invalid") from None
    finally:
        logger.setLevel(previous_level)


def _extract(data: bytes, *, max_pages: int, max_chars: int) -> ExtractedDocument:
    reader = pypdf.PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            status = reader.decrypt("")
        except Exception:
            raise ExtractionError("pdf_encrypted") from None
        if not status:
            raise ExtractionError("pdf_encrypted")

    page_count = len(reader.pages)
    if page_count > max_pages:
        raise ExtractionError("pdf_too_many_pages")

    chunks: list[str] = []
    blocks: list[tuple[int, int, int]] = []
    pages_without_text: list[int] = []
    images_present = False
    total_chars = 0
    lines_so_far = 0

    for number, page in enumerate(reader.pages, start=1):
        text = _normalize(page.extract_text() or "")
        if _has_images(page):
            images_present = True
        if text.strip():
            if not text.endswith("\n"):
                text += "\n"
        else:
            pages_without_text.append(number)
            text = ""

        total_chars += len(text)
        if total_chars > max_chars:
            raise ExtractionError("pdf_text_too_large")

        line_count = len(split_physical_lines(text))
        blocks.append((number, lines_so_far + 1, lines_so_far + line_count))
        lines_so_far += line_count
        chunks.append(text)

    if len(pages_without_text) == page_count:
        raise ExtractionError("pdf_no_text")

    document_blocks: list[DocumentBlock] = []
    for position, (number, line_start, line_end) in enumerate(blocks):
        attributes: dict[str, Any] = {}
        if position == 0 and pages_without_text:
            attributes["pages_without_text"] = list(pages_without_text)
        document_blocks.append(
            DocumentBlock(
                kind="page",
                index=number,
                line_start=line_start,
                line_end=line_end,
                attributes=attributes,
            )
        )

    warnings: list[str] = []
    if pages_without_text:
        warnings.append("pages_without_text")
    if images_present:
        warnings.append("images_present")

    return ExtractedDocument(
        text="".join(chunks),
        blocks=tuple(document_blocks),
        extractor_version=f"pdf-pypdf-{pypdf.__version__}-v1",
        warnings=tuple(warnings),
    )


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _resolve(obj: Any) -> Any:
    get_object = getattr(obj, "get_object", None)
    return get_object() if callable(get_object) else obj


def _has_images(page: Any) -> bool:
    """True when the page's own /Resources /XObject has an /Image entry.

    Malformed or unreadable entries are treated as "no image".
    """
    try:
        resources = _resolve(page.get("/Resources"))
        xobjects = _resolve(resources.get("/XObject"))
        entries = list(xobjects.values())
    except Exception:
        return False
    for entry in entries:
        try:
            if _resolve(entry).get("/Subtype") == "/Image":
                return True
        except Exception:
            continue
    return False


__all__ = ["extract_pdf"]
