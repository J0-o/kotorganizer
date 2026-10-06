"""
Copyright (c) Modding Forge
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Optional

_KILO: float = 1024.0
_MEGA: float = 1024.0 * 1024.0
_GIGA: float = 1024.0 * 1024.0 * 1024.0


@dataclass
class ProgressInfo:
    """
    Snapshot of the current archive operation progress.

    An instance is passed to the user-defined progress callback on every
    significant update from `IProgress::SetCompleted`.  The same object
    is reused across calls; do not retain references across invocations.

    The field layout mirrors NanaZip's `CProgressSync` fields as seen
    in `ProgressDialog2.h`.
    """

    total_bytes: int = 0
    """Total uncompressed bytes to process (0 when unknown)."""

    completed_bytes: int = 0
    """Uncompressed bytes processed so far."""

    total_files: int = 0
    """Total number of items to process."""

    completed_files: int = 0
    """Number of items fully processed."""

    current_file: Optional[str] = None
    """Archive-relative path of the item currently being processed."""

    elapsed_seconds: float = 0.0
    """Wall-clock seconds elapsed since the operation started."""

    speed_bytes_per_sec: float = 0.0
    """Average throughput in bytes per second."""

    remaining_seconds: Optional[float] = None
    """Estimated seconds until completion, or `None` if not calculable."""

    percent: float = 0.0
    """Completion percentage in the range `[0.0, 100.0]`."""

    is_compressing: bool = False
    """`True` when creating/updating an archive; `False` when extracting."""

    @property
    def elapsed(self) -> datetime.timedelta:
        """
        Elapsed time as a :class:`datetime.timedelta`.

        Returns:
            datetime.timedelta: Elapsed wall-clock time.
        """

        return datetime.timedelta(seconds=self.elapsed_seconds)

    @property
    def remaining(self) -> Optional[datetime.timedelta]:
        """
        Estimated remaining time, or `None` if not yet calculable.

        Returns:
            Optional[datetime.timedelta]: Remaining time or `None`.
        """

        if self.remaining_seconds is None:
            return None
        return datetime.timedelta(seconds=self.remaining_seconds)

    @property
    def speed_human(self) -> str:
        """
        Human-readable throughput string (e.g. `'12.3 MB/s'`).

        Returns:
            str: Formatted speed string.
        """

        s: float = self.speed_bytes_per_sec
        if s < _KILO:
            return f"{s:.0f} B/s"
        if s < _MEGA:
            return f"{s / _KILO:.1f} KB/s"
        if s < _GIGA:
            return f"{s / _MEGA:.1f} MB/s"
        return f"{s / _GIGA:.1f} GB/s"

    @property
    def summary(self) -> str:
        """
        Single-line human-readable progress summary suitable for logging.

        Returns:
            str: Formatted progress line.
        """

        e: int = int(self.elapsed_seconds)
        elapsed_str: str = f"{e // 60}:{e % 60:02d}"

        rem_str: str = ""
        if self.remaining_seconds is not None:
            r: int = int(self.remaining_seconds)
            rem_str = f"  rem {r // 60}:{r % 60:02d}"

        file_str: str = (
            f"  '{self.current_file}'" if self.current_file else ""
        )
        return (
            f"{self.percent:5.1f}%"
            f"  {self.speed_human}"
            f"  elapsed {elapsed_str}"
            f"{rem_str}"
            f"  [{self.completed_files}/{self.total_files} files]"
            f"{file_str}"
        )
