"""
Copyright (c) Modding Forge
"""

from __future__ import annotations

import ctypes
import io
from pathlib import Path
from typing import Optional

from ._com import (
    E_NOTIMPL,
    S_FALSE,
    S_OK,
    PyCOMObject,
    HRESULT_T,
    PVOID,
    _PUINT32,
    _PUINT64,
    com_method,
)
from ._guids import (
    IID_IInStream,
    IID_IOutStream,
    IID_ISequentialInStream,
    IID_ISequentialOutStream,
    IID_IStreamGetSize,
)

# ---------------------------------------------------------------------------
# vtable slot signatures
# ---------------------------------------------------------------------------

# ISequentialInStream::Read(void* data, UINT32 size, UINT32* processed)
_READ_FUNC = com_method(
    HRESULT_T, PVOID, ctypes.c_void_p, ctypes.c_uint32, _PUINT32
)

# ISequentialOutStream::Write(const void* data, UINT32 size, UINT32* processed)
_WRITE_FUNC = com_method(
    HRESULT_T, PVOID, ctypes.c_void_p, ctypes.c_uint32, _PUINT32
)

# IInStream/IOutStream::Seek(INT64 offset, UINT32 origin, UINT64* newPos)
_SEEK_FUNC = com_method(
    HRESULT_T, PVOID, ctypes.c_int64, ctypes.c_uint32, _PUINT64
)

# IOutStream::SetSize(UINT64 newSize)
_SET_SIZE_FUNC = com_method(HRESULT_T, PVOID, ctypes.c_uint64)

# IStreamGetSize::GetSize(UINT64*)
_GET_SIZE_FUNC = com_method(HRESULT_T, PVOID, _PUINT64)

_STREAM_SEEK_SET: int = 0
_STREAM_SEEK_CUR: int = 1
_STREAM_SEEK_END: int = 2


# ---------------------------------------------------------------------------
# FileInStream - IInStream backed by a file path
# ---------------------------------------------------------------------------

class FileInStream(PyCOMObject):
    """
    COM implementation of `IInStream` backed by a `pathlib.Path`.

    Opens the file in binary-read mode on construction and closes it
    when the object is destroyed.
    """

    _iids = [IID_IInStream, IID_ISequentialInStream]
    _vtable_methods = [
        ("Read", _READ_FUNC),
        ("Seek", _SEEK_FUNC),
    ]

    _path: Path
    _file: io.RawIOBase

    def __init__(self, path: Path) -> None:
        """
        Opens *path* for binary reading.

        Args:
            path (Path): Archive file to open.

        Raises:
            FileNotFoundError: If *path* does not exist.
            OSError: On any other I/O error.
        """

        self._path = path
        self._file = open(path, "rb", buffering=0)  # noqa: SIM115
        super().__init__()

    def __del__(self) -> None:
        """
        Closes the file handle.
        """

        try:
            self._file.close()
        except Exception:
            pass

    def _Read(
        self,
        this: ctypes.c_void_p,
        data: ctypes.c_void_p,
        size: int,
        processed_ptr: ctypes.POINTER(ctypes.c_uint32),  # type: ignore[valid-type]
    ) -> int:
        """
        Implements `ISequentialInStream::Read`.

        Args:
            this (ctypes.c_void_p): Ignored COM *this* pointer.
            data (ctypes.c_void_p): Output buffer pointer.
            size (int): Maximum bytes to read.
            processed_ptr: Pointer to receive actual bytes read.

        Returns:
            int: `S_OK` on success, HRESULT error code otherwise.
        """

        try:
            if size == 0:
                if processed_ptr:
                    processed_ptr[0] = 0
                return S_OK
            buf = (ctypes.c_ubyte * size).from_address(data)  # type: ignore[arg-type]
            n: int = self._file.readinto(buf)  # type: ignore[arg-type]
            if processed_ptr:
                processed_ptr[0] = n if n is not None else 0
            return S_OK
        except Exception:
            return 0x80070005  # E_ACCESSDENIED

    def _Seek(
        self,
        this: ctypes.c_void_p,
        offset: int,
        origin: int,
        new_pos_ptr: ctypes.POINTER(ctypes.c_uint64),  # type: ignore[valid-type]
    ) -> int:
        """
        Implements `IInStream::Seek`.

        Args:
            this (ctypes.c_void_p): Ignored COM *this* pointer.
            offset (int): Seek offset.
            origin (int): Seek origin (0=SET, 1=CUR, 2=END).
            new_pos_ptr: Pointer to receive the new file position.

        Returns:
            int: `S_OK` on success, HRESULT error code otherwise.
        """

        whence_map: dict[int, int] = {
            _STREAM_SEEK_SET: 0,
            _STREAM_SEEK_CUR: 1,
            _STREAM_SEEK_END: 2,
        }
        whence: Optional[int] = whence_map.get(origin)
        if whence is None:
            return 0x80030001  # STG_E_INVALIDFUNCTION
        try:
            pos: int = self._file.seek(offset, whence)
            if new_pos_ptr:
                new_pos_ptr[0] = pos
            return S_OK
        except Exception:
            return 0x80030001


