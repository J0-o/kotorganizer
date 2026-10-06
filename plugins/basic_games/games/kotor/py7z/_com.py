"""
Copyright (c) Modding Forge
"""

from __future__ import annotations

import ctypes
from typing import Any, Optional

from ._guids import GUID

# ---------------------------------------------------------------------------
# Win32 HRESULT helpers
# ---------------------------------------------------------------------------

S_OK: int = 0x00000000
S_FALSE: int = 0x00000001
E_NOINTERFACE: int = 0x80004002
E_NOTIMPL: int = 0x80004001
E_ABORT: int = 0x80004004
E_FAIL: int = 0x80004005
E_OUTOFMEMORY: int = 0x8007000E
E_INVALIDARG: int = 0x80070057
STG_E_INVALIDFUNCTION: int = 0x80030001


def succeeded(hr: int) -> bool:
    """
    Returns `True` when an HRESULT indicates success.

    Args:
        hr (int): HRESULT value (unsigned 32-bit integer).

    Returns:
        bool: `True` if `hr >= 0`, `False` otherwise.
    """

    return (hr & 0x80000000) == 0


def check_hr(hr: int, msg: str = "") -> None:
    """
    Raises `HResultError` when the HRESULT is a failure code.

    Args:
        hr (int): HRESULT value.
        msg (str): Optional context string for the error message.

    Raises:
        HResultError: If `hr` indicates failure.
    """

    from .exceptions import HResultError

    hr_u: int = hr & 0xFFFFFFFF
    if not succeeded(hr_u):
        prefix: str = f"{msg}: " if msg else ""
        raise HResultError(hr_u, f"{prefix}HRESULT 0x{hr_u:08X}")


# ---------------------------------------------------------------------------
# COM vtable function-pointer type factories
# ---------------------------------------------------------------------------

# Calling convention for all COM methods on Windows x64 is __stdcall on
# x86 / WINFUNCTYPE on x64 (they are identical on x64, but WINFUNCTYPE is
# correct regardless).
_WINFUNCTYPE = ctypes.WINFUNCTYPE


def com_method(
    restype: Any,
    *argtypes: Any,
) -> Any:
    """
    Creates a WINFUNCTYPE for a COM vtable slot.

    Args:
        restype: Return type (usually `ctypes.HRESULT` or `ctypes.c_ulong`).
        *argtypes: Argument types in order (`this` pointer first).

    Returns:
        type[ctypes._FuncPtr]: The WINFUNCTYPE function pointer type.
    """

    return _WINFUNCTYPE(restype, *argtypes)


# Shared base types for vtable slots
HRESULT_T = ctypes.c_long  # HRESULT as signed long
_ULONG = ctypes.c_ulong   # ULONG / AddRef / Release return
PVOID = ctypes.c_void_p
_PUINT32 = ctypes.POINTER(ctypes.c_uint32)
_PUINT64 = ctypes.POINTER(ctypes.c_uint64)
_LPVOID_P = ctypes.POINTER(ctypes.c_void_p)

# IUnknown vtable slot signatures
_QI_FUNC = com_method(HRESULT_T, PVOID, ctypes.POINTER(GUID), _LPVOID_P)
_ADDREF_FUNC = com_method(_ULONG, PVOID)
_RELEASE_FUNC = com_method(_ULONG, PVOID)


# ---------------------------------------------------------------------------
# COMPtr - thin wrapper around a raw COM pointer returned by 7z.dll
# ---------------------------------------------------------------------------

class COMPtr:
    """
    Reference-counted wrapper around a raw COM object pointer.

    Calls `AddRef` on construction and `Release` on destruction.
    Vtable calling is performed by using the vtable offset helpers.

    The vtable layout is a contiguous array of `void*` starting with
    the three IUnknown slots (QueryInterface=0, AddRef=1, Release=2).
    """

    _ptr: ctypes.c_void_p
    _vtable: ctypes.Array[ctypes.c_void_p]

    def __init__(self, raw: ctypes.c_void_p) -> None:
        """
        Wraps a raw COM pointer and calls AddRef.

        Args:
            raw (ctypes.c_void_p): Non-null pointer to a COM object.

        Raises:
            ValueError: If *raw* is NULL.
        """

        if not raw:
            raise ValueError("Cannot wrap a NULL COM pointer.")
        self._ptr = raw
        # Resolve vtable: *ptr → vtable pointer → array of function ptrs
        obj_pp = ctypes.cast(raw, ctypes.POINTER(ctypes.c_void_p))
        vtbl_ptr = obj_pp[0]
        vtbl_as_pp = ctypes.cast(vtbl_ptr, ctypes.POINTER(ctypes.c_void_p))
        # Keep a reference to the vtable as an array (arbitrary max 64)
        self._vtable = (ctypes.c_void_p * 64).from_address(vtbl_ptr)
        self._call_vtable(1, _ADDREF_FUNC)  # AddRef

    def __del__(self) -> None:
        """
        Releases the COM object when the Python wrapper is garbage-collected.
        """

        try:
            self._call_vtable(2, _RELEASE_FUNC)
        except Exception:
            pass

    def _call_vtable(
        self, index: int, func_type: Any, *args: Any
    ) -> Any:
        """
        Calls the COM vtable method at the given zero-based index.

        Args:
            index (int): Zero-based vtable slot index.
            func_type: The ctypes WINFUNCTYPE for this slot.
            *args: Arguments forwarded to the function (after *this*).

        Returns:
            Any: The raw return value of the COM method.
        """

        func_ptr = func_type(self._vtable[index])
        return func_ptr(self._ptr, *args)

    def query_interface(self, iid: GUID) -> "COMPtr":
        """
        Calls `IUnknown::QueryInterface` and returns a new `COMPtr`.

        Args:
            iid (GUID): The interface ID to query for.

        Returns:
            COMPtr: Wrapper for the queried interface.

        Raises:
            HResultError: If the query fails.
        """

        out = ctypes.c_void_p(None)
        qi_fn = _WINFUNCTYPE(
            HRESULT_T, PVOID, ctypes.POINTER(GUID), _LPVOID_P
        )
        hr: int = qi_fn(self._vtable[0])(
            self._ptr, ctypes.byref(iid), ctypes.byref(out)
        )
        check_hr(hr, "QueryInterface")
        return COMPtr(out)

    @property
    def raw(self) -> ctypes.c_void_p:
        """
        The underlying raw COM pointer value.

        Returns:
            ctypes.c_void_p: The raw pointer.
        """

        return self._ptr

    @property
    def vtable(self) -> ctypes.Array[ctypes.c_void_p]:
        """
        The raw vtable pointer array for this COM object.

        Returns:
            ctypes.Array[ctypes.c_void_p]: The vtable array.
        """

        return self._vtable


