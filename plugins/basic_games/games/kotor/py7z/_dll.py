"""
Copyright (c) Modding Forge
"""

from __future__ import annotations

import ctypes
from pathlib import Path
from typing import Optional

from ._com import (
    COMPtr,
    HRESULT_T,
    PVOID,
    com_method,
    check_hr,
)
from ._guids import (
    GUID,
    IID_IInArchive,
    IID_IOutArchive,
)
from .exceptions import DllLoadError

_dll_instance: Optional[ctypes.WinDLL] = None
"""Module-level singleton handle for the loaded 7z.dll."""


def load_dll(path: Optional[Path] = None) -> ctypes.WinDLL:
    """
    Loads 7z.dll and caches it in a module-level singleton.

    The DLL is only loaded once; subsequent calls return the cached
    handle regardless of the *path* argument.

    Args:
        path (Optional[Path]): Path to MO2's 7zip.dll.

    Returns:
        ctypes.WinDLL: The loaded DLL handle.

    Raises:
        DllLoadError: If the DLL cannot be found or loaded.
    """

    global _dll_instance
    if _dll_instance is not None:
        return _dll_instance

    if path is None:
        raise DllLoadError("A path to MO2's dlls\\7zip.dll is required.")
    dll_path = path
    if not dll_path.exists():
        raise DllLoadError(f"7z.dll not found at '{dll_path}'.")

    try:
        dll = ctypes.WinDLL(str(dll_path))
    except OSError as exc:
        raise DllLoadError(f"Failed to load 7z.dll: {exc}") from exc

    _configure_exports(dll)
    _dll_instance = dll
    return dll


def _configure_exports(dll: ctypes.WinDLL) -> None:
    """
    Sets argument and return types for all used 7z.dll exports.

    Args:
        dll (ctypes.WinDLL): The loaded 7z.dll handle.
    """

    # HRESULT CreateObject(REFCLSID, REFIID, LPVOID*)
    dll.CreateObject.restype = HRESULT_T
    dll.CreateObject.argtypes = [
        ctypes.POINTER(GUID),   # REFCLSID
        ctypes.POINTER(GUID),   # REFIID
        ctypes.POINTER(ctypes.c_void_p),  # LPVOID*
    ]

    # HRESULT GetNumberOfFormats(PUINT32)
    dll.GetNumberOfFormats.restype = HRESULT_T
    dll.GetNumberOfFormats.argtypes = [
        ctypes.POINTER(ctypes.c_uint32),
    ]

    # HRESULT GetHandlerProperty2(UINT32, PROPID, LPPROPVARIANT)
    from ._propvariant import PROPVARIANT
    dll.GetHandlerProperty2.restype = HRESULT_T
    dll.GetHandlerProperty2.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(PROPVARIANT),
    ]

    # HRESULT GetNumberOfMethods(PUINT32)
    dll.GetNumberOfMethods.restype = HRESULT_T
    dll.GetNumberOfMethods.argtypes = [
        ctypes.POINTER(ctypes.c_uint32),
    ]

    # HRESULT GetMethodProperty(UINT32, PROPID, LPPROPVARIANT)
    dll.GetMethodProperty.restype = HRESULT_T
    dll.GetMethodProperty.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(PROPVARIANT),
    ]

    # HRESULT CreateDecoder(UINT32, REFIID, LPVOID*)
    dll.CreateDecoder.restype = HRESULT_T
    dll.CreateDecoder.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]

    # HRESULT CreateEncoder(UINT32, REFIID, LPVOID*)
    dll.CreateEncoder.restype = HRESULT_T
    dll.CreateEncoder.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]

    # HRESULT GetHashers(IHashers**)
    dll.GetHashers.restype = HRESULT_T
    dll.GetHashers.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
    ]


# ---------------------------------------------------------------------------
# High-level wrappers for DLL exports
# ---------------------------------------------------------------------------

def create_object(
    clsid: GUID,
    iid: GUID,
    dll_path: Optional[Path] = None,
) -> COMPtr:
    """
    Calls `CreateObject` and returns a `COMPtr` for the new object.

    Args:
        clsid (GUID): The class ID of the archive handler to create.
        iid (GUID): The interface ID to retrieve (e.g. `IID_IInArchive`).
        dll_path (Optional[Path]): Optional path to the 7z.dll.

    Returns:
        COMPtr: Wrapped COM pointer with a `Release` on finalisation.

    Raises:
        HResultError: If `CreateObject` returns a failure HRESULT.
        DllLoadError: If the DLL cannot be loaded.
    """

    dll = load_dll(dll_path)
    out = ctypes.c_void_p(None)
    hr: int = dll.CreateObject(
        ctypes.byref(clsid),
        ctypes.byref(iid),
        ctypes.byref(out),
    )
    check_hr(hr, f"CreateObject({clsid})")
    return COMPtr(out)


def create_in_archive(
    clsid: GUID,
    dll_path: Optional[Path] = None,
) -> COMPtr:
    """
    Creates an `IInArchive` COM object for the given format CLSID.

    Args:
        clsid (GUID): Format class ID (e.g. `CLSID_7z`).
        dll_path (Optional[Path]): Optional path to the 7z.dll.

    Returns:
        COMPtr: `IInArchive` interface pointer.

    Raises:
        HResultError: On creation failure.
        DllLoadError: If the DLL cannot be loaded.
    """

    return create_object(clsid, IID_IInArchive, dll_path)


def create_out_archive(
    clsid: GUID,
    dll_path: Optional[Path] = None,
) -> COMPtr:
    """
    Creates an `IOutArchive` COM object for the given format CLSID.

    Args:
        clsid (GUID): Format class ID (e.g. `CLSID_7z`).
        dll_path (Optional[Path]): Optional path to the 7z.dll.

    Returns:
        COMPtr: `IOutArchive` interface pointer.

    Raises:
        HResultError: On creation failure.
        DllLoadError: If the DLL cannot be loaded.
    """

    return create_object(clsid, IID_IOutArchive, dll_path)


def get_number_of_formats(dll_path: Optional[Path] = None) -> int:
    """
    Returns the total number of archive formats supported by 7z.dll.

    Args:
        dll_path (Optional[Path]): Optional path to the 7z.dll.

    Returns:
        int: Number of formats.

    Raises:
        HResultError: On failure.
        DllLoadError: If the DLL cannot be loaded.
    """

    dll = load_dll(dll_path)
    n = ctypes.c_uint32(0)
    check_hr(dll.GetNumberOfFormats(ctypes.byref(n)), "GetNumberOfFormats")
    return int(n.value)


def get_handler_property(
    index: int,
    prop_id: int,
    dll_path: Optional[Path] = None,
) -> object:
    """
    Returns a single property value from a format handler.

    Args:
        index (int): Zero-based format index.
        prop_id (int): Property ID (see `SevenZipHandlerPropertyType`).
        dll_path (Optional[Path]): Optional path to the 7z.dll.

    Returns:
        object: Python value converted from the PROPVARIANT.

    Raises:
        HResultError: On failure.
        DllLoadError: If the DLL cannot be loaded.
    """

    from ._propvariant import PROPVARIANT, clear_propvariant

    dll = load_dll(dll_path)
    pv = PROPVARIANT()
    hr: int = dll.GetHandlerProperty2(
        ctypes.c_uint32(index),
        ctypes.c_uint32(prop_id),
        ctypes.byref(pv),
    )
    check_hr(hr, f"GetHandlerProperty2(index={index}, prop={prop_id})")
    result = pv.to_python()
    clear_propvariant(pv)
    return result
