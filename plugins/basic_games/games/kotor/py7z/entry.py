"""
Copyright (c) Modding Forge
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Property IDs from SevenZipArchivePropertyType
KPID_PATH: int = 3
KPID_NAME: int = 4
KPID_IS_DIR: int = 6
KPID_SIZE: int = 7
KPID_PACK_SIZE: int = 8
KPID_ATTRIB: int = 9
KPID_CTIME: int = 10
KPID_ATIME: int = 11
KPID_MTIME: int = 12
KPID_SOLID: int = 13
KPID_ENCRYPTED: int = 15
KPID_CRC: int = 19
KPID_TYPE: int = 20
KPID_IS_ANTI: int = 21
KPID_METHOD: int = 22
KPID_POSIX_ATTRIB: int = 53
KPID_SYMLINK: int = 54


@dataclass(frozen=True)
class ArchiveEntry:
    """
    Immutable model representing a single entry in an archive as
    reported by `IInArchive::GetProperty`.
    """

    index: int
    """Zero-based index of the entry within the archive."""

    path: str
    """Archive-relative path of the entry (may use / or \\ as separator)."""

    is_directory: bool
    """`True` if the entry is a directory."""

    size: int
    """Uncompressed size in bytes (0 for directories)."""

    packed_size: int
    """Compressed size in bytes (0 when unavailable)."""

    crc: Optional[int]
    """CRC-32 checksum, or `None` if not stored in the archive."""

    attributes: int
    """Windows file attributes bitmask."""

    created_at: Optional[datetime.datetime]
    """File creation timestamp (UTC), or `None` if not stored."""

    accessed_at: Optional[datetime.datetime]
    """File last-access timestamp (UTC), or `None` if not stored."""

    modified_at: Optional[datetime.datetime]
    """File last-modification timestamp (UTC), or `None` if not stored."""

    method: Optional[str]
    """Compression method name (e.g. `'LZMA2'`), or `None`."""

    encrypted: bool
    """`True` if the entry is encrypted."""

    is_anti: bool
    """`True` if the entry is an anti-item (deletion marker)."""

    @property
    def name(self) -> str:
        """
        The file name component of :attr:`path`.

        Returns:
            str: File name without directory prefix.
        """

        return Path(self.path.replace("\\", "/")).name

    @property
    def suffix(self) -> str:
        """
        The lower-cased file extension including the leading dot.

        Returns:
            str: Extension, e.g. `'.txt'`, or `''` for no extension.
        """

        return Path(self.path.replace("\\", "/")).suffix.lower()


@dataclass
class ArchiveEntryInput:
    """
    Mutable model describing a file to add when creating or updating
    an archive via :class:`~py7z.writer.ArchiveWriter`.
    """

    source_path: Optional[Path]
    """
    Absolute path to the source file on disk, or `None` for directories.
    """

    archive_path: str
    """Path inside the archive (using forward slashes)."""

    is_directory: bool = False
    """`True` if the entry represents a directory."""

    size: int = 0
    """Uncompressed size in bytes (filled automatically from `source_path`)."""

    attributes: int = 0x20
    """Windows file attributes (default: FILE_ATTRIBUTE_ARCHIVE)."""

    modified_at: Optional[datetime.datetime] = None
    """Last-modification time to store in the archive."""

    @classmethod
    def from_path(
        cls,
        source: Path,
        base: Optional[Path] = None,
    ) -> "ArchiveEntryInput":
        """
        Creates an `ArchiveEntryInput` from a file path.

        The *archive_path* is computed as the path relative to *base*;
        if *base* is `None` the file name only is used.

        Args:
            source (Path): Absolute path to the file.
            base (Optional[Path]): Root directory for relative path
                calculation.

        Returns:
            ArchiveEntryInput: Populated entry descriptor.
        """

        is_dir: bool = source.is_dir()
        try:
            rel: str = (
                str(source.relative_to(base)).replace("\\", "/")
                if base is not None
                else source.name
            )
        except ValueError:
            rel = source.name

        sz: int = source.stat().st_size if not is_dir else 0
        mtime: Optional[datetime.datetime] = (
            datetime.datetime.fromtimestamp(
                source.stat().st_mtime, tz=datetime.timezone.utc
            )
            if not is_dir
            else None
        )
        return cls(
            source_path=source,
            archive_path=rel,
            is_directory=is_dir,
            size=sz,
            modified_at=mtime,
        )
