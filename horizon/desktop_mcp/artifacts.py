"""Model-facing artifact renderers and a session-scoped artifact store.

Renderers are pure functions over ``ResultTable``. The store writes each
rendered artifact through ``PinnedStagingDirectory`` under a generated session
directory, using generated identifiers and file names only. Every read and
manifest call re-checks the artifact's dependencies, so a revoked source makes
the artifact unavailable and removes its file.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .staging_fs import PinnedStagingDirectory, StagingFilesystemError
from .tables import ResultTable

_MAX_STAGING_FILE_BYTES = 8 * 1024 * 1024
_MAX_TITLE_CHARS = 120
_PREVIEW_ROWS = 5
_FORMATS = {"csv": "csv", "json": "json", "markdown": "md", "text": "txt"}
_POLICIES = frozenset({"escape", "reject", "raw"})
_FORMULA_PREFIXES = ("=", "@", "\t", "\r")
_NUMBER = re.compile(r"[+-](?:[0-9]+\.?[0-9]*|\.[0-9]+)")
_ARTIFACT_ID = re.compile(r"[0-9a-f]{32}")
_NEWLINE = re.compile(r"\r\n|\r|\n")
_MD_LEADING = frozenset("#>*+-_`~<![]")
_MD_ORDERED = re.compile(r"([0-9]+)([.)])")


class ArtifactError(ValueError):
    """Sanitized artifact failure with a stable code and no source text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


_SESSION_DIR = re.compile(r"\.contextshrink-artifacts-[0-9a-f]{32}")
_ARTIFACT_FILE = re.compile(r"artifact-[0-9a-f]{32}\.(?:csv|json|md|txt)")
# Artifacts live at most an hour; a file older than this plus a margin is
# expired in every session, so removing it can never affect live results.
STALE_ARTIFACT_SECONDS = 3600 + 300
_REPARSE_POINT = 0x400


def _plain(entry: os.DirEntry[str], *, directory: bool) -> bool:
    """True for a real file/directory: no symlink, junction or reparse point."""
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    if entry.is_symlink() or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
        return False
    return stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)


def sweep_stale_sessions(base_dir: Path, *, now: float) -> int:
    """Best-effort removal of expired artifact files left by a crashed session.

    Only generated names are touched, links are never followed, and nothing is
    removed recursively; an empty session directory is removed last.
    """
    removed = 0
    try:
        sessions = list(os.scandir(base_dir))
    except OSError:
        return 0
    for session in sessions:
        if not _SESSION_DIR.fullmatch(session.name) or not _plain(session, directory=True):
            continue
        try:
            files = list(os.scandir(session.path))
        except OSError:
            continue
        for item in files:
            if not _ARTIFACT_FILE.fullmatch(item.name) or not _plain(item, directory=False):
                continue
            try:
                if now - item.stat(follow_symlinks=False).st_mtime > STALE_ARTIFACT_SECONDS:
                    os.unlink(item.path)
                    removed += 1
            except OSError:
                continue
        try:
            os.rmdir(session.path)
        except OSError:
            pass
    return removed


@dataclass(frozen=True)
class Dependency:
    """A source snapshot an artifact was derived from."""

    source_id: str
    version: str
    root_key: str


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _text(value: str | None) -> str:
    return "" if value is None else value


def _check_policy(formula_policy: str) -> None:
    if formula_policy not in _POLICIES:
        raise ArtifactError("artifact_formula_policy_invalid")


def _is_formula(value: str) -> bool:
    if value.startswith(_FORMULA_PREFIXES):
        return True
    if value.startswith(("+", "-")):
        # A signed decimal lexeme is a number, not a formula.
        return _NUMBER.fullmatch(value) is None
    return False


def _csv_cell(value: str | None, formula_policy: str) -> str:
    text = _text(value)
    if _is_formula(text):
        if formula_policy == "reject":
            raise ArtifactError("artifact_formula_cell")
        if formula_policy == "escape":
            text = "'" + text
    if any(char in text for char in ',"\r\n'):
        text = '"' + text.replace('"', '""') + '"'
    return text


def _csv_line(cells: list[str]) -> str:
    # A lone empty field would read back as a blank line, so quote it.
    if len(cells) == 1 and cells[0] == "":
        cells = ['""']
    return ",".join(cells) + "\r\n"


