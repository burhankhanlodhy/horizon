"""Reversible encoding for timestamped log lines (transform ``log_templates``).

A line that starts with a timestamp is split into the timestamp and its "rest":
the text after the timestamp and one following space. Any rest that occurs at
least three times is listed once in a legend at the top, as ``@k = <rest>``,
numbered by first occurrence. A line whose rest is in the legend is written as
``<timestamp> @k``; every other line is written unchanged.

Date-prefix form (``date_prefix=True``): an ISO-8601 ``YYYY-MM-DDT`` timestamp
has its date written once in a header line ``## date YYYY-MM-DDT``. The body
line then starts with the time alone, and the date is the most recent header.
A header is written whenever the date changes. Other timestamp styles keep the
full form.

:func:`encode_log_templates` returns the smallest verified candidate of the
plain and date-prefix forms, or ``None``. It returns ``None`` when a source
line could be confused with the syntax (a token that starts with ``@`` and a
digit, a line that starts with ``##``, or, for the date-prefix form, a line
that starts with ``HH:MM:SS``).
"""

from __future__ import annotations

import re
from collections import Counter

__all__ = [
    "LOG_TEMPLATE_MIN_REPEATS",
    "encode_log_templates",
    "expand_log_templates",
    "looks_like_log",
]

#: A rest must occur at least this many times to earn a legend entry.
LOG_TEMPLATE_MIN_REPEATS = 3

# Strict leading timestamp, which must be followed by exactly one space:
#   ISO-8601 with seconds: 2026-08-01T06:00:01Z, 2026-08-01 06:00:01.123+02:00
#   syslog:                Aug 16 11:02:00, Aug  6 11:02:00
#   common log format:     10/Oct/2000:13:55:36 -0700
_TIMESTAMP_LINE_RE = re.compile(
    r"^(?P<ts>"
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,9})?(?:Z|[+-]\d{2}:?\d{2})?"
    r"|[A-Z][a-z]{2} [ \d]\d \d{2}:\d{2}:\d{2}"
    r"|\d{2}/[A-Z][a-z]{2}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4}"
    r") "
)
# An ISO-8601 timestamp with a "T" separator, split into date and time.
_ISO_T_STAMP_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}T)(?P<time>\d{2}:\d{2}:\d{2}(?:[.,]\d{1,9})?(?:Z|[+-]\d{2}:?\d{2})?)$"
)

# Syntax of the output. A source line must not match any of these in a way the
# decoder could read as syntax.
_SYNTAX_COLLISION_RE = re.compile(r"(?:^|\s)@\d", re.MULTILINE)
_HEADER_COLLISION_RE = re.compile(r"^##", re.MULTILINE)
_TIME_COLLISION_RE = re.compile(r"^\d{2}:\d{2}:\d{2}", re.MULTILINE)
_LEGEND_LINE_RE = re.compile(r"^@(?P<k>\d+) = (?P<rest>.*)$")
_HEADER_LINE_RE = re.compile(r"^## date (?P<date>\d{4}-\d{2}-\d{2}T)$")
_TIME_LINE_RE = re.compile(r"^\d{2}:\d{2}:\d{2}(?:[.,]\d{1,9})?(?:Z|[+-]\d{2}:?\d{2})? ")
_REFERENCE_LINE_RE = re.compile(r"^(?P<ts>.*) @(?P<k>\d+)$")

#: Share of non-blank lines that must start with a timestamp for a file to count as a log.
_LOG_SHAPE_MIN_SHARE = 0.8


def looks_like_log(text: str) -> bool:
    """True when at least 80% of the non-blank lines start with a strict timestamp."""
    total = 0
    stamped = 0
    for line in text.split("\n"):
        if not line.strip():
            continue
        total += 1
        if _TIMESTAMP_LINE_RE.match(line):
            stamped += 1
    return total > 0 and stamped >= total * _LOG_SHAPE_MIN_SHARE


def _render(
    text: str,
    lines: list[str],
    stamps: list[tuple[str, str] | None],
    legend: dict[str, int],
    *,
    dated: bool,
) -> str | None:
    """Build one candidate; ``None`` when it would not reproduce ``text``."""
    body: list[str] = []
    active: str | None = None
    headers = 0
    for line, stamp in zip(lines, stamps):
        if stamp is None:
            body.append(line)
            continue
        ts, rest = stamp
        payload = f"@{legend[rest]}" if rest in legend else rest
        iso = _ISO_T_STAMP_RE.match(ts) if dated else None
        if iso is None:
            body.append(f"{ts} {payload}")
            continue
        if iso.group("date") != active:
            active = iso.group("date")
            body.append(f"## date {active}")
            headers += 1
        body.append(f"{iso.group('time')} {payload}")
    if dated and headers == 0:
        return None
    if not dated and not legend:
        return None
    header = [f"@{number} = {rest}" for rest, number in legend.items()]
    encoded = "\n".join(header + body)
    if expand_log_templates(encoded) != text:
        return None
    return encoded


def encode_log_templates(text: str, *, date_prefix: bool = True) -> str | None:
    """Return the smallest verified encoding of ``text``, or ``None``.

    ``date_prefix=False`` allows only the legend form. ``date_prefix=True`` also
    allows the date-prefix form, and the smallest verified result of the two
    wins (ties prefer the legend form). ``None`` means neither form applies:
    no rest repeats often enough, or a line collides with the syntax. Callers
    still compare ``expand_log_templates(result)`` with the input.
    """
    if not text or _SYNTAX_COLLISION_RE.search(text) or _HEADER_COLLISION_RE.search(text):
        return None
    lines = text.split("\n")
    stamps: list[tuple[str, str] | None] = []
    counts: Counter[str] = Counter()
    for line in lines:
        match = _TIMESTAMP_LINE_RE.match(line)
        if match is None:
            stamps.append(None)
            continue
        rest = line[match.end() :]
        stamps.append((match.group("ts"), rest))
        counts[rest] += 1

    legend: dict[str, int] = {}
    for stamp in stamps:
        if stamp is None:
            continue
        rest = stamp[1]
        if counts[rest] >= LOG_TEMPLATE_MIN_REPEATS and rest not in legend:
            legend[rest] = len(legend) + 1

    candidates: list[str] = []
    plain = _render(text, lines, stamps, legend, dated=False)
    if plain is not None:
        candidates.append(plain)
    if date_prefix and not _TIME_COLLISION_RE.search(text):
        dated = _render(text, lines, stamps, legend, dated=True)
        if dated is not None:
            candidates.append(dated)
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: len(candidate.encode("utf-8")))


def expand_log_templates(text: str) -> str:
    """Exact inverse of :func:`encode_log_templates` for its own outputs."""
    lines = text.split("\n")
    legend: dict[int, str] = {}
    start = 0
    while start < len(lines):
        match = _LEGEND_LINE_RE.match(lines[start])
        if match is None:
            break
        legend[int(match.group("k"))] = match.group("rest")
        start += 1

    out: list[str] = []
    active: str | None = None
    for line in lines[start:]:
        header = _HEADER_LINE_RE.match(line)
        if header is not None:
            active = header.group("date")
            continue
        if active is not None and _TIME_LINE_RE.match(line):
            line = active + line
        match = _REFERENCE_LINE_RE.match(line)
        if match is not None and int(match.group("k")) in legend:
            out.append(f"{match.group('ts')} {legend[int(match.group('k'))]}")
        else:
            out.append(line)
    return "\n".join(out)