# ---------------------------------------------------------------------------
# BytesInStream - IInStream backed by an in-memory bytes buffer
# ---------------------------------------------------------------------------

class BytesInStream(PyCOMObject):
    """
    COM implementation of `IInStream` backed by a `bytes` buffer
    via `io.BytesIO`.
    """

    _iids = [IID_IInStream, IID_ISequentialInStream]
    _vtable_methods = [
        ("Read", _READ_FUNC),
        ("Seek", _SEEK_FUNC),
    ]

    _buffer: io.BytesIO

    def __init__(self, data: bytes) -> None:
        """
        Wraps *data* in an in-memory stream.

        Args:
            data (bytes): The byte content to expose as an IInStream.
        """

        self._buffer = io.BytesIO(data)
        super().__init__()

    def _Read(
        self,
        this: ctypes.c_void_p,
        data: ctypes.c_void_p,
        size: int,
        processed_ptr: ctypes.POINTER(ctypes.c_uint32),  # type: ignore[valid-type]
    ) -> int:
        """
        Implements `ISequentialInStream::Read`.

        Args:
            this (ctypes.c_void_p): Ignored.
            data (ctypes.c_void_p): Output buffer.
            size (int): Bytes requested.
            processed_ptr: Receives actual byte count.

        Returns:
            int: `S_OK`.
        """

        try:
            chunk: bytes = self._buffer.read(size)
            n: int = len(chunk)
            if n and data:
                ctypes.memmove(data, chunk, n)
            if processed_ptr:
                processed_ptr[0] = n
            return S_OK
        except Exception:
            return 0x80004005  # E_FAIL

    def _Seek(
        self,
        this: ctypes.c_void_p,
        offset: int,
        origin: int,
        new_pos_ptr: ctypes.POINTER(ctypes.c_uint64),  # type: ignore[valid-type]
    ) -> int:
        """
        Implements `IInStream::Seek`.

        Args:
            this (ctypes.c_void_p): Ignored.
            offset (int): Seek offset.
            origin (int): Seek origin.
            new_pos_ptr: Receives new position.

        Returns:
            int: `S_OK` or error HRESULT.
        """

        whence_map: dict[int, int] = {0: 0, 1: 1, 2: 2}
        whence: Optional[int] = whence_map.get(origin)
        if whence is None:
            return 0x80030001
        try:
            pos: int = self._buffer.seek(offset, whence)
            if new_pos_ptr:
                new_pos_ptr[0] = pos
            return S_OK
        except Exception:
            return 0x80030001


# ---------------------------------------------------------------------------
# BytesOutStream - IOutStream backed by an in-memory BytesIO
# ---------------------------------------------------------------------------

class BytesOutStream(PyCOMObject):
    """
    COM implementation of `IOutStream` that collects data in memory.

    After extraction, call :meth:`getvalue` to retrieve the bytes.
    """

    _iids = [IID_IOutStream, IID_ISequentialOutStream]
    _vtable_methods = [
        ("Write", _WRITE_FUNC),
        ("Seek", _SEEK_FUNC),
        ("SetSize", _SET_SIZE_FUNC),
    ]

    _buffer: io.BytesIO

    def __init__(self) -> None:
        """
        Initialises an empty in-memory output stream.
        """

        self._buffer = io.BytesIO()
        super().__init__()

    def getvalue(self) -> bytes:
        """
        Returns all bytes written to the stream.

        Returns:
            bytes: Content of the internal buffer.
        """

        return self._buffer.getvalue()

    def _Write(
        self,
        this: ctypes.c_void_p,
        data: ctypes.c_void_p,
        size: int,
        processed_ptr: ctypes.POINTER(ctypes.c_uint32),  # type: ignore[valid-type]
    ) -> int:
        """
        Implements `ISequentialOutStream::Write`.

        Args:
            this (ctypes.c_void_p): Ignored.
            data (ctypes.c_void_p): Input buffer.
            size (int): Bytes to write.
            processed_ptr: Receives actual byte count written.

        Returns:
            int: `S_OK`.
        """

        try:
            if size and data:
                raw = (ctypes.c_ubyte * size).from_address(data)  # type: ignore[arg-type]
                self._buffer.write(bytes(raw))
            if processed_ptr:
                processed_ptr[0] = size
            return S_OK
        except Exception:
            return 0x80004005

    def _Seek(
        self,
        this: ctypes.c_void_p,
        offset: int,
        origin: int,
        new_pos_ptr: ctypes.POINTER(ctypes.c_uint64),  # type: ignore[valid-type]
    ) -> int:
        """
        Implements `IOutStream::Seek`.

        Args:
            this (ctypes.c_void_p): Ignored.
            offset (int): Seek offset.
            origin (int): Seek origin.
            new_pos_ptr: Receives new position.

        Returns:
            int: `S_OK` or error HRESULT.
        """

        whence_map: dict[int, int] = {0: 0, 1: 1, 2: 2}
        whence: Optional[int] = whence_map.get(origin)
        if whence is None:
            return 0x80030001
        try:
            pos: int = self._buffer.seek(offset, whence)
            if new_pos_ptr:
                new_pos_ptr[0] = pos
            return S_OK
        except Exception:
            return 0x80030001

    def _SetSize(
        self, this: ctypes.c_void_p, new_size: int
    ) -> int:
        """
        Implements `IOutStream::SetSize` by truncating/extending the buffer.

        Args:
            this (ctypes.c_void_p): Ignored.
            new_size (int): Desired size in bytes.

        Returns:
            int: `S_OK`.
        """

        try:
            pos: int = self._buffer.tell()
            self._buffer.seek(new_size)
            self._buffer.truncate()
            self._buffer.seek(min(pos, new_size))
            return S_OK
        except Exception:
            return 0x80004005


