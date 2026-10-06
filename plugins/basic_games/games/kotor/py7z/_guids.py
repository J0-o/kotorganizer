"""
Copyright (c) Modding Forge
"""

from __future__ import annotations

import ctypes
import uuid


class GUID(ctypes.Structure):
    """
    Windows GUID structure (16 bytes).

    Matches the Win32 GUID / IID / CLSID layout:
    Data1 (DWORD) + Data2 (WORD) + Data3 (WORD) + Data4 (8 bytes).
    """

    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_uint8 * 8),
    ]

    @classmethod
    def from_str(cls, guid_str: str) -> GUID:
        """
        Creates a GUID from a standard UUID string.

        Args:
            guid_str (str): UUID string in the format
                `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`.

        Returns:
            GUID: The parsed GUID structure.
        """

        u = uuid.UUID(guid_str)
        data4 = (ctypes.c_uint8 * 8)(*u.bytes[8:])
        return cls(
            Data1=u.time_low,
            Data2=u.time_mid,
            Data3=u.time_hi_version,
            Data4=data4,
        )

    def __str__(self) -> str:
        """
        Returns the GUID as a standard UUID string.

        Returns:
            str: UUID string representation.
        """

        d4 = bytes(self.Data4)
        return (
            f"{self.Data1:08X}-{self.Data2:04X}-{self.Data3:04X}"
            f"-{d4[:2].hex().upper()}-{d4[2:].hex().upper()}"
        )


def _g(guid_str: str) -> GUID:
    """
    Shorthand helper to build a GUID from a string literal.

    Args:
        guid_str (str): UUID string.

    Returns:
        GUID: Parsed GUID.
    """

    return GUID.from_str(guid_str)


# IUnknown
IID_IUnknown = _g("00000000-0000-0000-C000-000000000046")

# 7-Zip stream interfaces
IID_IProgress = _g("23170F69-40C1-278A-0000-000000050000")
IID_ISequentialInStream = _g("23170F69-40C1-278A-0000-000300010000")
IID_ISequentialOutStream = _g("23170F69-40C1-278A-0000-000300020000")
IID_IInStream = _g("23170F69-40C1-278A-0000-000300030000")
IID_IOutStream = _g("23170F69-40C1-278A-0000-000300040000")
IID_IStreamGetSize = _g("23170F69-40C1-278A-0000-000300060000")

# 7-Zip archive callback interfaces
IID_IArchiveOpenCallback = _g("23170F69-40C1-278A-0000-000600100000")
IID_IArchiveExtractCallback = _g("23170F69-40C1-278A-0000-000600200000")
IID_IArchiveOpenVolumeCallback = _g("23170F69-40C1-278A-0000-000600300000")
IID_IInArchiveGetStream = _g("23170F69-40C1-278A-0000-000600400000")
IID_IArchiveOpenSetSubArchiveName = _g(
    "23170F69-40C1-278A-0000-000600500000"
)

# 7-Zip archive handler interfaces
IID_IInArchive = _g("23170F69-40C1-278A-0000-000600600000")
IID_IArchiveUpdateCallback = _g("23170F69-40C1-278A-0000-000600800000")
IID_IArchiveUpdateCallback2 = _g("23170F69-40C1-278A-0000-000600820000")
IID_IOutArchive = _g("23170F69-40C1-278A-0000-000600A00000")
IID_ISetProperties = _g("23170F69-40C1-278A-0000-000600030000")

# 7-Zip password callback interface
IID_ICryptoGetTextPassword = _g("23170F69-40C1-278A-0000-000500100000")
IID_ICryptoGetTextPassword2 = _g("23170F69-40C1-278A-0000-000500110000")

# Well-known archive format CLSIDs (ClassID from GetHandlerProperty2)
# GUID format: 23170F69-40C1-278A-0000-01yy00000001
CLSID_7z = _g("23170F69-40C1-278A-1000-000110070000")
CLSID_Zip = _g("23170F69-40C1-278A-1000-000110010000")
CLSID_Tar = _g("23170F69-40C1-278A-1000-000110EE0000")
CLSID_GZip = _g("23170F69-40C1-278A-1000-000110EF0000")
CLSID_BZip2 = _g("23170F69-40C1-278A-1000-000110020000")
CLSID_Rar = _g("23170F69-40C1-278A-1000-000110030000")
CLSID_Rar5 = _g("23170F69-40C1-278A-1000-000110CC0000")
CLSID_Xz = _g("23170F69-40C1-278A-1000-0001100C0000")
CLSID_Lzma = _g("23170F69-40C1-278A-1000-0001100A0000")
CLSID_Lzma86 = _g("23170F69-40C1-278A-1000-0001100B0000")

FORMAT_EXTENSION_MAP: dict[str, GUID] = {
    ".7z": CLSID_7z,
    ".zip": CLSID_Zip,
    ".tar": CLSID_Tar,
    ".gz": CLSID_GZip,
    ".tgz": CLSID_GZip,
    ".bz2": CLSID_BZip2,
    ".tbz2": CLSID_BZip2,
    ".xz": CLSID_Xz,
    ".lzma": CLSID_Lzma,
}
"""Mapping from lowercase file extension to archive format CLSID."""
