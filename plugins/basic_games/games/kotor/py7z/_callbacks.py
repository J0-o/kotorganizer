"""
Copyright (c) Modding Forge
"""

from __future__ import annotations

import ctypes
import time
from pathlib import Path
from typing import Callable, Optional

from .progress import ProgressInfo

from ._com import (
    E_ABORT,
    E_NOTIMPL,
    S_FALSE,
    S_OK,
    PyCOMObject,
    HRESULT_T,
    PVOID,
    _PUINT64,
    com_method,
)
from ._guids import (
    IID_IArchiveExtractCallback,
    IID_IArchiveOpenCallback,
    IID_IArchiveUpdateCallback,
    IID_IArchiveUpdateCallback2,
    IID_ICryptoGetTextPassword,
    IID_ICryptoGetTextPassword2,
    IID_IProgress,
)

# ---------------------------------------------------------------------------
# Operation result codes from SevenZipExtractOperationResult
# ---------------------------------------------------------------------------

OP_RESULT_SUCCESS: int = 0
OP_RESULT_UNSUPPORTED_METHOD: int = 1
OP_RESULT_DATA_ERROR: int = 2
OP_RESULT_CRC_ERROR: int = 3
OP_RESULT_UNAVAILABLE: int = 4
OP_RESULT_UNEXPECTED_END: int = 5
OP_RESULT_DATA_AFTER_END: int = 6
OP_RESULT_IS_NOT_ARCHIVE: int = 7
OP_RESULT_HEADERS_ERROR: int = 8
OP_RESULT_WRONG_PASSWORD: int = 9

# Ask-mode values
ASK_EXTRACT: int = 0
ASK_TEST: int = 1
ASK_SKIP: int = 2

# ---------------------------------------------------------------------------
# vtable slot signatures
# ---------------------------------------------------------------------------

_SET_TOTAL_FUNC = com_method(HRESULT_T, PVOID, ctypes.c_uint64)
_SET_COMPLETED_FUNC = com_method(HRESULT_T, PVOID, _PUINT64)
_GET_STREAM_EX_FUNC = com_method(
    HRESULT_T, PVOID, ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_void_p), ctypes.c_int32,
)
_PREPARE_OP_FUNC = com_method(HRESULT_T, PVOID, ctypes.c_int32)
_SET_OP_RESULT_FUNC = com_method(HRESULT_T, PVOID, ctypes.c_int32)
_OPEN_SET_TOTAL_FUNC = com_method(
    HRESULT_T, PVOID, _PUINT64, _PUINT64
)
_OPEN_SET_COMPLETED_FUNC = com_method(
    HRESULT_T, PVOID, _PUINT64, _PUINT64
)
_GET_TEXT_PW_FUNC = com_method(
    HRESULT_T, PVOID, ctypes.POINTER(ctypes.c_wchar_p)
)
_GET_TEXT_PW2_FUNC = com_method(
    HRESULT_T, PVOID,
    ctypes.POINTER(ctypes.c_int32),
    ctypes.POINTER(ctypes.c_wchar_p),
)
_GET_UPDATE_ITEM_INFO_FUNC = com_method(
    HRESULT_T, PVOID, ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_int32),
    ctypes.POINTER(ctypes.c_int32),
    ctypes.POINTER(ctypes.c_uint32),
)
_GET_PROPERTY_FUNC = com_method(
    HRESULT_T, PVOID, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p
)
_GET_STREAM_IN_FUNC = com_method(
    HRESULT_T, PVOID, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)
)


_oleaut32 = ctypes.WinDLL("oleaut32.dll")
_sys_alloc_string = _oleaut32.SysAllocString
_sys_alloc_string.restype = ctypes.c_void_p
_sys_alloc_string.argtypes = [ctypes.c_wchar_p]


def _alloc_bstr(text: str) -> int:
    """
    Allocates a Windows BSTR via `SysAllocString`.

    Args:
        text (str): The string to allocate.

    Returns:
        int: Pointer to the allocated BSTR.
    """

    return _sys_alloc_string(text) or 0