# ---------------------------------------------------------------------------
# FileOutStream - IOutStream backed by a file on disk
# ---------------------------------------------------------------------------

class FileOutStream(PyCOMObject):
    """
    COM implementation of `IOutStream` that writes to a file on disk.

    The file is opened (and created/truncated) on construction.
    """

    _iids = [IID_IOutStream, IID_ISequentialOutStream]
    _vtable_methods = [
        ("Write", _WRITE_FUNC),
        ("Seek", _SEEK_FUNC),
        ("SetSize", _SET_SIZE_FUNC),
    ]

    _path: Path
    _file: io.RawIOBase

    def __init__(self, path: Path) -> None:
        """
        Opens (or creates) *path* for binary writing.

        Args:
            path (Path): Destination file path.

        Raises:
            OSError: On I/O errors.
        """

        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._file = open(path, "w+b", buffering=0)  # noqa: SIM115
        super().__init__()

    def __del__(self) -> None:
        """
        Closes the file handle.
        """

        try:
            self._file.close()
        except Exception:
            pass

    def close(self) -> None:
        """
        Explicitly closes the underlying file handle.
        """

        self._file.close()

    def _Write(
        self,
        this: ctypes.c_void_p,
        data: ctypes.c_void_p,
        size: int,
        processed_ptr: ctypes.POINTER(ctypes.c_uint32),  # type: ignore[valid-type]
    ) -> int:
        """
        Implements `ISequentialOutStream::Write`.

        Args:
            this (ctypes.c_void_p): Ignored.
            data (ctypes.c_void_p): Input buffer.
            size (int): Bytes to write.
            processed_ptr: Receives actual byte count.

        Returns:
            int: `S_OK` or error HRESULT.
        """

        try:
            if size and data:
                raw = (ctypes.c_ubyte * size).from_address(data)  # type: ignore[arg-type]
                written: Optional[int] = self._file.write(bytes(raw))
                if processed_ptr:
                    processed_ptr[0] = written if written is not None else 0
            elif processed_ptr:
                processed_ptr[0] = 0
            return S_OK
        except Exception:
            return 0x80004005

    def _Seek(
        self,
        this: ctypes.c_void_p,
        offset: int,
        origin: int,
        new_pos_ptr: ctypes.POINTER(ctypes.c_uint64),  # type: ignore[valid-type]
    ) -> int:
        """
        Implements `IOutStream::Seek`.

        Args:
            this (ctypes.c_void_p): Ignored.
            offset (int): Seek offset.
            origin (int): Seek origin.
            new_pos_ptr: Receives new position.

        Returns:
            int: `S_OK` or error HRESULT.
        """

        whence_map: dict[int, int] = {0: 0, 1: 1, 2: 2}
        whence: Optional[int] = whence_map.get(origin)
        if whence is None:
            return 0x80030001
        try:
            pos: int = self._file.seek(offset, whence)
            if new_pos_ptr:
                new_pos_ptr[0] = pos
            return S_OK
        except Exception:
            return 0x80030001

    def _SetSize(self, this: ctypes.c_void_p, new_size: int) -> int:
        """
        Implements `IOutStream::SetSize` via `os.truncate`.

        Args:
            this (ctypes.c_void_p): Ignored.
            new_size (int): New file size in bytes.

        Returns:
            int: `S_OK` or error HRESULT.
        """

        try:
            self._file.truncate(new_size)
            return S_OK
        except Exception:
            return 0x80004005
