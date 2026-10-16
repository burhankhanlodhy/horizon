"""Bounded, approved-root access to local text sources.

This module is deliberately independent of the MCP transport and broker. It
does not grant roots: callers must construct it only from locally approved
settings. Paths supplied by a caller are merely locators beneath those roots.
"""

from __future__ import annotations

import hashlib
import ntpath
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from .extractors import (
    DOCUMENT_KINDS,
    DocumentBlock,
    ExtractedDocument,
    ExtractionError,
    extract_document,
)

_DEFAULT_MAX_SOURCE_BYTES = 8 * 1024 * 1024
_MAX_CATALOG_ENTRIES = 1000
_MAX_CATALOG_LIMIT = 100
_SUPPORTED_SUFFIXES = {
    ".txt",
    ".md",
    ".markdown",
    ".json",
    ".jsonl",
    ".ndjson",
    ".log",
    ".csv",
    ".tsv",
    ".yaml",
    ".yml",
    ".ini",
    ".toml",
    ".conf",
    ".config",
    ".diff",
    ".patch",
    ".paths",
    ".search",
    ".pdf",
    ".docx",
}
_WINDOWS_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.I)


class SourceAccessError(Exception):
    """A generic source-access failure safe to return across the MCP boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Source access error ({code})")


@dataclass(frozen=True)
class OpenedSource:
    """Exact UTF-8 text and identity captured from one validated file handle."""

    root_key: str
    relative_path: str
    text: str
    digest: str
    byte_count: int
    identity: str
    # "text-v1" for UTF-8 text sources; e.g. "pdf-pypdf-6.20.1-v1" for documents.
    extractor_version: str = "text-v1"
    # Document block spans into ``text`` (PDF pages, DOCX paragraphs/tables).
    blocks: tuple[DocumentBlock, ...] = ()
    # Stable omission/reliability codes reported by the document extractor.
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ApprovedRoot:
    path: Path
    final_path: str
    identity: str
    root_key: str


class SourceReader:
    """Read approved local text files and enumerate bounded source metadata."""

    def __init__(
        self,
        approved_roots: tuple[Path, ...],
        max_source_bytes: int = _DEFAULT_MAX_SOURCE_BYTES,
    ) -> None:
        if not isinstance(max_source_bytes, int) or isinstance(max_source_bytes, bool):
            raise ValueError("max_source_bytes must be an integer")
        if not 1 <= max_source_bytes <= 25 * 1024 * 1024:
            raise ValueError("max_source_bytes is outside the supported range")
        self.max_source_bytes = max_source_bytes
        self._catalog_truncated = False
        self._catalog_unreadable_entries = 0
        roots: list[_ApprovedRoot] = []
        for supplied in approved_roots:
            root = self._prepare_root(supplied)
            if all(existing.root_key != root.root_key for existing in roots):
                roots.append(root)
        self._roots = tuple(roots)

    @property
    def catalog_truncated(self) -> bool:
        """Whether the most recent catalog scan reached its traversal cap."""

        return self._catalog_truncated

    @property
    def catalog_unreadable_entries(self) -> int:
        """Number of directory entries skipped after a metadata access error."""

        return self._catalog_unreadable_entries

    def read(self, source: str) -> OpenedSource:
        """Read a bounded exact UTF-8 snapshot beneath an approved root."""

        root, candidate, relative = self._locate(source)
        descriptor = self._open_validated(root, candidate)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise SourceAccessError("source_unavailable")
            _require_single_link(before)
            self._check_cloud_state(descriptor)
            before_identity = _identity_for_fd(descriptor, before)
            if before.st_size > self.max_source_bytes:
                raise SourceAccessError("source_too_large")
            chunks: list[bytes] = []
            total = 0
            while total <= self.max_source_bytes:
                chunk = os.read(descriptor, min(64 * 1024, self.max_source_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            if total > self.max_source_bytes:
                raise SourceAccessError("source_too_large")
            after = os.fstat(descriptor)
            after_identity = _identity_for_fd(descriptor, after)
            if before_identity != after_identity or _file_state(before) != _file_state(after):
                raise SourceAccessError("source_changed")
            raw = b"".join(chunks)
            kind = candidate.suffix.casefold().lstrip(".")
            if kind in DOCUMENT_KINDS:
                # Extraction runs only after every handle, size and change check.
                document = _extract_or_raise(raw, kind)
                text = document.text
                extractor_version = document.extractor_version
                blocks = document.blocks
                warnings = document.warnings
            else:
                if b"\x00" in raw:
                    raise SourceAccessError("source_invalid_text")
                try:
                    text = raw.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    raise SourceAccessError("source_invalid_text") from None
                extractor_version, blocks, warnings = "text-v1", (), ()
            return OpenedSource(
                root_key=root.root_key,
                relative_path=relative,
                text=text,
                digest=hashlib.sha256(raw).hexdigest(),
                byte_count=len(raw),
                identity=after_identity,
                extractor_version=extractor_version,
                blocks=blocks,
                warnings=warnings,
            )
        except SourceAccessError:
            raise
        except OSError:
            raise SourceAccessError("source_unavailable") from None
        finally:
            _close_quietly(descriptor)

    def check_access(self, source: str) -> str:
        """Reopen a source and recheck its current grant and OS access."""

        root, candidate, _relative = self._locate(source)
        descriptor = self._open_validated(root, candidate)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise SourceAccessError("source_unavailable")
            _require_single_link(info)
            self._check_cloud_state(descriptor)
            return root.root_key
        except SourceAccessError:
            raise
        except OSError:
            raise SourceAccessError("source_unavailable") from None
        finally:
            _close_quietly(descriptor)

    def catalog(self, query: str = "", limit: int = 5) -> list[dict[str, str]]:
        """List matching supported files without reading file contents.

        At most 1,000 filesystem entries are visited across all roots. Link-like
        directories are not traversed, and symbolic-link files are not listed.
        """

        if not isinstance(query, str) or len(query) > 4096:
            raise SourceAccessError("catalog_invalid")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= _MAX_CATALOG_LIMIT
        ):
            raise SourceAccessError("catalog_invalid")
        self._catalog_truncated = False
        self._catalog_unreadable_entries = 0
        needle = query.casefold()
        visited = 0
        found: list[dict[str, str]] = []
        for root in self._roots:
            self._check_root_current(root)
            pending = [root.path]
            stop_scan = False
            while pending and not stop_scan:
                directory = pending.pop()
                try:
                    with os.scandir(directory) as entries:
                        batch: list[os.DirEntry[str]] = []
                        capacity = max(0, _MAX_CATALOG_ENTRIES - visited)
                        for entry in entries:
                            if len(batch) == capacity:
                                self._catalog_truncated = True
                                stop_scan = True
                                break
                            batch.append(entry)
                except OSError:
                    self._catalog_unreadable_entries += 1
                    self._catalog_truncated = True
                    continue
                visited += len(batch)
                ordered = sorted(batch, key=lambda entry: entry.name.casefold())
                for entry in ordered:
                    try:
                        if entry.is_symlink():
                            continue
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        self._catalog_unreadable_entries += 1
                        self._catalog_truncated = True
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        # Reparse/junction directories are not traversed. This
                        # also avoids treating a link target as an approved root.
                        if _is_reparse(info):
                            continue
                        pending.append(Path(entry.path))
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    path = Path(entry.path)
                    if path.suffix.casefold() not in _SUPPORTED_SUFFIXES:
                        continue
                    try:
                        relative = self._relative_path(root, path)
                    except SourceAccessError:
                        self._catalog_truncated = True
                        continue
                    if needle and needle not in relative.casefold():
                        continue
                    try:
                        self.check_access(str(path))
                    except SourceAccessError:
                        self._catalog_unreadable_entries += 1
                        self._catalog_truncated = True
                        continue
                    found.append(
                        {
                            "source": str(path),
                            "root_key": root.root_key,
                            "relative_path": relative,
                            "name": path.name,
                            "kind": path.suffix.lower().lstrip("."),
                        }
                    )
                    found.sort(key=lambda item: (item["root_key"], item["relative_path"]))
                    if len(found) > limit:
                        self._catalog_truncated = True
                        del found[limit:]
            if stop_scan:
                break
        ordered = sorted(found, key=lambda item: (item["root_key"], item["relative_path"]))
        return ordered[:limit]

    def _prepare_root(self, supplied: Path) -> _ApprovedRoot:
        try:
            path = Path(supplied)
            if not path.is_absolute():
                raise SourceAccessError("source_unapproved")
            _reject_unsafe_path_text(str(path), is_root=True)
            canonical = path.resolve(strict=True)
            if not canonical.is_dir():
                raise SourceAccessError("source_unapproved")
            if os.name == "nt" and _windows_drive_type(_windows_drive_root(str(canonical))) == 4:
                raise SourceAccessError("source_unapproved")
            descriptor = _open_directory(canonical)
            try:
                final = _final_path_for_fd(descriptor)
                root_info = os.fstat(descriptor)
                identity = _identity_for_fd(descriptor, root_info)
            finally:
                _close_quietly(descriptor)
            if _is_network_final_path(final):
                raise SourceAccessError("source_unapproved")
        except SourceAccessError:
            raise
        except (OSError, RuntimeError, ValueError, TypeError):
            raise SourceAccessError("source_unapproved") from None
        root_key = _comparison_path(final)
        return _ApprovedRoot(path=canonical, final_path=final, identity=identity, root_key=root_key)

    def _locate(self, source: str) -> tuple[_ApprovedRoot, Path, str]:
        if not isinstance(source, str) or not source or len(source) > 4096:
            raise SourceAccessError("source_unavailable")
        _reject_unsafe_path_text(source)
        supplied_path = Path(source)
        if not supplied_path.is_absolute():
            if len(self._roots) == 1:
                supplied_path = self._roots[0].path / supplied_path
            else:
                # Several roots: a bare file name resolves only when exactly one root holds
                # a regular, non-link file of that exact name directly inside it.
                if source != supplied_path.name or source in {".", ".."}:
                    raise SourceAccessError("source_unapproved")
                holders: list[Path] = []
                for root in self._roots:
                    try:
                        info = os.lstat(root.path / source)
                    except OSError:
                        continue
                    if stat.S_ISREG(info.st_mode) and not _is_reparse(info):
                        holders.append(root.path / source)
                if len(holders) != 1:
                    raise SourceAccessError("source_unapproved")
                supplied_path = holders[0]
        matches: list[tuple[_ApprovedRoot, Path, str]] = []
        for root in self._roots:
            try:
                relative = self._relative_path(root, supplied_path)
            except SourceAccessError:
                continue
            matches.append((root, supplied_path, relative))
        if not matches:
            raise SourceAccessError("source_unapproved")
        selected = max(matches, key=lambda item: len(item[0].path.parts))
        if selected[1].suffix.casefold() not in _SUPPORTED_SUFFIXES:
            raise SourceAccessError("source_unsupported")
        return selected

    def _relative_path(self, root: _ApprovedRoot, candidate: Path) -> str:
        try:
            if os.name == "nt":
                root_text = ntpath.normcase(ntpath.normpath(str(root.path)))
                candidate_text = ntpath.normcase(ntpath.normpath(str(candidate)))
                if ntpath.commonpath((root_text, candidate_text)) != root_text:
                    raise SourceAccessError("source_unapproved")
                relative = ntpath.relpath(candidate_text, root_text)
            else:
                relative = candidate.absolute().relative_to(root.path).as_posix()
            if relative in {".", ""} or relative.startswith(".."):
                raise SourceAccessError("source_unapproved")
            return relative.replace("\\", "/")
        except SourceAccessError:
            raise
        except (OSError, ValueError):
            raise SourceAccessError("source_unapproved") from None

    def _open_validated(self, root: _ApprovedRoot, candidate: Path) -> int:
        self._check_root_current(root)
        try:
            if os.name == "nt":
                _preflight_windows_path(candidate)
                descriptor = _windows_open_read_file(candidate)
            else:
                flags = os.O_RDONLY
                if hasattr(os, "O_BINARY"):
                    flags |= os.O_BINARY
                descriptor = os.open(candidate, flags)
        except (OSError, ValueError):
            raise SourceAccessError("source_unavailable") from None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise SourceAccessError("source_unavailable")
            _require_single_link(info)
            self._check_cloud_state(descriptor)
            final = _final_path_for_fd(descriptor)
            if _is_network_final_path(final) or not _is_within(final, root.final_path):
                raise SourceAccessError("source_unapproved")
            self._check_root_current(root)
            return descriptor
        except SourceAccessError:
            _close_quietly(descriptor)
            raise
        except (OSError, RuntimeError, ValueError):
            _close_quietly(descriptor)
            raise SourceAccessError("source_unavailable") from None

    def _check_root_current(self, root: _ApprovedRoot) -> None:
        try:
            descriptor = _open_directory(root.path)
            try:
                final = _final_path_for_fd(descriptor)
                info = os.fstat(descriptor)
                identity = _identity_for_fd(descriptor, info)
            finally:
                _close_quietly(descriptor)
        except (OSError, RuntimeError, ValueError):
            raise SourceAccessError("source_unavailable") from None
        if (
            _is_network_final_path(final)
            or _comparison_path(final) != root.root_key
            or identity != root.identity
        ):
            raise SourceAccessError("source_unapproved")

    @staticmethod
    def _check_cloud_state(descriptor: int) -> None:
        if os.name != "nt":
            return
        attributes, tag = _windows_attribute_tag(descriptor)
        offline_or_recall = 0x00001000 | 0x00040000 | 0x00400000
        if attributes & offline_or_recall:
            raise SourceAccessError("source_unavailable")
        if attributes & 0x00000400:  # FILE_ATTRIBUTE_REPARSE_POINT
            # Only already-hydrated Cloud Files reparse points are supported.
            # Other reparse tags can redirect or invoke an unknown provider.
            cloud_base = 0x9000001A
            if tag is None or (tag & ~0x0000F000) != cloud_base:
                raise SourceAccessError("source_unavailable")


def _extract_or_raise(raw: bytes, kind: str) -> ExtractedDocument:
    try:
        return extract_document(raw, kind)
    except ExtractionError as exc:
        raise SourceAccessError(exc.code) from None


def _reject_unsafe_path_text(value: str, *, is_root: bool = False) -> None:
    if "\x00" in value or any(ord(character) < 32 for character in value):
        raise SourceAccessError("source_unapproved")
    if value.startswith(("\\\\?\\", "\\\\.\\", "//")):
        raise SourceAccessError("source_unapproved")
    if os.name == "nt":
        windows = PureWindowsPath(value)
        if windows.drive.startswith("\\\\") or (windows.root and not windows.drive):
            raise SourceAccessError("source_unapproved")
        if not is_root and not windows.is_absolute() and windows.drive:
            raise SourceAccessError("source_unapproved")
        drive_colon = 1 if len(value) >= 2 and value[1] == ":" else -1
        for index, character in enumerate(value):
            if character == ":" and index != drive_colon:
                raise SourceAccessError("source_unapproved")
        segments = re.split(r"[\\/]", value)
        if any(segment in {".", ".."} for segment in segments):
            raise SourceAccessError("source_unapproved")
        for segment in segments:
            if segment.endswith((" ", ".")) or _WINDOWS_RESERVED.fullmatch(segment):
                raise SourceAccessError("source_unapproved")
    else:
        if value.startswith("//") or any(segment == ".." for segment in value.split("/")):
            raise SourceAccessError("source_unapproved")


def _open_directory(path: Path) -> int:
    if os.name == "nt":
        return _windows_open_directory(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return os.open(path, flags)


def _final_path_for_fd(descriptor: int) -> str:
    if os.name == "nt":
        return _windows_final_path(descriptor)
    proc_path = Path(f"/proc/self/fd/{descriptor}")
    if not proc_path.exists():
        raise RuntimeError("handle_path_unavailable")
    return os.path.realpath(proc_path)


def _comparison_path(value: str) -> str:
    if os.name == "nt":
        normalized = value.replace("/", "\\")
        if normalized.startswith("\\\\?\\UNC\\"):
            normalized = "\\\\" + normalized[8:]
        elif normalized.startswith("\\\\?\\"):
            normalized = normalized[4:]
        return ntpath.normcase(ntpath.normpath(normalized))
    return os.path.normpath(value)


def _is_network_final_path(value: str) -> bool:
    return os.name == "nt" and _comparison_path(value).startswith("\\\\")


def _windows_drive_root(value: str) -> str:
    drive, _tail = ntpath.splitdrive(value)
    return f"{drive}\\" if drive else value


def _is_within(candidate: str, root: str) -> bool:
    candidate_path = _comparison_path(candidate)
    root_path = _comparison_path(root)
    try:
        if os.name == "nt":
            return ntpath.commonpath((candidate_path, root_path)) == root_path
        return os.path.commonpath((candidate_path, root_path)) == root_path
    except ValueError:
        return False


def _require_single_link(info: os.stat_result) -> None:
    """Reject files whose link count cannot prove a single directory entry."""

    link_count = getattr(info, "st_nlink", None)
    if type(link_count) is not int or link_count != 1:
        raise SourceAccessError("source_unapproved")


def _file_state(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
        getattr(info, "st_nlink", -1),
    )


def _identity_for_fd(descriptor: int, info: os.stat_result) -> str:
    if os.name == "nt":
        volume, file_index = _windows_file_identity(descriptor)
        return f"{volume:08x}:{file_index:016x}"
    return f"{info.st_dev:x}:{info.st_ino:x}"


def _close_quietly(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _is_reparse(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & 0x00000400)


def _windows_drive_type(path: str) -> int:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetDriveTypeW.restype = ctypes.c_uint
    return int(kernel32.GetDriveTypeW(path))


def _windows_open_directory(path: Path) -> int:
    import ctypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    handle = kernel32.CreateFileW(str(path), 0x80, 0x1 | 0x2 | 0x4, None, 3, 0x02000000, None)
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        raise OSError(ctypes.get_last_error(), "directory_open_failed")
    try:
        duplicate = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except BaseException:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        raise
    return duplicate


def _preflight_windows_path(path: Path) -> None:
    """Inspect reparse metadata without opening a placeholder for hydration."""

    descriptor = _windows_open_reparse_point(path)
    try:
        attributes, tag = _windows_attribute_tag(descriptor)
        unavailable = 0x00001000 | 0x00040000 | 0x00400000
        if attributes & unavailable:
            raise SourceAccessError("source_unavailable")
        if attributes & 0x00000400:  # FILE_ATTRIBUTE_REPARSE_POINT
            cloud_base = 0x9000001A
            if tag in {0xA0000003, 0xA000000C}:  # mount point or symbolic link
                raise SourceAccessError("source_unapproved")
            if tag is None or (tag & ~0x0000F000) != cloud_base:
                raise SourceAccessError("source_unavailable")
    finally:
        os.close(descriptor)


def _windows_open_reparse_point(path: Path) -> int:
    import ctypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    # OPEN_REPARSE_POINT reads the tag itself and avoids recall-on-open.
    handle = kernel32.CreateFileW(str(path), 0x80, 0x1 | 0x2 | 0x4, None, 3, 0x00200000, None)
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        raise SourceAccessError("source_unavailable")
    try:
        return msvcrt.open_osfhandle(int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except BaseException:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        raise


def _windows_open_read_file(path: Path) -> int:
    """Open for reading while denying concurrent writes and deletion."""

    import ctypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    handle = kernel32.CreateFileW(str(path), 0x80000000, 0x1, None, 3, 0x80, None)
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, invalid):
        raise OSError(ctypes.get_last_error(), "source_open_failed")
    try:
        return msvcrt.open_osfhandle(int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except BaseException:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        raise


def _windows_handle(descriptor: int) -> Any:
    import ctypes
    import msvcrt

    return ctypes.c_void_p(msvcrt.get_osfhandle(descriptor))


def _windows_final_path(descriptor: int) -> str:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFinalPathNameByHandleW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    kernel32.GetFinalPathNameByHandleW.restype = ctypes.c_uint32
    size = 32768
    buffer = ctypes.create_unicode_buffer(size)
    length = kernel32.GetFinalPathNameByHandleW(_windows_handle(descriptor), buffer, size, 0)
    if not length or length >= size:
        raise OSError(ctypes.get_last_error(), "final_path_unavailable")
    return buffer.value


def _windows_file_identity(descriptor: int) -> tuple[int, int]:
    import ctypes

    class _FileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", ctypes.c_uint32),
            ("creation_low", ctypes.c_uint32),
            ("creation_high", ctypes.c_uint32),
            ("access_low", ctypes.c_uint32),
            ("access_high", ctypes.c_uint32),
            ("write_low", ctypes.c_uint32),
            ("write_high", ctypes.c_uint32),
            ("volume_serial", ctypes.c_uint32),
            ("size_high", ctypes.c_uint32),
            ("size_low", ctypes.c_uint32),
            ("links", ctypes.c_uint32),
            ("index_high", ctypes.c_uint32),
            ("index_low", ctypes.c_uint32),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFileInformationByHandle.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_FileInformation),
    ]
    kernel32.GetFileInformationByHandle.restype = ctypes.c_int
    result = _FileInformation()
    if not kernel32.GetFileInformationByHandle(_windows_handle(descriptor), ctypes.byref(result)):
        raise OSError(ctypes.get_last_error(), "file_identity_unavailable")
    file_index = (result.index_high << 32) | result.index_low
    return result.volume_serial, file_index


def _windows_attribute_tag(descriptor: int) -> tuple[int, int | None]:
    import ctypes

    class _AttributeTagInfo(ctypes.Structure):
        _fields_ = [("attributes", ctypes.c_uint32), ("reparse_tag", ctypes.c_uint32)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = getattr(kernel32, "GetFileInformationByHandleEx", None)
    if get_info is None:
        raise SourceAccessError("source_unavailable")
    get_info.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    get_info.restype = ctypes.c_int
    result = _AttributeTagInfo()
    if not get_info(_windows_handle(descriptor), 9, ctypes.byref(result), ctypes.sizeof(result)):
        raise SourceAccessError("source_unavailable")
    return result.attributes, result.reparse_tag
