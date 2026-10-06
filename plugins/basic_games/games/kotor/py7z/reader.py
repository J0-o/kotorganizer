"""
Copyright (c) Modding Forge
"""

from __future__ import annotations

import ctypes
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Optional, cast

from ._callbacks import ExtractCallback, OpenCallback, ProgressCallback
from ._com import (
    HRESULT_T,
    PVOID,
    COMPtr,
    check_hr,
    com_method,
)
from ._dll import create_in_archive
from ._guids import FORMAT_EXTENSION_MAP, GUID, CLSID_7z, CLSID_Rar, CLSID_Rar5
from ._propvariant import PROPVARIANT, clear_propvariant
from ._streams import FileInStream
from .entry import (
    KPID_ATIME,
    KPID_ATTRIB,
    KPID_CRC,
    KPID_CTIME,
    KPID_ENCRYPTED,
    KPID_IS_ANTI,
    KPID_IS_DIR,
    KPID_METHOD,
    KPID_MTIME,
    KPID_PACK_SIZE,
    KPID_PATH,
    KPID_SIZE,
    ArchiveEntry,
)
from .exceptions import ArchiveOpenError, ExtractionError

# ---------------------------------------------------------------------------
# IInArchive vtable slot indices (0 = QI, 1 = AddRef, 2 = Release, …)
# ---------------------------------------------------------------------------

_VTBL_OPEN: int = 3
_VTBL_CLOSE: int = 4
_VTBL_GET_NUM_ITEMS: int = 5
_VTBL_GET_PROPERTY: int = 6
_VTBL_EXTRACT: int = 7
_VTBL_GET_ARCHIVE_PROPERTY: int = 8
_VTBL_GET_NUM_PROPERTIES: int = 9
_VTBL_GET_PROPERTY_INFO: int = 10
_VTBL_GET_NUM_ARCHIVE_PROPERTIES: int = 11
_VTBL_GET_ARCHIVE_PROPERTY_INFO: int = 12

# IInArchive vtable function types
_OPEN_FUNC = com_method(
    HRESULT_T,
    PVOID,                         # this
    ctypes.c_void_p,                # IInStream*
    ctypes.POINTER(ctypes.c_uint64),  # maxCheckStartPos*
    ctypes.c_void_p,                # IArchiveOpenCallback*
)
_CLOSE_FUNC = com_method(HRESULT_T, PVOID)
_GET_NUM_ITEMS_FUNC = com_method(
    HRESULT_T, PVOID, ctypes.POINTER(ctypes.c_uint32)
)
_GET_PROPERTY_FUNC = com_method(
    HRESULT_T, PVOID,
    ctypes.c_uint32,                # index
    ctypes.c_uint32,                # propId
    ctypes.POINTER(PROPVARIANT),    # value
)
_EXTRACT_FUNC = com_method(
    HRESULT_T, PVOID,
    ctypes.POINTER(ctypes.c_uint32),  # indices (NULL = all)
    ctypes.c_uint32,                  # numItems
    ctypes.c_int32,                   # testMode
    ctypes.c_void_p,                  # IArchiveExtractCallback*
)
_GET_ARCHIVE_PROPERTY_FUNC = com_method(
    HRESULT_T, PVOID,
    ctypes.c_uint32,                # propId
    ctypes.POINTER(PROPVARIANT),    # value
)


