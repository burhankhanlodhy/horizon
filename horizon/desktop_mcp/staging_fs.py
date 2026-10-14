"""Anchored filesystem operations for one companion-owned staging directory.

The helper never recursively removes paths. POSIX operations stay relative to
opened directory descriptors. Windows keeps the complete ancestor chain open
without write/delete sharing, then performs staged-file deletion through the
owned file handle itself.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import threading
from pathlib import Path

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_MAX_FILE_BYTES = 8 * 1024 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


class StagingFilesystemError(OSError):
    """Sanitized failure while operating in the pinned staging directory."""


def _safe_name(name: str) -> str:
    if not isinstance(name, str) or not _SAFE_NAME.fullmatch(name) or name in {".", ".."}:
        raise StagingFilesystemError("invalid_staging_name")
    return name


def _identity(info: os.stat_result) -> tuple[int, int]:
    return (info.st_dev, info.st_ino)


class PinnedStagingDirectory:
    """Pin one approved root and its generated child for safe local file ops."""

    def __init__(self, approved_root: Path, session_name: str) -> None:
        self.root = Path(approved_root)
        self.session_name = _safe_name(session_name)
        self.directory = self.root / self.session_name
        self._lock = threading.RLock()
        self._closed = False
        self._entries: dict[str, tuple[int, int]] = {}
        self._posix_root_fd: int | None = None
        self._posix_directory_fd: int | None = None
        self._windows_handles: list[int] = []
        self._windows_child_handle: int | None = None
        self._windows_root_handle: int | None = None
        self._windows_files: dict[str, int] = {}
        self._writer_descriptors: dict[str, int] = {}
        self._lease_name: str | None = None
        self._lease_descriptor: int | None = None
        self._lease_identity: tuple[int, int] | None = None
        self._root_identity: tuple[int, int] | None = None
        self._directory_identity: tuple[int, int] | None = None

    @classmethod
    def open(cls, approved_root: Path, session_name: str) -> PinnedStagingDirectory:
        instance = cls(approved_root, session_name)
        try:
            if os.name == "nt":
                instance._open_windows()
            else:
                instance._open_posix()
            return instance
        except Exception:
            if os.name == "nt":
                instance._remove_windows_directory_if_owned()
                instance._close_handles()
            else:
                instance._close_posix(remove_directory=True)
            raise

    @classmethod
    def open_existing(cls, approved_root: Path, session_name: str) -> PinnedStagingDirectory:
        """Pin an already existing generated child without creating it."""
        instance = cls(approved_root, session_name)
        try:
            if os.name == "nt":
                instance._open_windows(existing=True)
            else:
                instance._open_posix(existing=True)
            return instance
        except Exception:
            if os.name == "nt":
                instance._close_handles()
            else:
                instance._close_posix(remove_directory=False)
            raise

    @property
    def root_identity(self) -> tuple[int, int] | None:
        return self._root_identity

    @property
    def directory_identity(self) -> tuple[int, int] | None:
        return self._directory_identity

    def _open_posix(self, *, existing: bool = False) -> None:
        if not self.root.is_absolute():
            raise StagingFilesystemError("staging_root_not_absolute")
        flags = _DIRECTORY_FLAGS
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            raise StagingFilesystemError("safe_directory_open_unavailable")
        try:
            root_fd = os.open(self.root, flags)
            self._posix_root_fd = root_fd
            root_info = os.fstat(root_fd)
            if not stat.S_ISDIR(root_info.st_mode) or root_info.st_nlink < 1:
                os.close(root_fd)
                self._posix_root_fd = None
                raise StagingFilesystemError("staging_root_unavailable")
            self._root_identity = _identity(root_info)
            if not existing:
                os.mkdir(self.session_name, mode=0o700, dir_fd=root_fd)
            child_fd = os.open(self.session_name, flags, dir_fd=root_fd)
            self._posix_directory_fd = child_fd
            child_info = os.fstat(child_fd)
            if not stat.S_ISDIR(child_info.st_mode):
                raise StagingFilesystemError("staging_directory_unavailable")
            self._directory_identity = _identity(child_info)
        except StagingFilesystemError:
            raise
        except OSError as error:
            raise StagingFilesystemError("staging_directory_unavailable") from error

    def _open_windows(self, *, existing: bool = False) -> None:
        import ctypes
        import ctypes.wintypes

        if not self.root.is_absolute() or not self.root.drive:
            raise StagingFilesystemError("staging_root_not_absolute")

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.HANDLE,
        ]
        create_file.restype = ctypes.wintypes.HANDLE
        get_info = kernel32.GetFileInformationByHandle
        get_info.argtypes = [ctypes.wintypes.HANDLE, ctypes.c_void_p]
        get_info.restype = ctypes.wintypes.BOOL

        class _ByHandleInfo(ctypes.Structure):
            _fields_ = [
                ("attributes", ctypes.wintypes.DWORD),
                ("creation_low", ctypes.wintypes.DWORD),
                ("creation_high", ctypes.wintypes.DWORD),
                ("access_low", ctypes.wintypes.DWORD),
                ("access_high", ctypes.wintypes.DWORD),
                ("write_low", ctypes.wintypes.DWORD),
                ("write_high", ctypes.wintypes.DWORD),
                ("volume_serial", ctypes.wintypes.DWORD),
                ("size_high", ctypes.wintypes.DWORD),
                ("size_low", ctypes.wintypes.DWORD),
                ("links", ctypes.wintypes.DWORD),
                ("index_high", ctypes.wintypes.DWORD),
                ("index_low", ctypes.wintypes.DWORD),
            ]

        self._win_info_type = _ByHandleInfo
        self._win_create_file = create_file
        self._win_get_info = get_info
        self._win_kernel32 = kernel32
        share_read_write = 0x00000001 | 0x00000002
        open_existing = 3
        backup_semantics = 0x02000000
        open_reparse_point = 0x00200000
        read_attributes = 0x00000080
        list_directory = 0x00000001
        file_attribute_directory = 0x00000010
        reparse_attribute = 0x00000400

        try:
            # Hold every path component so an ancestor cannot be swapped after
            # validation and redirect a later absolute-path operation.
            current = Path(self.root.anchor)
            components = self.root.parts[1:]
            for component in components:
                current = current / component
                handle = create_file(
                    str(current),
                    read_attributes | list_directory | (0x00000004 if current == self.root else 0),
                    share_read_write,
                    None,
                    open_existing,
                    backup_semantics | open_reparse_point,
                    None,
                )
                if handle == ctypes.wintypes.HANDLE(-1).value:
                    raise self._win_os_error("staging_directory_unavailable")
                self._windows_handles.append(int(handle))
                info = _ByHandleInfo()
                if not get_info(handle, ctypes.byref(info)):
                    raise self._win_os_error("staging_directory_unavailable")
                if not (info.attributes & file_attribute_directory) or (
                    info.attributes & reparse_attribute
                ):
                    raise StagingFilesystemError("staging_reparse_point_rejected")
            if not self._windows_handles:
                raise StagingFilesystemError("staging_root_unavailable")
            self._windows_root_handle = self._windows_handles[-1]
            handle = self._nt_create_directory_relative(
                self._windows_root_handle,
                self.session_name,
                read_attributes
                | list_directory
                | 0x00000002
                | 0x00010000
                | 0x40000000
                | 0x00100000,
                share_read_write,
                0x00000010,
                existing=existing,
            )
            self._windows_handles.append(handle)
            self._windows_child_handle = handle
            info = _ByHandleInfo()
            if not get_info(handle, ctypes.byref(info)):
                raise self._win_os_error("staging_directory_unavailable")
            if not (info.attributes & file_attribute_directory) or (
                info.attributes & reparse_attribute
            ):
                raise StagingFilesystemError("staging_reparse_point_rejected")
            self._root_identity = self._windows_handle_identity(self._windows_root_handle)
            self._directory_identity = self._windows_handle_identity(handle)
        except StagingFilesystemError:
            self._remove_windows_directory_if_owned()
            raise
        except OSError:
            self._remove_windows_directory_if_owned()
            raise

    def _nt_create_directory_relative(
        self,
        parent_handle: int,
        name: str,
        desired_access: int,
        share_access: int,
        attributes: int,
        *,
        existing: bool = False,
        directory: bool = True,
    ) -> int:
        """Atomically create/open a child relative to an already pinned directory."""
        import ctypes
        import ctypes.wintypes

        class _UnicodeString(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.wintypes.USHORT),
                ("maximum_length", ctypes.wintypes.USHORT),
                ("buffer", ctypes.wintypes.LPWSTR),
            ]

        class _ObjectAttributes(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.wintypes.ULONG),
                ("root_directory", ctypes.wintypes.HANDLE),
                ("object_name", ctypes.POINTER(_UnicodeString)),
                ("attributes", ctypes.wintypes.ULONG),
                ("security_descriptor", ctypes.c_void_p),
                ("security_quality_of_service", ctypes.c_void_p),
            ]

        class _StatusUnion(ctypes.Union):
            _fields_ = [
                ("status", ctypes.c_long),
                ("pointer", ctypes.c_void_p),
            ]

        class _IoStatusBlock(ctypes.Structure):
            _anonymous_ = ("value",)
            _fields_ = [
                ("value", _StatusUnion),
                ("information", ctypes.c_size_t),
            ]

        native = ctypes.WinDLL("ntdll")
        nt_create_file = native.NtCreateFile
        nt_create_file.argtypes = [
            ctypes.POINTER(ctypes.wintypes.HANDLE),
            ctypes.wintypes.ULONG,
            ctypes.POINTER(_ObjectAttributes),
            ctypes.POINTER(_IoStatusBlock),
            ctypes.c_void_p,
            ctypes.wintypes.ULONG,
            ctypes.wintypes.ULONG,
            ctypes.wintypes.ULONG,
            ctypes.wintypes.ULONG,
            ctypes.c_void_p,
            ctypes.wintypes.ULONG,
        ]
        nt_create_file.restype = ctypes.c_long
        status_to_error = native.RtlNtStatusToDosError
        status_to_error.argtypes = [ctypes.c_long]
        status_to_error.restype = ctypes.wintypes.ULONG
        name_buffer = ctypes.create_unicode_buffer(name)
        unicode_name = _UnicodeString(
            len(name.encode("utf-16-le")),
            len(name.encode("utf-16-le")) + 2,
            ctypes.cast(name_buffer, ctypes.wintypes.LPWSTR),
        )
        object_attributes = _ObjectAttributes(
            ctypes.sizeof(_ObjectAttributes),
            parent_handle,
            ctypes.pointer(unicode_name),
            0x00000040,  # OBJ_CASE_INSENSITIVE
            None,
            None,
        )
        io_status = _IoStatusBlock()
        output = ctypes.wintypes.HANDLE()
        # FILE_CREATE/FILE_OPEN + FILE_DIRECTORY_FILE/FILE_NON_DIRECTORY_FILE
        # + FILE_SYNCHRONOUS_IO_NONALERT
        # + FILE_OPEN_REPARSE_POINT. The returned handle pins the new directory
        # before any path-based operation can observe or replace its name.
        status = nt_create_file(
            ctypes.byref(output),
            desired_access | (0x00100000 if not directory else 0),
            ctypes.byref(object_attributes),
            ctypes.byref(io_status),
            None,
            attributes,
            share_access,
            1 if existing else 2,
            (0x00000001 if directory else 0x00000040) | 0x00000020 | 0x00200000,
            None,
            0,
        )
        if status < 0:
            error = status_to_error(status)
            failure = StagingFilesystemError("staging_directory_unavailable", int(error))
            failure.winerror = int(error)
            raise failure
        return int(output.value)

    def _windows_handle_identity(self, handle: int) -> tuple[int, int]:
        import ctypes

        info = self._win_info_type()
        if not self._win_get_info(handle, ctypes.byref(info)):
            raise self._win_os_error("staging_directory_unavailable")
        file_index = (int(info.index_high) << 32) | int(info.index_low)
        return int(info.volume_serial), file_index

    def _win_os_error(self, message: str) -> StagingFilesystemError:
        import ctypes

        return StagingFilesystemError(message, ctypes.get_last_error())

    @property
    def _is_windows(self) -> bool:
        return os.name == "nt"

    def _check_open(self) -> None:
        if self._closed:
            raise StagingFilesystemError("staging_closed")

    def check_directory(self) -> None:
        """Fail if the configured root or its session child was redirected."""

        with self._lock:
            self._check_open()
            if self._is_windows:
                if len(self._windows_handles) < 2:
                    raise StagingFilesystemError("staging_directory_unavailable")
                return
            if self._posix_root_fd is None or self._posix_directory_fd is None:
                raise StagingFilesystemError("staging_closed")
            try:
                root_path_info = self.root.lstat()
                child_info = os.stat(
                    self.session_name,
                    dir_fd=self._posix_root_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(root_path_info.st_mode)
                    or _identity(root_path_info) != self._root_identity
                    or not stat.S_ISDIR(child_info.st_mode)
                    or _identity(child_info) != self._directory_identity
                    or _identity(os.fstat(self._posix_root_fd)) != self._root_identity
                    or _identity(os.fstat(self._posix_directory_fd)) != self._directory_identity
                ):
                    raise StagingFilesystemError("staging_directory_unavailable")
            except StagingFilesystemError:
                raise
            except OSError as error:
                raise StagingFilesystemError("staging_directory_unavailable") from error

    def write_exclusive(self, filename: str, data: bytes) -> tuple[int, int]:
        filename = _safe_name(filename)
        if not isinstance(data, bytes) or len(data) > _MAX_FILE_BYTES:
            raise StagingFilesystemError("staging_file_size_invalid")
        with self._lock:
            self._check_open()
            if filename in self._entries:
                raise StagingFilesystemError("staging_file_exists")
            identity = self.create_empty_exclusive(filename)
            try:
                written = self.write_owned(filename, identity, data)
            except StagingFilesystemError:
                self.delete_owned(filename, identity)
                raise
            if not written:
                self.delete_owned(filename, identity)
                raise StagingFilesystemError("staging_file_changed")
            return identity

    def list_names(self, *, limit: int = 256) -> list[str]:
        """List a bounded number of children while the directory stays pinned."""
        if not 1 <= limit <= 4096:
            raise StagingFilesystemError("staging_directory_limit_invalid")
        with self._lock:
            self.check_directory()
            if self._is_windows:
                children = os.scandir(self.directory)
            else:
                if self._posix_directory_fd is None:
                    raise StagingFilesystemError("staging_closed")
                children = os.scandir(self._posix_directory_fd)
            try:
                names: list[str] = []
                for child in children:
                    if len(names) >= limit:
                        raise StagingFilesystemError("staging_directory_too_many_children")
                    names.append(child.name)
                return names
            finally:
                children.close()

    def create_empty_exclusive(self, filename: str) -> tuple[int, int]:
        """Create an empty, owned file and retain its writer handle.

        Recovery metadata can durably claim the returned identity before any
        user text is written through :meth:`write_owned`.
        """
        filename = _safe_name(filename)
        with self._lock:
            self._check_open()
            if filename in self._entries or filename in self._writer_descriptors:
                raise StagingFilesystemError("staging_file_exists")
            if self._is_windows:
                import msvcrt

                handle = self._nt_create_directory_relative(
                    self._windows_child_handle,
                    filename,
                    0x80000000 | 0x40000000 | 0x00010000 | 0x00000080,
                    0x00000001,
                    0x00000080,
                    directory=False,
                )
                descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
                identity, attributes, links = self._windows_file_info(
                    msvcrt.get_osfhandle(descriptor)
                )
                if attributes & (0x00000010 | 0x00000400) or links != 1:
                    os.close(descriptor)
                    raise StagingFilesystemError("staging_file_rejected")
            else:
                if self._posix_directory_fd is None:
                    raise StagingFilesystemError("staging_closed")
                flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                try:
                    descriptor = os.open(filename, flags, 0o600, dir_fd=self._posix_directory_fd)
                    info = os.fstat(descriptor)
                except OSError as error:
                    raise StagingFilesystemError("staging_write_failed") from error
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != 0:
                    os.close(descriptor)
                    raise StagingFilesystemError("staging_file_rejected")
                identity = _identity(info)
            self._entries[filename] = identity
            self._writer_descriptors[filename] = descriptor
            try:
                self.sync_directory()
            except StagingFilesystemError:
                self.delete_owned(filename, identity)
                raise
            return identity

    def sync_directory(self) -> None:
        """Flush directory-entry changes before any user text write follows."""
        with self._lock:
            self.check_directory()
            if self._is_windows:
                import ctypes.wintypes

                flush = self._win_kernel32.FlushFileBuffers
                flush.argtypes = [ctypes.wintypes.HANDLE]
                flush.restype = ctypes.wintypes.BOOL
                if not flush(self._windows_child_handle):
                    raise self._win_os_error("staging_directory_sync_failed")
            else:
                if self._posix_directory_fd is None:
                    raise StagingFilesystemError("staging_closed")
                try:
                    os.fsync(self._posix_directory_fd)
                except OSError as error:
                    raise StagingFilesystemError("staging_directory_sync_failed") from error

    def write_owned(self, filename: str, identity: tuple[int, int], data: bytes) -> bool:
        """Write bounded bytes through the retained identity-checked handle."""
        filename = _safe_name(filename)
        if not isinstance(data, bytes) or len(data) > _MAX_FILE_BYTES:
            raise StagingFilesystemError("staging_file_size_invalid")
        with self._lock:
            self._check_open()
            descriptor = self._writer_descriptors.get(filename)
            if descriptor is None or self._entries.get(filename) != identity:
                return False
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != 0:
                    return False
                if self._is_windows:
                    import msvcrt

                    observed, attributes, links = self._windows_file_info(
                        msvcrt.get_osfhandle(descriptor)
                    )
                    if observed != identity or attributes & (0x00000010 | 0x00000400) or links != 1:
                        return False
                total = 0
                while total < len(data):
                    written = os.write(descriptor, data[total:])
                    if written <= 0:
                        raise StagingFilesystemError("staging_write_failed")
                    total += written
                os.fsync(descriptor)
                return True
            finally:
                os.close(descriptor)
                self._writer_descriptors.pop(filename, None)

    def read_existing(
        self, filename: str, *, limit: int
    ) -> tuple[bytes, tuple[int, int], int, int]:
        """Read one bounded, regular, single-link file relative to the pinned child."""
        filename = _safe_name(filename)
        if not 0 <= limit <= _MAX_FILE_BYTES:
            raise StagingFilesystemError("staging_file_size_invalid")
        with self._lock:
            self._check_open()
            descriptor: int | None = None
            try:
                if self._is_windows:
                    import msvcrt

                    handle = self._nt_create_directory_relative(
                        self._windows_child_handle,
                        filename,
                        0x80000000 | 0x00000080,
                        0x00000001,
                        0x00000080,
                        existing=True,
                        directory=False,
                    )
                    identity, attributes, links = self._windows_file_info(handle)
                    if attributes & (0x00000010 | 0x00000400) or links != 1:
                        self._close_handle(handle)
                        raise StagingFilesystemError("staging_file_rejected")
                    descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
                else:
                    if self._posix_directory_fd is None:
                        raise StagingFilesystemError("staging_closed")
                    descriptor = os.open(
                        filename,
                        os.O_RDONLY | os.O_NOFOLLOW,
                        dir_fd=self._posix_directory_fd,
                    )
                    info = os.fstat(descriptor)
                    identity = _identity(info)
                    links = info.st_nlink
                    if not stat.S_ISREG(info.st_mode) or links != 1:
                        raise StagingFilesystemError("staging_file_rejected")
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                    raise StagingFilesystemError("staging_file_size_invalid")
                data = bytearray()
                while len(data) <= limit:
                    block = os.read(descriptor, min(65536, limit + 1 - len(data)))
                    if not block:
                        break
                    data.extend(block)
                if len(data) > limit:
                    raise StagingFilesystemError("staging_file_size_invalid")
                return bytes(data), identity, links, info.st_size
            except StagingFilesystemError:
                raise
            except OSError as error:
                raise StagingFilesystemError("staging_file_unavailable") from error
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    def adopt_existing(self, filename: str, identity: tuple[int, int]) -> bool:
        """Register a previously checked owned file for anchored deletion."""
        filename = _safe_name(filename)
        with self._lock:
            self._check_open()
            try:
                if self._is_windows:
                    handle = self._nt_create_directory_relative(
                        self._windows_child_handle,
                        filename,
                        0x00010000 | 0x00000080,
                        0x00000001,
                        0x00000080,
                        existing=True,
                        directory=False,
                    )
                    observed, attributes, links = self._windows_file_info(handle)
                    self._close_handle(handle)
                    if observed != identity or attributes & (0x00000010 | 0x00000400) or links != 1:
                        return False
                else:
                    if self._posix_directory_fd is None:
                        return False
                    info = os.stat(filename, dir_fd=self._posix_directory_fd, follow_symlinks=False)
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_nlink != 1
                        or _identity(info) != identity
                    ):
                        return False
            except (OSError, StagingFilesystemError):
                return False
            self._entries[filename] = identity
            return True

    def try_acquire_lease(self, filename: str, *, create: bool) -> bool:
        """Hold a kernel-released live-session lease; return false if active."""
        filename = _safe_name(filename)
        with self._lock:
            self._check_open()
            if self._lease_descriptor is not None or self._lease_name is not None:
                raise StagingFilesystemError("staging_lease_already_held")
            try:
                if self._is_windows:
                    handle = self._nt_create_directory_relative(
                        self._windows_child_handle,
                        filename,
                        0x80000000 | 0x40000000 | 0x00010000 | 0x00000080,
                        0x00000001 if create else 0,
                        0x00000080,
                        existing=not create,
                        directory=False,
                    )
                    identity, attributes, links = self._windows_file_info(handle)
                    if attributes & (0x00000010 | 0x00000400) or links != 1:
                        self._close_handle(handle)
                        raise StagingFilesystemError("staging_lease_invalid")
                    if self._windows_file_size(handle) != 0:
                        self._close_handle(handle)
                        raise StagingFilesystemError("staging_lease_invalid")
                    descriptor = -int(handle)
                else:
                    if self._posix_directory_fd is None:
                        raise StagingFilesystemError("staging_closed")
                    flags = os.O_RDWR | os.O_NOFOLLOW
                    if create:
                        flags |= os.O_CREAT | os.O_EXCL
                    descriptor = os.open(
                        filename,
                        flags,
                        0o600,
                        dir_fd=self._posix_directory_fd,
                    )
                    info = os.fstat(descriptor)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != 0:
                        os.close(descriptor)
                        raise StagingFilesystemError("staging_lease_invalid")
                    identity = _identity(info)
                    import fcntl

                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        os.close(descriptor)
                        return False
                self._entries[filename] = identity
                self._lease_name = filename
                self._lease_descriptor = descriptor
                self._lease_identity = identity
                return True
            except StagingFilesystemError as error:
                if not create and getattr(error, "winerror", None) in {32, 33}:
                    return False
                raise
            except FileExistsError:
                if create:
                    return False
                raise StagingFilesystemError("staging_lease_unavailable") from None
            except PermissionError as error:
                if not create and os.name == "nt" and getattr(error, "winerror", None) in {32, 33}:
                    return False
                raise StagingFilesystemError("staging_lease_unavailable") from error
            except OSError as error:
                if not create and isinstance(error, FileNotFoundError):
                    return False
                if not create and getattr(error, "winerror", None) in {32, 33}:
                    return False
                raise StagingFilesystemError("staging_lease_unavailable") from error

    def release_lease(self) -> None:
        with self._lock:
            descriptor = self._lease_descriptor
            if descriptor is None:
                return
            if descriptor < 0:
                self._close_handle(-descriptor)
            else:
                os.close(descriptor)
            self._lease_descriptor = None

    @property
    def lease_identity(self) -> tuple[int, int] | None:
        return self._lease_identity

    def rename_owned_new(self, source: str, destination: str, identity: tuple[int, int]) -> bool:
        """Atomically rename one owned file to a new name within the pinned child."""
        source = _safe_name(source)
        destination = _safe_name(destination)
        with self._lock:
            self._check_open()
            if self._entries.get(source) != identity or destination in self._entries:
                return False
            if self._is_windows:
                import ctypes
                import ctypes.wintypes

                handle = self._nt_create_directory_relative(
                    self._windows_child_handle,
                    source,
                    0x00010000 | 0x00000080,
                    0x00000001,
                    0x00000080,
                    existing=True,
                    directory=False,
                )
                try:
                    observed, attributes, links = self._windows_file_info(handle)
                    if observed != identity or attributes & (0x00000010 | 0x00000400) or links != 1:
                        return False

                    destination_path = str(self.directory / destination)

                    class _RenameInfo(ctypes.Structure):
                        _fields_ = [
                            ("flags", ctypes.wintypes.DWORD),
                            ("root_directory", ctypes.wintypes.HANDLE),
                            ("name_bytes", ctypes.wintypes.DWORD),
                            ("name", ctypes.c_wchar * (len(destination_path) + 1)),
                        ]

                    info = _RenameInfo(
                        0,
                        None,
                        len(destination_path.encode("utf-16-le")),
                        destination_path,
                    )
                    set_info = self._win_kernel32.SetFileInformationByHandle
                    set_info.argtypes = [
                        ctypes.wintypes.HANDLE,
                        ctypes.wintypes.DWORD,
                        ctypes.c_void_p,
                        ctypes.wintypes.DWORD,
                    ]
                    set_info.restype = ctypes.wintypes.BOOL
                    if not set_info(handle, 3, ctypes.byref(info), ctypes.sizeof(info)):
                        error = ctypes.get_last_error()
                        if error in {80, 183}:
                            return False
                        raise self._win_os_error("staging_rename_failed")
                finally:
                    self._close_handle(handle)
            else:
                if self._posix_directory_fd is None:
                    raise StagingFilesystemError("staging_closed")
                import ctypes
                import errno
                import sys

                try:
                    source_info = os.stat(
                        source, dir_fd=self._posix_directory_fd, follow_symlinks=False
                    )
                    if (
                        not stat.S_ISREG(source_info.st_mode)
                        or source_info.st_nlink != 1
                        or _identity(source_info) != identity
                    ):
                        return False
                    libc = ctypes.CDLL(None, use_errno=True)
                    if sys.platform.startswith("linux"):
                        rename = getattr(libc, "renameat2", None)
                        if rename is None:
                            raise StagingFilesystemError("safe_rename_unavailable")
                        rename.argtypes = [
                            ctypes.c_int,
                            ctypes.c_char_p,
                            ctypes.c_int,
                            ctypes.c_char_p,
                            ctypes.c_uint,
                        ]
                        rename.restype = ctypes.c_int
                        result = rename(
                            self._posix_directory_fd,
                            os.fsencode(source),
                            self._posix_directory_fd,
                            os.fsencode(destination),
                            1,
                        )
                    elif sys.platform == "darwin":
                        rename = getattr(libc, "renameatx_np", None)
                        if rename is None:
                            raise StagingFilesystemError("safe_rename_unavailable")
                        rename.argtypes = [
                            ctypes.c_int,
                            ctypes.c_char_p,
                            ctypes.c_int,
                            ctypes.c_char_p,
                            ctypes.c_uint,
                        ]
                        rename.restype = ctypes.c_int
                        result = rename(
                            self._posix_directory_fd,
                            os.fsencode(source),
                            self._posix_directory_fd,
                            os.fsencode(destination),
                            0x00000004,
                        )
                    else:
                        raise StagingFilesystemError("safe_rename_unavailable")
                    if result != 0:
                        error_number = ctypes.get_errno()
                        if error_number == errno.EEXIST:
                            return False
                        raise OSError(error_number, "staging_rename_failed")
                except StagingFilesystemError:
                    raise
                except FileExistsError:
                    return False
                except OSError as error:
                    raise StagingFilesystemError("staging_rename_failed") from error
            self.sync_directory()
            self._entries.pop(source, None)
            self._entries[destination] = identity
            self._writer_descriptors.pop(source, None)
            return True

    def _write_posix(self, filename: str, data: bytes) -> tuple[int, int]:
        if self._posix_directory_fd is None:
            raise StagingFilesystemError("staging_closed")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        descriptor: int | None = None
        created_identity: tuple[int, int] | None = None
        try:
            descriptor = os.open(
                filename,
                flags,
                0o600,
                dir_fd=self._posix_directory_fd,
            )
            info = os.fstat(descriptor)
            created_identity = _identity(info)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise StagingFilesystemError("staging_file_rejected")
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            return created_identity
        except StagingFilesystemError:
            if descriptor is not None:
                os.close(descriptor)
            if created_identity is not None:
                self._unlink_posix(filename, created_identity)
            raise
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            if created_identity is not None:
                self._unlink_posix(filename, created_identity)
            raise StagingFilesystemError("staging_write_failed") from error

    def _write_windows(self, filename: str, data: bytes) -> tuple[int, int]:
        import ctypes
        import ctypes.wintypes
        import msvcrt

        create_new = 1
        share_read = 0x00000001
        generic_write = 0x40000000
        delete_access = 0x00010000
        read_attributes = 0x00000080
        open_reparse_point = 0x00200000
        writer_handle = self._win_create_file(
            str(self.directory / filename),
            generic_write | delete_access | read_attributes,
            share_read,
            None,
            create_new,
            open_reparse_point,
            None,
        )
        if writer_handle == ctypes.wintypes.HANDLE(-1).value:
            raise self._win_os_error("staging_write_failed")
        writer_handle = int(writer_handle)
        descriptor: int | None = None
        file_object = None
        identity: tuple[int, int] | None = None
        try:
            descriptor = msvcrt.open_osfhandle(writer_handle, os.O_WRONLY | os.O_BINARY)
            writer_handle = -1  # The descriptor now owns the native handle.
            file_object = os.fdopen(descriptor, "wb")
            descriptor = None
            info = os.fstat(file_object.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise StagingFilesystemError("staging_file_rejected")
            identity, attributes, links = self._windows_file_info(
                msvcrt.get_osfhandle(file_object.fileno())
            )
            if attributes & (0x00000010 | 0x00000400) or links != 1:
                raise StagingFilesystemError("staging_file_rejected")
            file_object.write(data)
            file_object.flush()
            os.fsync(file_object.fileno())
            file_object.close()
            file_object = None
            return identity
        except StagingFilesystemError:
            if file_object is not None:
                try:
                    self._mark_windows_delete(msvcrt.get_osfhandle(file_object.fileno()))
                except OSError:
                    pass
                file_object.close()
            elif descriptor is not None:
                try:
                    self._mark_windows_delete(msvcrt.get_osfhandle(descriptor))
                except OSError:
                    pass
                os.close(descriptor)
            if writer_handle != -1:
                self._mark_windows_delete(writer_handle)
                self._close_handle(writer_handle)
            elif identity is not None:
                self._delete_windows_unpinned(filename, identity)
            raise
        except OSError as error:
            if file_object is not None:
                try:
                    self._mark_windows_delete(msvcrt.get_osfhandle(file_object.fileno()))
                except OSError:
                    pass
                file_object.close()
            elif descriptor is not None:
                try:
                    self._mark_windows_delete(msvcrt.get_osfhandle(descriptor))
                except OSError:
                    pass
                os.close(descriptor)
            if writer_handle != -1:
                self._mark_windows_delete(writer_handle)
                self._close_handle(writer_handle)
            elif identity is not None:
                self._delete_windows_unpinned(filename, identity)
            raise StagingFilesystemError("staging_write_failed") from error

    def delete_owned(self, filename: str, identity: tuple[int, int]) -> bool:
        filename = _safe_name(filename)
        with self._lock:
            self._check_open()
            if self._entries.get(filename) != identity:
                return False
            writer = self._writer_descriptors.pop(filename, None)
            if writer is not None:
                os.close(writer)
            if self._is_windows:
                result = self._delete_windows(filename, identity)
            else:
                result = self._unlink_posix(filename, identity)
            if result:
                self._entries.pop(filename, None)
            return result

    def pin_owned_file(
        self, filename: str, identity: tuple[int, int], expected_sha256: str
    ) -> tuple[int, int]:
        """Verify exact staged content after broker capture and pin cleanup identity."""

        filename = _safe_name(filename)
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise StagingFilesystemError("invalid_staging_digest")
        with self._lock:
            self._check_open()
            if self._entries.get(filename) != identity:
                raise StagingFilesystemError("staging_file_changed")
            if self._is_windows:
                self._pin_windows(filename, identity, expected_sha256)
            else:
                self._pin_posix(filename, identity, expected_sha256)
            return identity

    def _pin_posix(self, filename: str, identity: tuple[int, int], expected_sha256: str) -> None:
        if self._posix_directory_fd is None:
            raise StagingFilesystemError("staging_closed")
        descriptor: int | None = None
        try:
            descriptor = os.open(
                filename,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=self._posix_directory_fd,
            )
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or _identity(info) != identity:
                raise StagingFilesystemError("staging_file_changed")
            digest = hashlib.sha256()
            total = 0
            while True:
                block = os.read(descriptor, min(65536, _MAX_FILE_BYTES + 1 - total))
                if not block:
                    break
                total += len(block)
                if total > _MAX_FILE_BYTES:
                    raise StagingFilesystemError("staging_file_size_invalid")
                digest.update(block)
            if digest.hexdigest() != expected_sha256:
                raise StagingFilesystemError("staging_file_changed")
        except StagingFilesystemError:
            raise
        except OSError as error:
            raise StagingFilesystemError("staging_file_unavailable") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _pin_windows(self, filename: str, identity: tuple[int, int], expected_sha256: str) -> None:
        import ctypes
        import ctypes.wintypes
        import msvcrt

        pinned_handle = self._win_create_file(
            str(self.directory / filename),
            0x00000080,
            0x00000001,
            None,
            3,
            0x00200000,
            None,
        )
        if pinned_handle == ctypes.wintypes.HANDLE(-1).value:
            raise self._win_os_error("staging_file_unavailable")
        pinned_handle = int(pinned_handle)
        read_handle: int | None = None
        descriptor: int | None = None
        file_object = None
        try:
            observed_identity, attributes, links = self._windows_file_info(pinned_handle)
            if (
                observed_identity != identity
                or attributes & (0x00000010 | 0x00000400)
                or links != 1
            ):
                raise StagingFilesystemError("staging_file_changed")
            read_handle_value = self._win_create_file(
                str(self.directory / filename),
                0x80000000,
                0x00000001,
                None,
                3,
                0x00200000,
                None,
            )
            if read_handle_value == ctypes.wintypes.HANDLE(-1).value:
                raise self._win_os_error("staging_file_unavailable")
            read_handle = int(read_handle_value)
            read_identity, read_attributes, read_links = self._windows_file_info(read_handle)
            if (
                read_identity != identity
                or read_attributes & (0x00000010 | 0x00000400)
                or read_links != 1
            ):
                raise StagingFilesystemError("staging_file_changed")
            descriptor = msvcrt.open_osfhandle(read_handle, os.O_RDONLY | os.O_BINARY)
            read_handle = None
            file_object = os.fdopen(descriptor, "rb")
            descriptor = None
            digest = hashlib.sha256()
            total = 0
            while True:
                block = file_object.read(min(65536, _MAX_FILE_BYTES + 1 - total))
                if not block:
                    break
                total += len(block)
                if total > _MAX_FILE_BYTES:
                    raise StagingFilesystemError("staging_file_size_invalid")
                digest.update(block)
            if digest.hexdigest() != expected_sha256:
                raise StagingFilesystemError("staging_file_changed")
            file_object.close()
            self._windows_files[filename] = pinned_handle
        except StagingFilesystemError:
            if file_object is not None:
                file_object.close()
            elif descriptor is not None:
                os.close(descriptor)
            if read_handle is not None:
                self._close_handle(read_handle)
            self._close_handle(pinned_handle)
            raise
        except OSError as error:
            if file_object is not None:
                file_object.close()
            elif descriptor is not None:
                os.close(descriptor)
            if read_handle is not None:
                self._close_handle(read_handle)
            self._close_handle(pinned_handle)
            raise StagingFilesystemError("staging_file_unavailable") from error

    def verify_owned(self, filename: str, identity: tuple[int, int]) -> bool:
        """Confirm a staged path still names its original single-link file."""

        filename = _safe_name(filename)
        with self._lock:
            self._check_open()
            if self._entries.get(filename) != identity:
                return False
            if self._is_windows:
                handle = self._windows_files.get(filename)
                if handle is None:
                    handle_value = self._win_create_file(
                        str(self.directory / filename),
                        0x00000080,
                        0x00000001,
                        None,
                        3,
                        0x00200000,
                        None,
                    )
                    import ctypes.wintypes

                    if handle_value == ctypes.wintypes.HANDLE(-1).value:
                        return False
                    handle = int(handle_value)
                    close_after = True
                else:
                    close_after = False
                observed_identity, attributes, links = self._windows_file_info(handle)
                result = (
                    observed_identity == identity
                    and not attributes & (0x00000010 | 0x00000400)
                    and links == 1
                )
                if close_after:
                    self._close_handle(handle)
                return result
            if self._posix_directory_fd is None:
                return False
            try:
                info = os.stat(filename, dir_fd=self._posix_directory_fd, follow_symlinks=False)
            except OSError:
                return False
            return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and _identity(info) == identity

    def _unlink_posix(self, filename: str, identity: tuple[int, int] | None) -> bool:
        if self._posix_directory_fd is None or identity is None:
            return False
        try:
            info = os.stat(
                filename,
                dir_fd=self._posix_directory_fd,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(info.st_mode):
                return False
            if info.st_nlink != 1:
                return False
            if identity is not None and _identity(info) != identity:
                return False
            os.unlink(filename, dir_fd=self._posix_directory_fd)
            return True
        except FileNotFoundError:
            return False
        except OSError as error:
            if getattr(error, "winerror", None) in {32, 33}:
                return False
            raise StagingFilesystemError("staging_delete_failed") from error

    def _delete_windows(self, filename: str, identity: tuple[int, int]) -> bool:
        pinned_handle = self._windows_files.get(filename)
        try:
            if pinned_handle is None:
                return self._delete_windows_unpinned(filename, identity)
            observed_identity, attributes, _links = self._windows_file_info(pinned_handle)
            if observed_identity != identity or attributes & (0x00000010 | 0x00000400):
                return False
            self._close_handle(pinned_handle)
            self._windows_files.pop(filename, None)
            return self._delete_windows_unpinned(filename, identity)
        except OSError as error:
            if getattr(error, "winerror", None) in {32, 33}:
                return False
            raise StagingFilesystemError("staging_delete_failed") from error

    def _delete_windows_unpinned(self, filename: str, identity: tuple[int, int]) -> bool:
        try:
            handle = self._nt_create_directory_relative(
                self._windows_child_handle,
                filename,
                0x00010000 | 0x00000080,
                0x00000001,
                0x00000080,
                existing=True,
                directory=False,
            )
        except StagingFilesystemError as error:
            if getattr(error, "winerror", None) in {2, 3}:
                return False
            raise
        try:
            observed_identity, attributes, _links = self._windows_file_info(handle)
            if (
                observed_identity != identity
                or attributes & (0x00000010 | 0x00000400)
                or _links != 1
            ):
                return False
            self._mark_windows_delete(handle)
            return True
        finally:
            self._close_handle(handle)

    def close(self, *, remove_directory: bool = True, delete_entries: bool = True) -> bool:
        """Release handles and report whether owned staging was removed."""
        with self._lock:
            if self._closed:
                return not self._entries
            cleanup_complete = True
            for filename, descriptor in tuple(self._writer_descriptors.items()):
                try:
                    os.close(descriptor)
                except OSError:
                    cleanup_complete = False
                self._writer_descriptors.pop(filename, None)
            try:
                self.release_lease()
            except OSError:
                cleanup_complete = False
            for filename, identity in tuple(self._entries.items()) if delete_entries else ():
                try:
                    if self._is_windows:
                        deleted = self._delete_windows(filename, identity)
                    else:
                        deleted = self._unlink_posix(filename, identity)
                    if deleted:
                        self._entries.pop(filename, None)
                    else:
                        cleanup_complete = False
                except StagingFilesystemError:
                    cleanup_complete = False
            if self._is_windows:
                directory_removed = self._close_windows(remove_directory)
            else:
                directory_removed = self._close_posix(remove_directory)
            self._lease_name = None
            self._lease_identity = None
            self._closed = True
            return cleanup_complete and directory_removed

    def _close_posix(self, remove_directory: bool) -> bool:
        root_fd, directory_fd = self._posix_root_fd, self._posix_directory_fd
        removed = not remove_directory
        if remove_directory and root_fd is not None and self._directory_identity is not None:
            try:
                info = os.stat(self.session_name, dir_fd=root_fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode) and _identity(info) == self._directory_identity:
                    os.rmdir(self.session_name, dir_fd=root_fd)
                    removed = True
            except FileNotFoundError:
                removed = True
            except OSError:
                removed = False
        if directory_fd is not None:
            os.close(directory_fd)
        if root_fd is not None:
            os.close(root_fd)
        self._posix_directory_fd = None
        self._posix_root_fd = None
        self._lease_name = None
        self._lease_identity = None
        return removed

    def _close_windows(self, remove_directory: bool) -> bool:
        disposition_set = not remove_directory
        if remove_directory and self._windows_child_handle is not None:
            session_handle = self._windows_child_handle
            try:
                import ctypes
                import ctypes.wintypes

                disposition = ctypes.c_ubyte(1)
                set_info = self._win_kernel32.SetFileInformationByHandle
                set_info.argtypes = [
                    ctypes.wintypes.HANDLE,
                    ctypes.wintypes.DWORD,
                    ctypes.c_void_p,
                    ctypes.wintypes.DWORD,
                ]
                set_info.restype = ctypes.wintypes.BOOL
                disposition_set = bool(
                    set_info(
                        session_handle,
                        4,
                        ctypes.byref(disposition),
                        ctypes.sizeof(disposition),
                    )
                )
            except (AttributeError, OSError):
                disposition_set = False
        self._close_handles()
        if not remove_directory:
            return True
        try:
            return disposition_set and not self.directory.exists()
        except OSError:
            return False

    def _mark_windows_delete(self, handle: int) -> None:
        import ctypes
        import ctypes.wintypes

        disposition = ctypes.c_ubyte(1)
        set_info = self._win_kernel32.SetFileInformationByHandle
        set_info.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.wintypes.DWORD,
        ]
        set_info.restype = ctypes.wintypes.BOOL
        if not set_info(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
            raise self._win_os_error("staging_delete_failed")

    def _windows_file_info(self, handle: int) -> tuple[tuple[int, int], int, int]:
        import ctypes

        info = self._win_info_type()
        if not self._win_get_info(handle, ctypes.byref(info)):
            raise self._win_os_error("staging_file_unavailable")
        file_index = (int(info.index_high) << 32) | int(info.index_low)
        return (int(info.volume_serial), file_index), int(info.attributes), int(info.links)

    def _windows_file_size(self, handle: int) -> int:
        import ctypes

        info = self._win_info_type()
        if not self._win_get_info(handle, ctypes.byref(info)):
            raise self._win_os_error("staging_file_unavailable")
        return (int(info.size_high) << 32) | int(info.size_low)

    def _remove_windows_directory_if_owned(self) -> None:
        # During a partial open, only remove the generated child if the open
        # handles still pin the approved root. Keep the operation fail-closed.
        if self._windows_child_handle is None:
            return
        try:
            self._mark_windows_delete(self._windows_child_handle)
        except OSError:
            return

    @staticmethod
    def _close_handle(handle: int) -> None:
        import ctypes
        import ctypes.wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
        kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
        if not kernel32.CloseHandle(handle):
            raise OSError(ctypes.get_last_error(), "staging_handle_close_failed")

    def _close_handles(self) -> None:
        for handle in tuple(self._windows_files.values()):
            try:
                self._close_handle(handle)
            except OSError:
                pass
        self._windows_files.clear()
        for handle in reversed(self._windows_handles):
            try:
                self._close_handle(handle)
            except OSError:
                pass
        self._windows_handles.clear()

    def __enter__(self) -> PinnedStagingDirectory:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = ["PinnedStagingDirectory", "StagingFilesystemError"]