# ---------------------------------------------------------------------------
# PyCOMObject - base class for Python-implemented COM objects (callbacks)
# ---------------------------------------------------------------------------

class PyCOMObject:
    """
    Base class for Python-side COM object implementations (callbacks).

    Subclasses provide vtable method implementations.  The vtable is
    assembled once per class from the `_vtable_methods` class variable,
    which is a list of `(name, WINFUNCTYPE)` tuples in vtable order
    (starting with QueryInterface/AddRef/Release at indices 0-2 which
    are provided automatically).

    The ctypes structure and vtable are stored as instance attributes to
    prevent garbage collection.

    Usage::

        class MyCallback(PyCOMObject):
            _iids = [IID_IArchiveExtractCallback]
            _vtable_methods = [...]   # without IUnknown prefix

            def _method_get_stream(self, this, ...) -> int:
                ...
    """

    _iids: list[GUID] = []
    _vtable_methods: list[tuple[str, Any]] = []

    # Instance-level storage for ctypes structures
    _vtable_array: Any
    _com_struct: Any
    _func_refs: list[Any]

    def __init__(self) -> None:
        """
        Builds the vtable and COM object structure on construction.
        """

        self._ref_count: int = 1
        self._func_refs = []
        self._build_vtable()

    def _build_vtable(self) -> None:
        """
        Assembles the vtable array and the COM object structure in memory.
        """

        def _qi(this: Any, riid_ptr: Any, ppv: Any) -> int:
            riid = GUID.from_address(riid_ptr)  # type: ignore[attr-defined]
            for iid in self._iids:
                if bytes(riid) == bytes(iid):
                    val = ctypes.cast(
                        ctypes.pointer(self._com_struct), ctypes.c_void_p
                    )
                    ctypes.cast(ppv, ctypes.POINTER(ctypes.c_void_p))[0] = (
                        val.value
                    )
                    self._ref_count += 1
                    return S_OK
            # Always support IUnknown
            from ._guids import IID_IUnknown
            if bytes(riid) == bytes(IID_IUnknown):
                val = ctypes.cast(
                    ctypes.pointer(self._com_struct), ctypes.c_void_p
                )
                ctypes.cast(ppv, ctypes.POINTER(ctypes.c_void_p))[0] = (
                    val.value
                )
                self._ref_count += 1
                return S_OK
            ctypes.cast(ppv, ctypes.POINTER(ctypes.c_void_p))[0] = None  # type: ignore[assignment]
            return E_NOINTERFACE

        def _addref(this: Any) -> int:
            self._ref_count += 1
            return self._ref_count

        def _release(this: Any) -> int:
            self._ref_count -= 1
            return max(0, self._ref_count)

        iunknown_slots: list[tuple[str, Any]] = [
            ("QueryInterface", _WINFUNCTYPE(HRESULT_T, PVOID, PVOID, PVOID)),
            ("AddRef", _ADDREF_FUNC),
            ("Release", _RELEASE_FUNC),
        ]
        all_slots = iunknown_slots + self._vtable_methods
        n: int = len(all_slots)

        vtable_type = ctypes.c_void_p * n
        vtable = vtable_type()

        slot_callbacks = [_qi, _addref, _release]
        for name, _ in self._vtable_methods:
            slot_callbacks.append(getattr(self, f"_{name}"))

        for i, ((_name, ftype), cb) in enumerate(
            zip(all_slots, slot_callbacks)
        ):
            wrapped = ftype(cb)
            self._func_refs.append(wrapped)
            vtable[i] = ctypes.cast(wrapped, ctypes.c_void_p).value  # type: ignore[assignment]

        self._vtable_array = vtable

        class _COMStruct(ctypes.Structure):
            _fields_ = [("lpVtbl", ctypes.POINTER(vtable_type))]

        struct = _COMStruct()
        struct.lpVtbl = ctypes.pointer(vtable)
        self._com_struct = struct

    @property
    def as_void_p(self) -> ctypes.c_void_p:
        """
        Returns the COM object as a `c_void_p` suitable for passing
        to 7z.dll methods.

        Returns:
            ctypes.c_void_p: Pointer to the COM object structure.
        """

        return ctypes.cast(
            ctypes.pointer(self._com_struct), ctypes.c_void_p
        )