class ArchiveReader:
    """
    High-level wrapper around `IInArchive` for reading 7-Zip archives.

    Opens the archive on construction and closes it when the object
    is used as a context manager or when :meth:`close` is called.

    Usage::

        with ArchiveReader(Path("archive.7z")) as reader:
            for entry in reader.entries():
                print(entry.path, entry.size)
            reader.extract_all(Path("output/"))
    """

    _path: Path
    _password: Optional[str]
    _dll_path: Optional[Path]
    _archive: Optional[COMPtr]
    _in_stream: Optional[FileInStream]
    _open_callback: Optional[OpenCallback]
    _num_items: Optional[int]

    def __init__(
        self,
        path: Path,
        password: Optional[str] = None,
        dll_path: Optional[Path] = None,
    ) -> None:
        """
        Opens the archive file.

        Args:
            path (Path): Path to the archive file.
            password (Optional[str]): Password for encrypted archives.
            dll_path (Optional[Path]): Custom path to 7z.dll.

        Raises:
            ArchiveFormatError: If the file extension is not recognised.
            ArchiveOpenError: If `IInArchive::Open` fails.
            DllLoadError: If the DLL cannot be loaded.
            FileNotFoundError: If *path* does not exist.
        """

        self._path = path
        self._password = password
        self._dll_path = dll_path
        self._archive = None
        self._in_stream = None
        self._open_callback = None
        self._num_items = None
        self._open()

    def _open(self) -> None:
        """
        Creates the `IInArchive` COM object and calls `Open`.

        Raises:
            ArchiveFormatError: Unknown extension.
            ArchiveOpenError: Open failure.
        """

        ext: str = self._path.suffix.lower()
        if ext == ".rar":
            with self._path.open("rb") as archive_file:
                clsid = CLSID_Rar5 if archive_file.read(8) == b"Rar!\x1a\x07\x01\x00" else CLSID_Rar
        else:
            clsid = FORMAT_EXTENSION_MAP.get(ext)
        if clsid is None:
            # Fall back to auto-detection by trying 7z format
            clsid = CLSID_7z

        self._archive = create_in_archive(clsid, self._dll_path)
        self._in_stream = FileInStream(self._path)
        self._open_callback = OpenCallback(self._password)

        open_fn = _OPEN_FUNC(self._archive.vtable[_VTBL_OPEN])
        hr: int = open_fn(
            self._archive.raw,
            self._in_stream.as_void_p,
            None,
            self._open_callback.as_void_p,
        )
        if not _hr_succeeded(hr):
            raise ArchiveOpenError(
                f"Failed to open archive '{self._path}': "
                f"HRESULT 0x{hr & 0xFFFFFFFF:08X}"
            )

    def close(self) -> None:
        """
        Closes the archive and releases all COM resources.
        """

        if self._archive is not None:
            close_fn = _CLOSE_FUNC(self._archive.vtable[_VTBL_CLOSE])
            close_fn(self._archive.raw)
            self._archive = None
        self._in_stream = None
        self._open_callback = None

    def __enter__(self) -> ArchiveReader:
        """
        Returns *self* when entering a `with` block.

        Returns:
            ArchiveReader: This instance.
        """

        return self

    def __exit__(self, *_: object) -> None:
        """
        Closes the archive on leaving a `with` block.
        """

        self.close()

    def count(self) -> int:
        """
        Returns the total number of entries in the archive.

        Returns:
            int: Number of items including directories.

        Raises:
            HResultError: If the COM call fails.
        """

        if self._num_items is not None:
            return self._num_items
        assert self._archive is not None
        n = ctypes.c_uint32(0)
        fn = _GET_NUM_ITEMS_FUNC(
            self._archive.vtable[_VTBL_GET_NUM_ITEMS]
        )
        check_hr(fn(self._archive.raw, ctypes.byref(n)), "GetNumberOfItems")
        self._num_items = int(n.value)
        return self._num_items

    def _get_entry_property(
        self, index: int, prop_id: int
    ) -> object:
        """
        Reads a single property for an archive entry.

        Args:
            index (int): Entry index.
            prop_id (int): Property ID constant.

        Returns:
            object: Python value (or `None` for VT_EMPTY).
        """

        assert self._archive is not None
        pv = PROPVARIANT()
        fn = _GET_PROPERTY_FUNC(
            self._archive.vtable[_VTBL_GET_PROPERTY]
        )
        fn(
            self._archive.raw,
            ctypes.c_uint32(index),
            ctypes.c_uint32(prop_id),
            ctypes.byref(pv),
        )
        result = pv.to_python()
        clear_propvariant(pv)
        return result

    def get_entry(self, index: int) -> ArchiveEntry:
        """
        Returns a fully populated :class:`~py7z.entry.ArchiveEntry`
        for the given index.

        Args:
            index (int): Zero-based entry index.

        Returns:
            ArchiveEntry: Entry metadata.
        """

        def _prop(pid: int) -> object:
            return self._get_entry_property(index, pid)

        path_val = _prop(KPID_PATH)
        return ArchiveEntry(
            index=index,
            path=str(path_val) if path_val is not None else "",
            is_directory=bool(_prop(KPID_IS_DIR) or False),
            size=cast(int, _prop(KPID_SIZE) or 0),
            packed_size=cast(int, _prop(KPID_PACK_SIZE) or 0),
            crc=_prop(KPID_CRC),  # type: ignore[arg-type]
            attributes=cast(int, _prop(KPID_ATTRIB) or 0),
            created_at=_prop(KPID_CTIME),  # type: ignore[arg-type]
            accessed_at=_prop(KPID_ATIME),  # type: ignore[arg-type]
            modified_at=_prop(KPID_MTIME),  # type: ignore[arg-type]
            method=_prop(KPID_METHOD),  # type: ignore[arg-type]
            encrypted=bool(_prop(KPID_ENCRYPTED) or False),
            is_anti=bool(_prop(KPID_IS_ANTI) or False),
        )

    def entries(self) -> Iterator[ArchiveEntry]:
        """
        Iterates over all entries in the archive.

        Yields:
            ArchiveEntry: One entry per item.
        """

        for i in range(self.count()):
            yield self.get_entry(i)

    def extract_all(
        self,
        output_dir: Path,
        progress_cb: Optional[ProgressCallback] = None,
        overwrite: bool = True,
    ) -> None:
        """
        Extracts all entries to *output_dir*.

        Args:
            output_dir (Path): Destination directory (created if absent).
            progress_cb (Optional[ProgressCallback]): Optional progress
                callback `(completed, total) -> None`.
            overwrite (bool): Whether to overwrite existing files.

        Raises:
            ExtractionError: If any entry fails to extract.
            WrongPasswordError: If a password is incorrect.
        """

        output_dir.mkdir(parents=True, exist_ok=True)
        output_root = output_dir.resolve()
        entries_meta: list[tuple[str, bool]] = [
            self._get_extract_entry(index)
            for index in range(self.count())
        ]
        destinations = [
            _safe_output_path(output_root, entry_path)
            for entry_path, _is_directory in entries_meta
        ]

        from ._streams import FileOutStream

        active_streams: list[FileOutStream] = []

        def stream_factory(index: int) -> Optional[FileOutStream]:
            _entry_path, is_directory = entries_meta[index]
            dest = destinations[index]
            if is_directory:
                dest.mkdir(parents=True, exist_ok=True)
                return None
            if dest.exists() and not overwrite:
                return None
            stream = FileOutStream(dest)
            active_streams.append(stream)
            return stream

        def _file_name_provider(i: int) -> str:
            return entries_meta[i][0] if i < len(entries_meta) else ""

        try:
            self._extract(
                stream_factory,
                progress_cb,
                all_items=True,
                total_files=len(entries_meta),
                file_name_provider=_file_name_provider,
            )
        finally:
            for stream in active_streams:
                stream.close()

    def _get_extract_entry(self, index: int) -> tuple[str, bool]:
        """
        Returns the minimal metadata needed for extraction.

        Args:
            index (int): Zero-based entry index.

        Returns:
            tuple[str, bool]: Archive-relative path and directory flag.
        """

        path_val = self._get_entry_property(index, KPID_PATH)
        is_directory = bool(self._get_entry_property(index, KPID_IS_DIR) or False)
        entry_path = str(path_val) if path_val is not None else ""
        if not entry_path and not is_directory:
            entry_path = self._path.stem
            if self._path.suffix.lower() in {".tgz", ".tbz2", ".txz"}:
                entry_path += ".tar"
        return entry_path, is_directory

    def extract_entry(
        self,
        index: int,
        output_dir: Optional[Path] = None,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> bytes:
        """
        Extracts a single entry by index and returns its content.

        If *output_dir* is provided the file is also written to disk;
        otherwise only the bytes are returned.

        Args:
            index (int): Zero-based entry index.
            output_dir (Optional[Path]): Optional directory for writing
                the extracted file.
            progress_cb (Optional[ProgressCallback]): Optional progress
                callback.

        Returns:
            bytes: Decompressed content of the entry.

        Raises:
            ExtractionError: On extraction failure.
        """

        from ._streams import BytesOutStream, FileOutStream

        buf = BytesOutStream()
        file_stream: Optional[FileOutStream] = None

        def stream_factory(i: int) -> Optional[BytesOutStream]:
            if i != index:
                return None
            entry = self.get_entry(i)
            nonlocal file_stream
            if output_dir is not None and not entry.is_directory:
                dest = output_dir / entry.path.replace("\\", "/")
                file_stream = FileOutStream(dest)
            return buf

        _entry_path: str = self.get_entry(index).path
        indices = (ctypes.c_uint32 * 1)(index)
        self._extract(
            stream_factory,
            progress_cb,
            all_items=False,
            indices=indices,
            count=1,
            total_files=1,
            file_name_provider=lambda _i: _entry_path,
        )
        if file_stream is not None:
            content = buf.getvalue()
            file_stream._file.write(content)
            file_stream.close()
        return buf.getvalue()

    def _extract(
        self,
        stream_factory: object,
        progress_cb: Optional[ProgressCallback],
        all_items: bool,
        indices: Optional[ctypes.Array[ctypes.c_uint32]] = None,
        count: int = 0,
        total_files: int = 0,
        file_name_provider: Optional[Callable[[int], str]] = None,
    ) -> None:
        """
        Calls `IInArchive::Extract` with an `ExtractCallback`.

        Args:
            stream_factory: Stream factory callable.
            progress_cb (Optional[ProgressCallback]): Progress callback.
            all_items (bool): If `True` extract all items (indices=NULL).
            indices (Optional[ctypes.Array]): Array of indices (if not all).
            count (int): Number of indices.
            total_files (int): Total file count passed to the callback.
            file_name_provider (Optional[Callable[[int], str]]): Maps
                item index to archive path for `current_file`.

        Raises:
            ExtractionError: On failure.
        """

        assert self._archive is not None
        cb = ExtractCallback(
            stream_factory=stream_factory,  # type: ignore[arg-type]
            progress_cb=progress_cb,
            password=self._password,
            total_files=total_files,
            file_name_provider=file_name_provider,
        )
        extract_fn = _EXTRACT_FUNC(
            self._archive.vtable[_VTBL_EXTRACT]
        )
        if all_items:
            hr: int = extract_fn(
                self._archive.raw,
                None,
                0xFFFFFFFF,
                0,
                cb.as_void_p,
            )
        else:
            hr = extract_fn(
                self._archive.raw,
                indices,
                ctypes.c_uint32(count),
                0,
                cb.as_void_p,
            )
        check_hr(hr, "IInArchive::Extract")
        cb.raise_if_error()

    def get_archive_property(
        self, prop_id: int
    ) -> object:
        """
        Returns a global archive property (format, solid, etc.).

        Args:
            prop_id (int): Property ID constant.

        Returns:
            object: Python value or `None`.
        """

        assert self._archive is not None
        pv = PROPVARIANT()
        fn = _GET_ARCHIVE_PROPERTY_FUNC(
            self._archive.vtable[_VTBL_GET_ARCHIVE_PROPERTY]
        )
        fn(
            self._archive.raw,
            ctypes.c_uint32(prop_id),
            ctypes.byref(pv),
        )
        result = pv.to_python()
        clear_propvariant(pv)
        return result


def _safe_output_path(output_root: Path, entry_path: str) -> Path:
    destination = (output_root / entry_path.replace("\\", "/")).resolve()
    if destination != output_root and output_root not in destination.parents:
        raise ExtractionError(1, f"Archive entry escapes the output directory: {entry_path}")
    return destination


def _hr_succeeded(hr: int) -> bool:
    """
    Returns `True` when an HRESULT indicates success.

    Args:
        hr (int): HRESULT value.

    Returns:
        bool: `True` if success.
    """

    return (hr & 0x80000000) == 0