def _update_timing(info: ProgressInfo, start: float) -> None:
    """
    Recomputes elapsed, speed, remaining, and percent on *info* in place.

    Mirrors the calculation from NanaZip's
    `CProgressDialog::UpdateStatInfo` in `ProgressDialog2.cpp`.

    Args:
        info (ProgressInfo): The progress object to update.
        start (float): `time.perf_counter()` value at operation start.
    """

    elapsed: float = time.perf_counter() - start
    info.elapsed_seconds = elapsed
    if elapsed > 0.0:
        info.speed_bytes_per_sec = info.completed_bytes / elapsed
    else:
        info.speed_bytes_per_sec = 0.0
    if info.total_bytes > 0:
        info.percent = min(
            100.0, info.completed_bytes / info.total_bytes * 100.0
        )
        if info.speed_bytes_per_sec > 0.0:
            remaining: int = info.total_bytes - info.completed_bytes
            info.remaining_seconds = (
                remaining / info.speed_bytes_per_sec
            )
        else:
            info.remaining_seconds = None
    elif info.total_files > 0:
        info.percent = min(
            100.0, info.completed_files / info.total_files * 100.0
        )
        info.remaining_seconds = None
    else:
        info.percent = 0.0
        info.remaining_seconds = None


# ---------------------------------------------------------------------------
# OpenCallback
# ---------------------------------------------------------------------------

class OpenCallback(PyCOMObject):
    """
    COM implementation of `IArchiveOpenCallback` and, optionally,
    `ICryptoGetTextPassword`.

    Passed to `IInArchive::Open` to report progress and provide
    a password for encrypted archives.
    """

    _vtable_methods = [
        ("SetTotal_open", _OPEN_SET_TOTAL_FUNC),
        ("SetCompleted_open", _OPEN_SET_COMPLETED_FUNC),
    ]

    _password: Optional[str]

    def __init__(self, password: Optional[str] = None) -> None:
        """
        Creates the open callback.

        Args:
            password (Optional[str]): Password for encrypted archives,
                or `None` if no password is required.
        """

        self._password = password
        self._iids = [IID_IArchiveOpenCallback]
        if password is not None:
            self._iids.append(IID_ICryptoGetTextPassword)
            self._vtable_methods = self._vtable_methods + [
                ("CryptoGetTextPassword", _GET_TEXT_PW_FUNC),
            ]
        super().__init__()

    def _SetTotal_open(
        self,
        this: ctypes.c_void_p,
        files_ptr: ctypes.POINTER(ctypes.c_uint64),  # type: ignore[valid-type]
        bytes_ptr: ctypes.POINTER(ctypes.c_uint64),  # type: ignore[valid-type]
    ) -> int:
        """
        Implements `IArchiveOpenCallback::SetTotal`.

        Args:
            this (ctypes.c_void_p): Ignored.
            files_ptr: Pointer to total file count.
            bytes_ptr: Pointer to total byte count.

        Returns:
            int: `S_OK`.
        """

        return S_OK

    def _SetCompleted_open(
        self,
        this: ctypes.c_void_p,
        files_ptr: ctypes.POINTER(ctypes.c_uint64),  # type: ignore[valid-type]
        bytes_ptr: ctypes.POINTER(ctypes.c_uint64),  # type: ignore[valid-type]
    ) -> int:
        """
        Implements `IArchiveOpenCallback::SetCompleted`.

        Args:
            this (ctypes.c_void_p): Ignored.
            files_ptr: Pointer to processed file count.
            bytes_ptr: Pointer to processed byte count.

        Returns:
            int: `S_OK`.
        """

        return S_OK

    def _CryptoGetTextPassword(
        self,
        this: ctypes.c_void_p,
        password_ptr: ctypes.POINTER(ctypes.c_wchar_p),  # type: ignore[valid-type]
    ) -> int:
        """
        Implements `ICryptoGetTextPassword::CryptoGetTextPassword`.

        Args:
            this (ctypes.c_void_p): Ignored.
            password_ptr: Pointer to receive the BSTR password.

        Returns:
            int: `S_OK` or `E_ABORT` if no password is set.
        """

        if self._password is None:
            return E_ABORT
        if password_ptr:
            password_ptr[0] = _alloc_bstr(self._password)
        return S_OK


# ---------------------------------------------------------------------------
# ExtractCallback
# ---------------------------------------------------------------------------

ProgressCallback = Callable[[ProgressInfo], None]
"""
Callback signature for progress notifications.

Receives a :class:`~py7z.progress.ProgressInfo` on each significant
update.  The same instance is reused across calls - do not store it.
"""

StreamFactory = Callable[[int], Optional[object]]
"""
Factory that maps a zero-based item index to an output stream or
`None` to skip the item.
"""