def render_csv(table: ResultTable, *, formula_policy: str = "escape") -> str:
    """RFC 4180 CSV with CRLF terminators and the requested formula policy."""

    _check_policy(formula_policy)
    lines = [_csv_line([_csv_cell(name, formula_policy) for name in table.columns])]
    for row in table.rows:
        lines.append(_csv_line([_csv_cell(value, formula_policy) for value in row]))
    return "".join(lines)


def render_json(table: ResultTable) -> str:
    """Array of objects keyed by column name, in column order."""

    # Duplicate names would silently drop a column's values from each object.
    if len(set(table.columns)) != len(table.columns):
        raise ArtifactError("artifact_columns_invalid")
    records = [dict(zip(table.columns, row, strict=True)) for row in table.rows]
    return json.dumps(records, ensure_ascii=False, indent=2) + "\n"


def _md_cell(value: str | None) -> str:
    text = _text(value).replace("\\", "\\\\").replace("|", "\\|")
    text = _NEWLINE.sub("<br>", text)
    ordered = _MD_ORDERED.match(text)
    if ordered:
        text = ordered.group(1) + "\\" + ordered.group(2) + text[ordered.end() :]
    elif text and text[0] in _MD_LEADING:
        text = "\\" + text
    return text


def _md_row(cells: Iterable[str]) -> str:
    return "| " + " | ".join(cells) + " |\n"


def render_markdown(table: ResultTable, *, title: str | None = None) -> str:
    """Display-oriented Markdown table; not a lossless export."""

    parts: list[str] = []
    if title is not None:
        parts.append(f"# {_md_cell(title)}\n\n")
    parts.append(_md_row(_md_cell(name) for name in table.columns))
    parts.append(_md_row("---" for _ in table.columns))
    for row in table.rows:
        parts.append(_md_row(_md_cell(value) for value in row))
    return "".join(parts)


def render_text(table: ResultTable) -> str:
    """Tab-separated plain text for display; newlines and tabs become spaces."""

    def cell(value: str | None) -> str:
        return _NEWLINE.sub(" ", _text(value)).replace("\t", " ")

    lines = ["\t".join(cell(name) for name in table.columns)]
    for row in table.rows:
        lines.append("\t".join(cell(value) for value in row))
    return "\n".join(lines) + "\n"


def _render(fmt: str, table: ResultTable, title: str, formula_policy: str) -> str:
    if fmt == "csv":
        return render_csv(table, formula_policy=formula_policy)
    if fmt == "json":
        return render_json(table)
    if fmt == "markdown":
        return render_markdown(table, title=title)
    return render_text(table)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def _validate_title(title: object) -> str:
    if (
        not isinstance(title, str)
        or not 1 <= len(title) <= _MAX_TITLE_CHARS
        or not title.isprintable()
    ):
        raise ArtifactError("artifact_title_invalid")
    return title


def _validate_dependencies(dependencies: object) -> tuple[Dependency, ...]:
    if isinstance(dependencies, (str, bytes)) or not isinstance(dependencies, Iterable):
        raise ArtifactError("artifact_dependency_invalid")
    result = tuple(dependencies)
    if not all(isinstance(item, Dependency) for item in result):
        raise ArtifactError("artifact_dependency_invalid")
    return result


def _iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class _Entry:
    artifact_id: str
    filename: str
    identity: tuple[int, int]
    sha256: str
    utf8_bytes: int
    dependencies: tuple[Dependency, ...]
    expires_epoch: float
    manifest: dict[str, Any]


