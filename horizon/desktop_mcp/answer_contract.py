"""Versioned answer-style instruction packs authored by ContextShrink."""

from __future__ import annotations

import hashlib

_V1_TEXT = (
    "ContextShrink answer rules (v1). Lead with the direct answer, then the supporting detail. "
    "Keep every requested fact, figure, deliverable and required section complete. "
    "Give citations for sources by their reference (source id, line range or selector) "
    "instead of quoting the source again. "
    "Expand whenever correctness, safety or the user's request needs more detail. "
    "A full document the user asks for is delivered in full, never abbreviated. "
    "If coverage is partial, say so plainly and name what is missing. "
    "Do not invent citations or facts."
)

_PACKS: dict[str, str] = {"v1": _V1_TEXT}

INSTRUCTION_PACK_VERSIONS: tuple[str, ...] = tuple(sorted(_PACKS))


def instruction_pack(version: str = "v1") -> dict[str, object]:
    """Return the named instruction pack with its text, digest and length."""
    text = _PACKS.get(version) if isinstance(version, str) else None
    if text is None:
        raise ValueError("instruction_pack_unknown")
    return {
        "version": version,
        "text": text,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "characters": len(text),
    }


__all__ = ["INSTRUCTION_PACK_VERSIONS", "instruction_pack"]
