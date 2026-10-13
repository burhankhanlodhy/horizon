"""Query-aware pruning of a pasted document (whole sentences, never words).

Kompress drops words evenly and cannot see the question; the result reads as
damaged and saves few tokens. This instead scores each sentence or list item
against the caller's question and the document's own centre of gravity, then
omits the lowest-scoring ones until a word budget is met. What stays is
verbatim, so the document still reads as written.

Headings, short lines, fenced code and tables are never removed.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

# A sentence ends at . ! or ? followed by space and an uppercase letter, digit,
# backtick, bracket or quote. Abbreviations like "e.g. the" do not split.
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])[ \t]+(?=[A-Z0-9`(\[\"'*])")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6}\s|[=-]{3,}\s*$)")
_BULLET_RE = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)")
_SIGNAL_RE = re.compile(r"\d|`|\b[A-Z]{2,}\b")
MIN_UNIT_WORDS = 4

Embed = Callable[[list[str]], np.ndarray]  # rows L2-normalised


@dataclass
class _Unit:
    line: int
    text: str
    words: int
    lead: bool  # first sentence after a heading or paragraph start


def _split_line(line: str) -> tuple[str, list[str]]:
    """Return (bullet prefix, sentences) for one line of prose."""
    m = _BULLET_RE.match(line)
    prefix = m.group(1) if m else line[: len(line) - len(line.lstrip())]
    body = line[len(prefix) :]
    return prefix, [s for s in _SENTENCE_END_RE.split(body) if s.strip()]


def prune_pieces(
    pieces: Sequence[tuple[str, bool]],
    question: str,
    remove_fraction: float,
    embed: Embed,
    *,
    relevance_weight: float = 0.6,
    density: Callable[[list[str]], np.ndarray] | None = None,
    density_weight: float = 0.0,
) -> tuple[list[str], int, int]:
    """Prune compressible pieces; returns (pieces out, words before, words after).

    ``pieces`` are ``(text, compressible)`` as from ``_prompt_document_pieces``.
    Non-compressible pieces are returned unchanged.
    """
    units: list[_Unit] = []
    layout: list[list[tuple[int, str, list[int]] | str]] = []  # per piece: lines
    after_heading = True
    for text, compressible in pieces:
        lines_out: list[tuple[int, str, list[int]] | str] = []
        if not compressible:
            layout.append([text])
            after_heading = False
            continue
        for line in text.split("\n"):
            words = len(line.split())
            if not line.strip():
                lines_out.append(line)
                after_heading = True
                continue
            if _HEADING_RE.match(line) or words < MIN_UNIT_WORDS:
                lines_out.append(line)
                after_heading = bool(_HEADING_RE.match(line)) or after_heading
                continue
            prefix, sentences = _split_line(line)
            idxs = []
            for j, s in enumerate(sentences):
                idxs.append(len(units))
                units.append(_Unit(len(layout), s, len(s.split()), after_heading and j == 0))
            lines_out.append((len(layout), prefix, idxs))
            after_heading = False
        layout.append(lines_out)

    total = sum(u.words for u in units)
    keep = [True] * len(units)
    if units and remove_fraction > 0 and question.strip():
        vecs = embed([u.text for u in units])
        q = embed([question])[0]
        centre = vecs.mean(axis=0)
        centre = centre / (np.linalg.norm(centre) or 1.0)
        rel, cen = vecs @ q, vecs @ centre

        def z(a: np.ndarray) -> np.ndarray:
            return (a - a.mean()) / (a.std() or 1.0)

        score = (1 - density_weight) * (
            relevance_weight * z(rel) + (1 - relevance_weight) * z(cen)
        )
        if density is not None and density_weight > 0:
            score += density_weight * z(np.asarray(density([u.text for u in units]), dtype=float))
        score += np.array([0.5 if u.lead else 0.0 for u in units])
        score += np.array([0.25 if _SIGNAL_RE.search(u.text) else 0.0 for u in units])
        budget = total * (1 - remove_fraction)
        keep = [False] * len(units)
        kept = 0
        for i in np.argsort(-score):
            if kept >= budget:
                break
            keep[i] = True
            kept += units[i].words

    out: list[str] = []
    for lines_out in layout:
        rendered: list[str] = []
        for item in lines_out:
            if isinstance(item, str):
                rendered.append(item)
                continue
            _, prefix, idxs = item
            kept_text = " ".join(units[i].text.strip() for i in idxs if keep[i])
            if kept_text:
                rendered.append(prefix + kept_text)
        piece = "\n".join(rendered)
        out.append(re.sub(r"\n{3,}", "\n\n", piece))
    after = sum(u.words for u, k in zip(units, keep) if k)
    return out, total, after


# ── Is the request about one part of the document, or all of it? ──────────
#
# Pruning a whole-document request (review, summarize, plan, find every gap)
# loses the specific findings that make the answer useful, so only a question
# that clearly targets one part is pruned. Anything unclear stays verbatim.
_WHOLE_DOC_RE = re.compile(
    r"\b(review|summari[sz]e|summary|overview|tl;?dr|critique|audit|proofread|"
    r"feedback|improve|rewrite|refactor|translate|convert|reformat|edit|polish|"
    r"go (?:through|over)|walk me through|read (?:it|this|the)|analy[sz]e|assess|evaluate|"
    r"(?:a|me a|make a|write a|draft a) plan|plan (?:for|out)|roadmap|"
    r"build (?:it|this)|implement (?:it|this)|"
    r"gaps|risks|(?:any|all|main|biggest|key|major|top|open) (?:gap|risk)\b|risky|"
    r"look (?:over|at|into) (?:it|this)|makes? sense|(?:score|rate|grade|judge) (?:it|this)|out of 10|"
    r"ready to (?:ship|go|merge|launch|publish)|good enough|sound|"
    r"would you|should (?:i|we)|reasonable|sensible|better|worse|recommend\w*|suggest\w*|"
    r"issues|problems|weakness(?:es)?|inconsisten\w*|conflicts?|contradict\w*|"
    r"missing|everything|all of|whole|entire|every|each|overall|"
    r"what do you think|thoughts|opinion|explain (?:it|this|the (?:doc|document|file|spec|readme))|"
    r"key (?:points|takeaways)|main (?:points|ideas|limitations)|takeaways|highlights|"
    r"checklist|action items|next steps|compare)\b",
    re.I,
)
_ASK_RE = re.compile(
    r"(^|[.!?,;:]\s+|\n)\s*(what|which|how|when|where|why|who|does|do|is|are|can|should|"
    r"according to|find|show me|tell me|list the \w+ (?:for|of|in)|give me the)\b",
    re.I,
)
_GENERIC_WORDS = frozenset(
    "a an the this that these those it its in on of for to from about and or with by is are "
    "does do what which how when where why who can should according say says said tell me "
    "document doc file spec specification readme plan page text here above below there "
    "show give find you your please i my we our".split()
)


def question_scope(question: str, document: str | None = None) -> str | None:
    """'narrow' for a question about one part of a document, 'broad' for the whole
    document, None when unclear (callers treat None like 'broad').

    With ``document``, a narrow question must name at least two things the
    document itself mentions: "is this ready to ship?" names nothing in it.
    """
    q = " ".join(question.split())
    if not q:
        return None
    if _WHOLE_DOC_RE.search(q):
        return "broad"
    content = {
        w.strip(".-")
        for w in re.findall(r"[A-Za-z0-9][\w.-]*", q.lower())
        if w not in _GENERIC_WORDS and len(w.strip(".-")) >= 3
    }
    if document is not None:
        doc_words = set(re.findall(r"[a-z0-9][\w-]*", document.lower()))
        content = {w for w in content if w in doc_words or w.rstrip("s") in doc_words}
    if _ASK_RE.search(q) and len(content) >= 2:
        return "narrow"
    return None


_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")
SECTION_OMITTED = "[Section omitted: not relevant to this request.]"


def _sections(text: str) -> list[str]:
    """Split markdown at headings outside code fences; each section keeps its heading."""
    sections: list[list[str]] = [[]]
    in_fence = False
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence and _MD_HEADING_RE.match(line) and sections[-1]:
            sections.append([])
        sections[-1].append(line)
    return ["\n".join(s) for s in sections]


_TERM_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")


def _terms(text: str) -> list[str]:
    return [t.rstrip("s") if len(t) > 3 else t for t in _TERM_RE.findall(text.lower())]


def bm25_section_scores(sections: list[str], question: str, k1: float = 1.2, b: float = 0.75) -> list[float]:
    """Keyword relevance of each section to the question (BM25; headings count twice)."""
    import math

    docs = []
    for s in sections:
        heading = s.split("\n", 1)[0]
        docs.append(_terms(s) + (_terms(heading) if _MD_HEADING_RE.match(heading) else []))
    q_terms = {t for t in _terms(question) if t not in _GENERIC_WORDS and len(t) >= 3}
    n = len(docs)
    avg = sum(len(d) for d in docs) / max(n, 1)
    df = {t: sum(1 for d in docs if t in d) for t in q_terms}
    scores = []
    for d in docs:
        tf: dict[str, int] = {}
        for t in d:
            if t in q_terms:
                tf[t] = tf.get(t, 0) + 1
        s = 0.0
        for t, f in tf.items():
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * len(d) / max(avg, 1)))
        scores.append(s)
    return scores


def prune_sections(
    text: str, question: str, remove_fraction: float, embed: Embed | None
) -> tuple[str, int, int]:
    """Keep the sections most relevant to ``question`` whole; omit the rest.

    A section scores by its best-matching sentence (a narrow question names one
    part of a document) plus its average. Omitted sections keep their heading
    so the reader still sees the document's shape. Returns (text, words before,
    words after).
    """
    secs = _sections(text)
    words = [len(s.split()) for s in secs]
    total = sum(words)
    if remove_fraction <= 0 or not question.strip() or len(secs) < 3:
        return text, total, total
    per_section = [
        [u for line in s.split("\n") for u in _split_line(line)[1] if len(u.split()) >= 3] for s in secs
    ]
    if embed is None:  # keyword scoring: milliseconds, for slow hosts
        kw = bm25_section_scores(secs, question)
        return _keep_sections(secs, words, [
            float("inf") if not units else kw[i] for i, units in enumerate(per_section)
        ], remove_fraction)
    flat = [u for units in per_section for u in units]
    vecs = embed([question] + flat)  # one call: batches share padding work
    rel_all = vecs[1:] @ vecs[0]
    scores = []
    pos = 0
    for units in per_section:
        if not units:
            scores.append(float("inf"))  # headings alone cost nothing to keep
            continue
        rel = rel_all[pos : pos + len(units)]
        pos += len(units)
        scores.append(float(rel.max() + 0.3 * rel.mean()))
    return _keep_sections(secs, words, scores, remove_fraction)


def _keep_sections(
    secs: list[str], words: list[int], scores: list[float], remove_fraction: float
) -> tuple[str, int, int]:
    """Keep the best-scoring sections up to the word budget; the rest become headings."""
    total = sum(words)
    keep = [False] * len(secs)
    kept, budget = 0, total * (1 - remove_fraction)
    for i in sorted(range(len(secs)), key=lambda i: -scores[i]):
        if kept >= budget and scores[i] != float("inf"):
            continue
        keep[i] = True
        kept += words[i]
    out = []
    for s, k in zip(secs, keep):
        if k:
            out.append(s)
        else:
            heading = s.split("\n", 1)[0]
            out.append(heading + "\n" + SECTION_OMITTED + "\n" if _MD_HEADING_RE.match(heading) else SECTION_OMITTED + "\n")
    return "\n".join(out), total, kept


# ── Sentence embedder for the proxy ────────────────────────────────────────

logger = logging.getLogger(__name__)
EMBED_REPO = "Qdrant/all-MiniLM-L6-v2-onnx"


class PruneDeadline(Exception):
    """Embedding ran past its time budget; the caller leaves the document alone."""


class SentenceEmbedder:
    """all-MiniLM-L6-v2 over ONNX Runtime, loaded once in the background.

    Batches are padded to their longest sentence (not a fixed length), which is
    3-4x faster on short sentences. ``embed`` raises :class:`PruneDeadline` once
    ``deadline`` (a ``time.monotonic()`` value) has passed.
    """

    def __init__(self, threads: int = 2, max_tokens: int = 128, batch: int = 32) -> None:
        self._threads, self._max_tokens, self._batch = threads, max_tokens, batch
        self._session: object | None = None
        self._tokenizer: object | None = None
        self._inputs: list[str] = []
        self._loading = threading.Lock()
        self._failed = False

    @property
    def ready(self) -> bool:
        return self._session is not None

    def warm(self) -> None:
        """Start loading in a background thread (no-op once loaded or loading)."""
        if self._session is not None or self._failed or self._loading.locked():
            return
        threading.Thread(target=self._load, name="doc-embedder-load", daemon=True).start()

    def _load(self) -> None:
        if not self._loading.acquire(blocking=False):
            return
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer

            from horizon.onnx_runtime import create_cpu_session_options, hf_hub_download_local_first

            model = hf_hub_download_local_first(EMBED_REPO, "model.onnx")
            tok = Tokenizer.from_file(hf_hub_download_local_first(EMBED_REPO, "tokenizer.json"))
            tok.enable_truncation(max_length=self._max_tokens)
            tok.no_padding()
            opts = create_cpu_session_options(
                ort, intra_op_num_threads=self._threads, inter_op_num_threads=1
            )
            session = ort.InferenceSession(model, opts, providers=["CPUExecutionProvider"])
            self._inputs = [i.name for i in session.get_inputs()]
            self._tokenizer, self._session = tok, session
        except Exception as exc:  # noqa: BLE001 - never break the proxy
            self._failed = True
            logger.warning("Document embedder unavailable; query pruning off: %s", exc)
        finally:
            self._loading.release()

    def embed(self, texts: list[str], deadline: float | None = None) -> np.ndarray:
        assert self._session is not None and self._tokenizer is not None
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        rows: list[np.ndarray | None] = [None] * len(texts)
        for start in range(0, len(order), self._batch):
            if deadline is not None and time.monotonic() > deadline:
                raise PruneDeadline
            idx = order[start : start + self._batch]
            encs = self._tokenizer.encode_batch([texts[i] for i in idx])  # type: ignore[attr-defined]
            width = max(1, max(len(e.ids) for e in encs))
            ids = np.zeros((len(idx), width), np.int64)
            mask = np.zeros((len(idx), width), np.int64)
            for r, e in enumerate(encs):
                ids[r, : len(e.ids)] = e.ids
                mask[r, : len(e.ids)] = 1
            feeds = {}
            for name in self._inputs:
                if "input_ids" in name:
                    feeds[name] = ids
                elif "attention_mask" in name:
                    feeds[name] = mask
                elif "token_type_ids" in name:
                    feeds[name] = np.zeros_like(ids)
            out = self._session.run(None, feeds)[0]  # type: ignore[attr-defined]
            m = mask[:, :, None].astype(np.float32)
            vecs = (out * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None)
            vecs /= np.clip(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-9, None)
            for r, i in enumerate(idx):
                rows[i] = vecs[r]
        return np.vstack(rows) if rows else np.zeros((0, 384), np.float32)


_EMBEDDER: SentenceEmbedder | None = None
_EMBEDDER_LOCK = threading.Lock()


def shared_embedder() -> SentenceEmbedder:
    global _EMBEDDER
    with _EMBEDDER_LOCK:
        if _EMBEDDER is None:
            _EMBEDDER = SentenceEmbedder(
                threads=max(1, int(os.environ.get("HORIZON_PROMPT_DOCUMENTS_QUERY_THREADS", "2") or 2))
            )
        return _EMBEDDER