class ExtractCallback(PyCOMObject):
    """
    COM implementation of `IArchiveExtractCallback` and, optionally,
    `ICryptoGetTextPassword`.

    The *stream_factory* callable receives the item index and returns
    either a :class:`~py7z._streams.BytesOutStream` /
    :class:`~py7z._streams.FileOutStream` instance or `None` to skip
    the item.
    """

    _vtable_methods = [
        ("SetTotal", _SET_TOTAL_FUNC),
        ("SetCompleted", _SET_COMPLETED_FUNC),
        ("GetStream", _GET_STREAM_EX_FUNC),
        ("PrepareOperation", _PREPARE_OP_FUNC),
        ("SetOperationResult", _SET_OP_RESULT_FUNC),
    ]

    _stream_factory: StreamFactory
    _progress_cb: Optional[ProgressCallback]
    _password: Optional[str]
    _last_error: Optional[Exception]
    _current_stream: Optional[object]
    _info: ProgressInfo
    _start_time: float
    _current_index: int
    _file_name_provider: Optional[Callable[[int], str]]

    def __init__(
        self,
        stream_factory: StreamFactory,
        progress_cb: Optional[ProgressCallback] = None,
        password: Optional[str] = None,
        total_files: int = 0,
        file_name_provider: Optional[Callable[[int], str]] = None,
    ) -> None:
        """
        Creates the extract callback.

        Args:
            stream_factory (StreamFactory): Callable that maps item index
                to an output stream (or `None` to skip).
            progress_cb (Optional[ProgressCallback]): Optional progress
                notification callback.
            password (Optional[str]): Password for encrypted entries.
            total_files (int): Total number of items to extract.
            file_name_provider (Optional[Callable[[int], str]]): Maps
                item index to its archive path for `current_file`.
        """

        self._stream_factory = stream_factory
        self._progress_cb = progress_cb
        self._password = password
        self._last_error = None
        self._current_stream = None
        self._current_index = -1
        self._file_name_provider = file_name_provider
        self._info = ProgressInfo(
            total_files=total_files,
            is_compressing=False,
        )
        self._start_time = 0.0
        self._iids = [IID_IArchiveExtractCallback, IID_IProgress]
        if password is not None:
            self._iids.append(IID_ICryptoGetTextPassword)
            self._vtable_methods = self._vtable_methods + [
                ("CryptoGetTextPassword", _GET_TEXT_PW_FUNC),
            ]
        super().__init__()

    def _SetTotal(
        self, this: ctypes.c_void_p, total: int
    ) -> int:
        """
        Implements `IProgress::SetTotal`.

        Args:
            this (ctypes.c_void_p): Ignored.
            total (int): Total bytes to process.

        Returns:
            int: `S_OK`.
        """

        self._info.total_bytes = total
        self._start_time = time.perf_counter()
        return S_OK

    def _SetCompleted(
        self,
        this: ctypes.c_void_p,
        completed_ptr: ctypes.POINTER(ctypes.c_uint64),  # type: ignore[valid-type]
    ) -> int:
        """
        Implements `IProgress::SetCompleted`.

        Updates speed, elapsed, remaining, and percent, then fires
        the progress callback.

        Args:
            this (ctypes.c_void_p): Ignored.
            completed_ptr: Pointer to completed byte count.

        Returns:
            int: `S_OK`.
        """

        if not completed_ptr:
            return S_OK
        self._info.completed_bytes = int(completed_ptr[0])
        if self._start_time > 0.0:
            _update_timing(self._info, self._start_time)
        if self._progress_cb is not None:
            try:
                self._progress_cb(self._info)
            except Exception:
                pass
        return S_OK

    def _GetStream(
        self,
        this: ctypes.c_void_p,
        index: int,
        out_stream_ptr: ctypes.POINTER(ctypes.c_void_p),  # type: ignore[valid-type]
        ask_extract_mode: int,
    ) -> int:
        """
        Implements `IArchiveExtractCallback::GetStream`.

        Args:
            this (ctypes.c_void_p): Ignored.
            index (int): Zero-based item index.
            out_stream_ptr: Receives the output stream COM pointer.
            ask_extract_mode (int): 0=extract, 1=test, 2=skip.

        Returns:
            int: `S_OK` to extract, `S_FALSE` to skip.
        """

        self._current_index = index
        self._current_stream = None
        if ask_extract_mode != ASK_EXTRACT:
            if out_stream_ptr:
                out_stream_ptr[0] = None  # type: ignore[assignment]
            return S_OK

        try:
            stream = self._stream_factory(index)
        except Exception as exc:
            self._last_error = exc
            return E_ABORT

        if stream is None:
            if out_stream_ptr:
                out_stream_ptr[0] = None  # type: ignore[assignment]
            # Return S_OK (not S_FALSE) so 7-zip continues processing the
            # solid block.  S_FALSE causes the extraction engine to treat
            # the entire remaining solid block as done and skip all
            # subsequent items.  A NULL outStream with S_OK tells 7-zip to
            # advance (decompress + discard) without writing.
            return S_OK

        self._current_stream = stream
        if out_stream_ptr:
            out_stream_ptr[0] = ctypes.cast(
                stream.as_void_p, ctypes.c_void_p  # type: ignore[attr-defined]
            ).value
        return S_OK

    def _PrepareOperation(
        self, this: ctypes.c_void_p, ask_extract_mode: int
    ) -> int:
        """
        Implements `IArchiveExtractCallback::PrepareOperation`.

        Updates `current_file` on the ProgressInfo using the index
        stored by `_GetStream` and the file-name provider.

        Args:
            this (ctypes.c_void_p): Ignored.
            ask_extract_mode (int): The operation mode.

        Returns:
            int: `S_OK`.
        """

        if (
            self._file_name_provider is not None
            and self._current_index >= 0
        ):
            try:
                self._info.current_file = self._file_name_provider(
                    self._current_index
                )
            except Exception:
                pass
        return S_OK

    def _SetOperationResult(
        self, this: ctypes.c_void_p, operation_result: int
    ) -> int:
        """
        Implements `IArchiveExtractCallback::SetOperationResult`.

        Increments `completed_files`, fires the progress callback,
        then stores any error.

        Args:
            this (ctypes.c_void_p): Ignored.
            operation_result (int): Result code for the current item.

        Returns:
            int: `S_OK`.
        """

        self._current_stream = None
        self._info.completed_files += 1
        if self._start_time > 0.0:
            _update_timing(self._info, self._start_time)
        if self._progress_cb is not None:
            try:
                self._progress_cb(self._info)
            except Exception:
                pass
        if operation_result == OP_RESULT_WRONG_PASSWORD:
            from .exceptions import WrongPasswordError
            self._last_error = WrongPasswordError(
                "Wrong or missing password."
            )
        elif operation_result not in (
            OP_RESULT_SUCCESS,
            OP_RESULT_DATA_AFTER_END,
        ):
            from .exceptions import ExtractionError
            self._last_error = ExtractionError(operation_result)
        return S_OK

    def _CryptoGetTextPassword(
        self,
        this: ctypes.c_void_p,
        password_ptr: ctypes.POINTER(ctypes.c_wchar_p),  # type: ignore[valid-type]
    ) -> int:
        """
        Implements `ICryptoGetTextPassword::CryptoGetTextPassword`.

        Args:
            this (ctypes.c_void_p): Ignored.
            password_ptr: Receives the BSTR password.

        Returns:
            int: `S_OK` or `E_ABORT`.
        """

        if self._password is None:
            return E_ABORT
        if password_ptr:
            password_ptr[0] = _alloc_bstr(self._password)
        return S_OK

    def raise_if_error(self) -> None:
        """
        Raises any exception stored during extraction.

        Raises:
            Exception: The last error encountered during extraction,
                if any.
        """

        if self._last_error is not None:
            raise self._last_error