class ArtifactStore:
    """Session-owned rendered artifacts with limits, expiry and revocation."""

    def __init__(
        self,
        base_dir: Path,
        *,
        dependency_check: Callable[[tuple[Dependency, ...]], bool],
        max_artifact_bytes: int = _MAX_STAGING_FILE_BYTES,
        max_total_bytes: int = 128 * 1024 * 1024,
        max_items: int = 64,
        ttl_seconds: float = 3600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not 1 <= max_artifact_bytes <= _MAX_STAGING_FILE_BYTES:
            raise ValueError("max_artifact_bytes out of range")
        if max_total_bytes < 1 or max_items < 1 or ttl_seconds <= 0:
            raise ValueError("artifact store limits must be positive")
        if not callable(dependency_check):
            raise TypeError("dependency_check must be callable")
        self._dependency_check = dependency_check
        self._max_artifact_bytes = max_artifact_bytes
        self._max_total_bytes = max_total_bytes
        self._max_items = max_items
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: dict[str, _Entry] = {}
        self._pending: dict[str, tuple[int, int]] = {}
        self._bytes = 0
        self._closed = False
        session_name = f".contextshrink-artifacts-{uuid.uuid4().hex}"
        sweep_stale_sessions(Path(base_dir), now=clock())
        try:
            self._fs = PinnedStagingDirectory.open(Path(base_dir), session_name)
        except (StagingFilesystemError, OSError, ValueError):
            raise ArtifactError("artifact_store_unavailable") from None

    # -- lifecycle helpers -------------------------------------------------

    def _require_open(self) -> None:
        if self._closed:
            raise ArtifactError("artifact_store_closed")

    def _check_directory(self) -> None:
        try:
            self._fs.check_directory()
        except (OSError, RuntimeError, ValueError, StagingFilesystemError):
            raise ArtifactError("artifact_unavailable") from None

    def _delete_file(self, filename: str, identity: tuple[int, int]) -> bool:
        try:
            deleted = self._fs.delete_owned(filename, identity)
        except (OSError, RuntimeError, ValueError, StagingFilesystemError):
            deleted = False
        if not deleted:
            self._pending[filename] = identity
        return deleted

    def _remove_locked(self, artifact_id: str) -> bool:
        entry = self._entries.pop(artifact_id, None)
        if entry is None:
            return True
        self._bytes -= entry.utf8_bytes
        return self._delete_file(entry.filename, entry.identity)

    def _expire_locked(self) -> None:
        now = self._clock()
        for artifact_id in [
            key for key, entry in self._entries.items() if entry.expires_epoch <= now
        ]:
            self._remove_locked(artifact_id)

    def _lookup_locked(self, artifact_id: object) -> _Entry:
        if not isinstance(artifact_id, str) or not _ARTIFACT_ID.fullmatch(artifact_id):
            raise ArtifactError("artifact_unknown")
        entry = self._entries.get(artifact_id)
        if entry is None:
            raise ArtifactError("artifact_unknown")
        return entry

    def _dependencies_ok(self, entry: _Entry) -> bool:
        try:
            result = self._dependency_check(entry.dependencies)
        except Exception:
            return False
        return result is True

    def _check_dependencies_locked(self, entry: _Entry) -> None:
        if not self._dependencies_ok(entry):
            self._remove_locked(entry.artifact_id)
            raise ArtifactError("artifact_revoked")

    def _open_locked(self, artifact_id: object) -> _Entry:
        self._require_open()
        self._expire_locked()
        entry = self._lookup_locked(artifact_id)
        self._check_dependencies_locked(entry)
        return entry

    def _verified_path_locked(self, entry: _Entry) -> Path:
        """Return the owned file path after confirming it is still the staged file."""

        self._check_directory()
        try:
            owned = self._fs.verify_owned(entry.filename, entry.identity)
        except (OSError, RuntimeError, ValueError, StagingFilesystemError):
            owned = False
        if not owned:
            self._remove_locked(entry.artifact_id)
            raise ArtifactError("artifact_unavailable")
        return self._fs.directory / entry.filename

    # -- public API -----------------------------------------------------------

    def create(
        self,
        fmt: str,
        table: ResultTable,
        *,
        title: str,
        dependencies: Iterable[Dependency],
        formula_policy: str = "escape",
    ) -> dict[str, Any]:
        if fmt not in _FORMATS:
            raise ArtifactError("artifact_format_unsupported")
        title = _validate_title(title)
        deps = _validate_dependencies(dependencies)
        _check_policy(formula_policy)
        text = _render(fmt, table, title, formula_policy)
        try:
            data = text.encode("utf-8")
        except UnicodeEncodeError:
            raise ArtifactError("artifact_text_invalid") from None
        size = len(data)

        with self._lock:
            self._require_open()
            self._expire_locked()
            if size > self._max_artifact_bytes:
                raise ArtifactError("artifact_too_large")
            if len(self._entries) >= self._max_items or self._bytes + size > self._max_total_bytes:
                raise ArtifactError("artifact_store_full")
            self._check_directory()

            artifact_id = uuid.uuid4().hex
            filename = f"artifact-{artifact_id}.{_FORMATS[fmt]}"
            try:
                identity = self._fs.write_exclusive(filename, data)
            except (OSError, RuntimeError, ValueError, StagingFilesystemError):
                raise ArtifactError("artifact_unavailable") from None

            created = self._clock()
            expires = created + self._ttl_seconds
            digest = hashlib.sha256(data).hexdigest()
            manifest = {
                "artifact_id": artifact_id,
                "format": fmt,
                "title": title,
                "utf8_bytes": size,
                "sha256": digest,
                "row_count": len(table.rows),
                "columns": list(table.columns),
                "preview_rows": [list(row) for row in table.rows[:_PREVIEW_ROWS]],
                "dependencies": [
                    {"source_id": dep.source_id, "version": dep.version} for dep in deps
                ],
                "created_at": _iso_utc(created),
                "expires_at": _iso_utc(expires),
            }
            self._entries[artifact_id] = _Entry(
                artifact_id=artifact_id,
                filename=filename,
                identity=identity,
                sha256=digest,
                utf8_bytes=size,
                dependencies=deps,
                expires_epoch=expires,
                manifest=manifest,
            )
            self._bytes += size
            return copy.deepcopy(manifest)

    def read(self, artifact_id: str) -> bytes:
        with self._lock:
            entry = self._open_locked(artifact_id)
            path = self._verified_path_locked(entry)
            try:
                with open(path, "rb") as handle:
                    data = handle.read(entry.utf8_bytes + 1)
                still_owned = self._fs.verify_owned(entry.filename, entry.identity)
            except (OSError, RuntimeError, ValueError, StagingFilesystemError):
                self._remove_locked(entry.artifact_id)
                raise ArtifactError("artifact_unavailable") from None
            if (
                not still_owned
                or len(data) != entry.utf8_bytes
                or hashlib.sha256(data).hexdigest() != entry.sha256
            ):
                self._remove_locked(entry.artifact_id)
                raise ArtifactError("artifact_unavailable")
            return data

    def manifest(self, artifact_id: str) -> dict[str, Any]:
        with self._lock:
            entry = self._open_locked(artifact_id)
            return copy.deepcopy(entry.manifest)

    def list(self) -> list[dict[str, Any]]:
        """Manifests of live artifacts in creation order; revoked ones are dropped."""

        with self._lock:
            self._require_open()
            self._expire_locked()
            manifests: list[dict[str, Any]] = []
            for entry in list(self._entries.values()):
                if self._dependencies_ok(entry):
                    manifests.append(copy.deepcopy(entry.manifest))
                else:
                    self._remove_locked(entry.artifact_id)
            return manifests

    def delete(self, artifact_id: str) -> bool:
        """Remove one artifact. Returns True when its file removal was confirmed."""

        with self._lock:
            self._require_open()
            self._expire_locked()
            self._lookup_locked(artifact_id)
            return self._remove_locked(artifact_id)

    def invalidate_where(self, predicate: Callable[[dict[str, Any]], bool]) -> int:
        """Remove artifacts whose manifest copy matches the predicate; return the count."""

        with self._lock:
            self._require_open()
            self._expire_locked()
            matches = [
                entry.artifact_id
                for entry in list(self._entries.values())
                if predicate(copy.deepcopy(entry.manifest))
            ]
            for artifact_id in matches:
                self._remove_locked(artifact_id)
            return len(matches)

    def expire(self) -> int:
        """Remove expired artifacts now; return how many were removed."""

        with self._lock:
            self._require_open()
            before = len(self._entries)
            self._expire_locked()
            return before - len(self._entries)

    def export_path(self, artifact_id: str) -> Path:
        """Path for the companion's user-operated Save As only; never model-facing."""

        with self._lock:
            entry = self._open_locked(artifact_id)
            return self._verified_path_locked(entry)

    def close(self) -> bool:
        """Remove every owned file and the session directory. True when all removed."""

        with self._lock:
            if self._closed:
                return not self._entries and not self._pending
            complete = True
            for artifact_id in list(self._entries):
                if not self._remove_locked(artifact_id):
                    complete = False
            for filename, identity in list(self._pending.items()):
                try:
                    deleted = self._fs.delete_owned(filename, identity)
                except (OSError, RuntimeError, ValueError, StagingFilesystemError):
                    deleted = False
                if deleted:
                    self._pending.pop(filename, None)
                else:
                    complete = False
            self._closed = True
            try:
                directory_removed = self._fs.close(remove_directory=True)
            except (OSError, RuntimeError, ValueError, StagingFilesystemError):
                directory_removed = False
            return complete and directory_removed and not self._pending

    def __enter__(self) -> ArtifactStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = [
    "ArtifactError",
    "ArtifactStore",
    "Dependency",
    "render_csv",
    "render_json",
    "render_markdown",
    "render_text",
]
