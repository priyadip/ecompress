"""Compress any file below a target size in MB, with one simple command.

    compress "video.mp4" 50

The target is a hard ceiling: the produced file is measured on disk and
re-parsed by an independent reader before the run is called a success.

Python usage::

    from compress import compress

    result = compress(r"D:\\Videos\\movie.mp4", 50)
    print(result.output_path)
    print(result.output_size_mb)   # always < 50
"""

from __future__ import annotations

from compress.api import compress
from compress.errors import (
    CompressError,
    InputFileError,
    InvalidTargetError,
    MissingDependencyError,
    OutputValidationError,
    TargetNotAchievableError,
    ToolExecutionError,
    UnsupportedFormatError,
)
from compress.reporting import ConsoleReporter, NullReporter, Reporter
from compress.result import Attempt, CompressionResult, MediaType
from compress.units import BYTES_PER_MB, bytes_to_mb, format_size, mb_to_bytes

__version__ = "1.2.0"

__all__ = [
    "BYTES_PER_MB",
    "Attempt",
    "CompressError",
    "CompressionResult",
    "ConsoleReporter",
    "InputFileError",
    "InvalidTargetError",
    "MediaType",
    "MissingDependencyError",
    "NullReporter",
    "OutputValidationError",
    "Reporter",
    "TargetNotAchievableError",
    "ToolExecutionError",
    "UnsupportedFormatError",
    "__version__",
    "bytes_to_mb",
    "compress",
    "format_size",
    "mb_to_bytes",
]
