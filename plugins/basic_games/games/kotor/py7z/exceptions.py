"""
Copyright (c) Modding Forge
"""

from __future__ import annotations


class SevenZipError(Exception):
    """
    Base exception for all py7z errors.
    """

    pass


class DllLoadError(SevenZipError):
    """
    Raised when the 7z.dll cannot be loaded.
    """

    pass


class HResultError(SevenZipError):
    """
    Raised when a COM method returns a failing HRESULT.

    Args:
        hresult (int): The HRESULT value returned by the COM method.
        message (str): Human-readable description.
    """

    hresult: int

    def __init__(self, hresult: int, message: str = "") -> None:
        """
        Initialises the error with an HRESULT code and optional message.

        Args:
            hresult (int): The HRESULT error code.
            message (str): Optional human-readable description.
        """

        self.hresult = hresult
        super().__init__(
            message or f"HRESULT 0x{hresult & 0xFFFFFFFF:08X}"
        )


class ArchiveOpenError(SevenZipError):
    """
    Raised when an archive cannot be opened.
    """

    pass


class ArchiveFormatError(SevenZipError):
    """
    Raised when the archive format is unknown or unsupported.
    """

    pass


class PasswordRequiredError(SevenZipError):
    """
    Raised when a password is required but not provided.
    """

    pass


class WrongPasswordError(SevenZipError):
    """
    Raised when the provided password is incorrect.
    """

    pass


class ExtractionError(SevenZipError):
    """
    Raised when extraction of one or more entries fails.

    Args:
        operation_result (int): The operation result code from 7z.
    """

    operation_result: int

    def __init__(self, operation_result: int, message: str = "") -> None:
        """
        Initialises the error with an operation result code.

        Args:
            operation_result (int): The result code from IArchiveExtractCallback.
            message (str): Optional human-readable description.
        """

        self.operation_result = operation_result
        super().__init__(message or f"Extraction failed (result={operation_result})")
