"""
Copyright (c) Modding Forge
"""

from __future__ import annotations

import ctypes
import datetime
from typing import Optional

# VARTYPE constants matching Windows oleauto.h
VT_EMPTY: int = 0
VT_NULL: int = 1
VT_I2: int = 2
VT_I4: int = 3
VT_R4: int = 4
VT_R8: int = 5
VT_BOOL: int = 11
VT_BSTR: int = 8
VT_ERROR: int = 10
VT_UI1: int = 17
VT_UI2: int = 18
VT_UI4: int = 19
VT_UI8: int = 21
VT_I8: int = 20
VT_FILETIME: int = 64

_EPOCH = datetime.datetime(1601, 1, 1, tzinfo=datetime.UTC)
_TICKS_PER_SECOND: int = 10_000_000

_ole32 = ctypes.OleDLL("ole32.dll")
_prop_variant_clear = _ole32.PropVariantClear
_prop_variant_clear.argtypes = [ctypes.c_void_p]
_prop_variant_clear.restype = ctypes.c_long

_oleaut32 = ctypes.OleDLL("oleaut32.dll")
_sys_string_len = _oleaut32.SysStringLen
_sys_string_len.argtypes = [ctypes.c_void_p]
_sys_string_len.restype = ctypes.c_uint


class _PropVariantUnion(ctypes.Union):
    """
    Inner union matching the PROPVARIANT data field layout.
    Contains all value types that are relevant for 7-Zip properties.
    """

    _fields_ = [
        ("boolVal", ctypes.c_int16),       # VT_BOOL  (VARIANT_BOOL)
        ("iVal", ctypes.c_int16),           # VT_I2
        ("lVal", ctypes.c_int32),           # VT_I4
        ("ulVal", ctypes.c_uint32),         # VT_UI4
        ("hVal", ctypes.c_int64),           # VT_I8
        ("uhVal", ctypes.c_uint64),         # VT_UI8
        ("fltVal", ctypes.c_float),         # VT_R4
        ("dblVal", ctypes.c_double),        # VT_R8
        ("bstrVal", ctypes.c_void_p),       # VT_BSTR (owned by OLE)
        ("ptr", ctypes.c_void_p),           # generic pointer slot
        # FILETIME: two DWORDs (low, high)
        ("filetime_low", ctypes.c_uint32),
    ]


class PROPVARIANT(ctypes.Structure):
    """
    Windows PROPVARIANT structure for use with 7-Zip COM interfaces.

    Only the variant types used by 7-Zip archive/item properties are
    modelled.  The layout matches the 16-byte PROPVARIANT defined in
    <propidlbase.h>.
    """

    _fields_ = [
        ("vt", ctypes.c_uint16),
        ("wReserved1", ctypes.c_uint16),
        ("wReserved2", ctypes.c_uint16),
        ("wReserved3", ctypes.c_uint16),
        ("_data", _PropVariantUnion),
    ]

    def clear(self) -> None:
        """
        Resets the variant to VT_EMPTY without calling PropVariantClear.

        For BSTR / IUnknown variants the caller is responsible for
        releasing memory before calling this method.
        """

        self.vt = VT_EMPTY
        self._data.uhVal = 0

    def to_python(
        self,
    ) -> Optional[bool | int | float | str | datetime.datetime]:
        """
        Converts the PROPVARIANT to an equivalent Python value.

        Returns:
            Optional[bool | int | float | str | datetime.datetime]:
                Python representation of the stored value, or `None`
                for VT_EMPTY / VT_NULL.
        """

        vt: int = self.vt
        if vt in (VT_EMPTY, VT_NULL):
            return None
        if vt == VT_BOOL:
            return self._data.boolVal != 0
        if vt == VT_I2:
            return int(self._data.iVal)
        if vt == VT_I4:
            return int(self._data.lVal)
        if vt == VT_I8:
            return int(self._data.hVal)
        if vt == VT_UI1:
            return int(self._data.ulVal & 0xFF)
        if vt == VT_UI2:
            return int(self._data.ulVal & 0xFFFF)
        if vt == VT_UI4:
            return int(self._data.ulVal)
        if vt == VT_UI8:
            return int(self._data.uhVal)
        if vt == VT_R4:
            return float(self._data.fltVal)
        if vt == VT_R8:
            return float(self._data.dblVal)
        if vt == VT_BSTR:
            ptr: Optional[int] = self._data.bstrVal
            if not ptr:
                return ""
            length: int = int(_sys_string_len(ptr))
            return ctypes.wstring_at(ptr, length)
        if vt == VT_FILETIME:
            low: int = self._data.filetime_low
            # high DWORD is stored at offset 12 in the 16-byte structure
            raw: bytes = bytes(self)[8:]
            high: int = int.from_bytes(raw[4:8], "little")
            ticks: int = (high << 32) | low
            if ticks == 0:
                return None
            return _EPOCH + datetime.timedelta(
                microseconds=ticks // 10
            )
        return None

    @classmethod
    def from_python(
        cls, value: bool | int | float | str | None
    ) -> PROPVARIANT:
        """
        Creates a PROPVARIANT from a Python value.

        Args:
            value (bool | int | float | str | None): The value to wrap.

        Returns:
            PROPVARIANT: The filled PROPVARIANT structure.

        Raises:
            TypeError: If the value type is not supported.
        """

        pv = cls()
        pv.clear()
        if value is None:
            pv.vt = VT_EMPTY
        elif isinstance(value, bool):
            pv.vt = VT_BOOL
            pv._data.boolVal = -1 if value else 0
        elif isinstance(value, int):
            if 0 <= value <= 0xFFFFFFFF:
                pv.vt = VT_UI4
                pv._data.ulVal = value
            else:
                pv.vt = VT_UI8
                pv._data.uhVal = value & 0xFFFFFFFFFFFFFFFF
        elif isinstance(value, float):
            pv.vt = VT_R8
            pv._data.dblVal = value
        elif isinstance(value, str):
            # SysAllocString not used - caller must own the buffer.
            # For write use, allocate via OLE.
            pv.vt = VT_BSTR
            pv._data.bstrVal = ctypes.cast(
                ctypes.c_wchar_p(value),
                ctypes.c_void_p,
            ).value
        else:
            raise TypeError(f"Unsupported PROPVARIANT value type: {type(value)}")
        return pv


def clear_propvariant(pv: PROPVARIANT) -> None:
    """
    Calls the OLE `PropVariantClear` API to release resources held by
    a PROPVARIANT (e.g. BSTR memory).

    Args:
        pv (PROPVARIANT): The variant to clear.
    """

    _prop_variant_clear(ctypes.byref(pv))