# ---------------------------------------------------------------------------
# UpdateCallback
# ---------------------------------------------------------------------------

class UpdateCallback(PyCOMObject):
    """
    COM implementation of `IArchiveUpdateCallback` for creating/updating
    archives.

    Callers provide a list of
    :class:`~py7z.entry.ArchiveEntryInput` objects describing the files
    to add, and an optional progress callback.
    """

    _vtable_methods = [
        ("SetTotal", _SET_TOTAL_FUNC),
        ("SetCompleted", _SET_COMPLETED_FUNC),
        ("GetUpdateItemInfo", _GET_UPDATE_ITEM_INFO_FUNC),
        ("GetProperty", _GET_PROPERTY_FUNC),
        ("GetStream", _GET_STREAM_IN_FUNC),
        ("SetOperationResult", _SET_OP_RESULT_FUNC),
    ]

    _entries: list[object]
    _progress_cb: Optional[ProgressCallback]
    _password: Optional[str]
    _info: ProgressInfo
    _start_time: float

    def __init__(
        self,
        entries: list[object],
        progress_cb: Optional[ProgressCallback] = None,
        password: Optional[str] = None,
    ) -> None:
        """
        Creates the update callback.

        Args:
            entries (list[object]): List of
                :class:`~py7z.entry.ArchiveEntryInput` describing files
                to add.
            progress_cb (Optional[ProgressCallback]): Optional progress
                notification callback.
            password (Optional[str]): Password for encrypted archives.
        """

        self._entries = entries
        self._progress_cb = progress_cb
        self._password = password
        self._info = ProgressInfo(
            total_files=len(entries),
            is_compressing=True,
        )
        self._start_time = 0.0
        self._iids = [IID_IArchiveUpdateCallback, IID_IProgress]
        if password is not None:
            self._iids.append(IID_ICryptoGetTextPassword2)
            self._vtable_methods = self._vtable_methods + [
                ("CryptoGetTextPassword2", _GET_TEXT_PW2_FUNC),
            ]
        super().__init__()

    def _SetTotal(self, this: ctypes.c_void_p, total: int) -> int:
        """
        Implements `IProgress::SetTotal`.

        Args:
            this (ctypes.c_void_p): Ignored.
            total (int): Total byte count.

        Returns:
            int: `S_OK`.
        """

        self._info.total_bytes = total
        self._start_time = time.perf_counter()
        return S_OK

    def _SetCompleted(
        self,
        this: ctypes.c_void_p,
        completed_ptr: ctypes.POINTER(ctypes.c_uint64),  # type: ignore[valid-type]
    ) -> int:
        """
        Implements `IProgress::SetCompleted`.

        Updates speed, elapsed, remaining, and percent, then fires
        the progress callback.

        Args:
            this (ctypes.c_void_p): Ignored.
            completed_ptr: Pointer to completed byte count.

        Returns:
            int: `S_OK`.
        """

        if not completed_ptr:
            return S_OK
        self._info.completed_bytes = int(completed_ptr[0])
        if self._start_time > 0.0:
            _update_timing(self._info, self._start_time)
        if self._progress_cb is not None:
            try:
                self._progress_cb(self._info)
            except Exception:
                pass
        return S_OK

    def _GetUpdateItemInfo(
        self,
        this: ctypes.c_void_p,
        index: int,
        new_data_ptr: ctypes.POINTER(ctypes.c_int32),  # type: ignore[valid-type]
        new_props_ptr: ctypes.POINTER(ctypes.c_int32),  # type: ignore[valid-type]
        index_in_archive_ptr: ctypes.POINTER(ctypes.c_uint32),  # type: ignore[valid-type]
    ) -> int:
        """
        Implements `IArchiveUpdateCallback::GetUpdateItemInfo`.

        All items are treated as new (no existing archive update).

        Args:
            this (ctypes.c_void_p): Ignored.
            index (int): Item index.
            new_data_ptr: Receives 1 for new data.
            new_props_ptr: Receives 1 for new properties.
            index_in_archive_ptr: Receives 0xFFFFFFFF (new item).

        Returns:
            int: `S_OK`.
        """

        if new_data_ptr:
            new_data_ptr[0] = 1
        if new_props_ptr:
            new_props_ptr[0] = 1
        if index_in_archive_ptr:
            index_in_archive_ptr[0] = 0xFFFFFFFF
        return S_OK

    def _GetProperty(
        self,
        this: ctypes.c_void_p,
        index: int,
        prop_id: int,
        pv_ptr: ctypes.c_void_p,
    ) -> int:
        """
        Implements `IArchiveUpdateCallback::GetProperty`.

        Reads properties from the :class:`~py7z.entry.ArchiveEntryInput`
        at position *index*.

        Args:
            this (ctypes.c_void_p): Ignored.
            index (int): Item index.
            prop_id (int): Property ID.
            pv_ptr: Pointer to a PROPVARIANT to fill.

        Returns:
            int: `S_OK` or `E_NOTIMPL`.
        """

        from ._propvariant import PROPVARIANT, VT_EMPTY

        if index >= len(self._entries):
            return E_NOTIMPL
        entry = self._entries[index]
        pv = PROPVARIANT.from_address(pv_ptr)  # type: ignore[attr-defined]
        pv.vt = VT_EMPTY

        # kpid values from SevenZipArchivePropertyType
        KPID_PATH = 3
        KPID_IS_DIR = 6
        KPID_SIZE = 7
        KPID_ATTRIB = 9
        KPID_CTIME = 10
        KPID_ATIME = 11
        KPID_MTIME = 12

        try:
            if prop_id == KPID_PATH:
                from ._propvariant import VT_BSTR
                pv.vt = VT_BSTR
                # Must allocate a proper BSTR - 7-Zip calls SysFreeString
                # on VT_BSTR values; a plain Python string pointer would
                # corrupt the heap.
                pv._data.ptr = _alloc_bstr(
                    getattr(entry, "archive_path", "")
                )
            elif prop_id == KPID_IS_DIR:
                from ._propvariant import VT_BOOL
                pv.vt = VT_BOOL
                pv._data.boolVal = (
                    -1 if getattr(entry, "is_directory", False) else 0
                )
            elif prop_id == KPID_SIZE:
                from ._propvariant import VT_UI8
                size: int = getattr(entry, "size", 0) or 0
                pv.vt = VT_UI8
                pv._data.uhVal = size
            elif prop_id == KPID_ATTRIB:
                from ._propvariant import VT_UI4
                attrib: int = getattr(entry, "attributes", 0x20) or 0x20
                pv.vt = VT_UI4
                pv._data.ulVal = attrib
        except Exception:
            pass
        return S_OK

    def _GetStream(
        self,
        this: ctypes.c_void_p,
        index: int,
        in_stream_ptr: ctypes.POINTER(ctypes.c_void_p),  # type: ignore[valid-type]
    ) -> int:
        """
        Implements `IArchiveUpdateCallback::GetStream`.

        Returns an `ISequentialInStream` for the file at *index*.

        Args:
            this (ctypes.c_void_p): Ignored.
            index (int): Item index.
            in_stream_ptr: Receives the input stream COM pointer.

        Returns:
            int: `S_OK` or `S_FALSE` to skip.
        """

        from ._streams import FileInStream

        if index >= len(self._entries):
            if in_stream_ptr:
                in_stream_ptr[0] = None  # type: ignore[assignment]
            return S_FALSE
        entry = self._entries[index]
        archive_path: str = getattr(entry, "archive_path", "")
        self._info.current_file = archive_path or None
        if getattr(entry, "is_directory", False):
            if in_stream_ptr:
                in_stream_ptr[0] = None  # type: ignore[assignment]
            return S_OK
        src: Optional[Path] = getattr(entry, "source_path", None)
        if src is None or not src.exists():
            if in_stream_ptr:
                in_stream_ptr[0] = None  # type: ignore[assignment]
            return S_FALSE
        stream = FileInStream(src)
        # Keep stream alive for duration of write
        if not hasattr(self, "_active_streams"):
            object.__setattr__(self, "_active_streams", [])
        self._active_streams.append(stream)  # type: ignore[attr-defined]
        if in_stream_ptr:
            in_stream_ptr[0] = ctypes.cast(
                stream.as_void_p, ctypes.c_void_p
            ).value
        return S_OK

    def _SetOperationResult(
        self, this: ctypes.c_void_p, operation_result: int
    ) -> int:
        """
        Implements `IArchiveUpdateCallback::SetOperationResult`.

        Increments `completed_files` and fires the progress callback.

        Args:
            this (ctypes.c_void_p): Ignored.
            operation_result (int): Result for the current item.

        Returns:
            int: `S_OK`.
        """

        self._info.completed_files += 1
        if self._start_time > 0.0:
            _update_timing(self._info, self._start_time)
        if self._progress_cb is not None:
            try:
                self._progress_cb(self._info)
            except Exception:
                pass
        return S_OK

    def _CryptoGetTextPassword2(
        self,
        this: ctypes.c_void_p,
        password_defined_ptr: ctypes.POINTER(ctypes.c_int32),  # type: ignore[valid-type]
        password_ptr: ctypes.POINTER(ctypes.c_wchar_p),  # type: ignore[valid-type]
    ) -> int:
        """
        Implements `ICryptoGetTextPassword2::CryptoGetTextPassword2`.

        Args:
            this (ctypes.c_void_p): Ignored.
            password_defined_ptr: Receives 1 if a password is set.
            password_ptr: Receives the BSTR password.

        Returns:
            int: `S_OK`.
        """

        if self._password is None:
            if password_defined_ptr:
                password_defined_ptr[0] = 0
            return S_OK
        if password_defined_ptr:
            password_defined_ptr[0] = 1
        if password_ptr:
            password_ptr[0] = _alloc_bstr(self._password)
        return S_OK
