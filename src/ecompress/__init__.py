"""Compress any file below a target size in MB, with one simple command.

    ecompress "video.mp4" 50

The target is a hard ceiling: the produced file is measured on disk and
re-parsed by an independent reader before the run is called a success.

Python usage::

    from ecompress import compress

    result = compress(r"D:\\Videos\\movie.mp4", 50)
    print(result.output_path)
    print(result.output_size_mb)   # always < 50
"""

from __future__ import annotations

from ecompress.api import compress
from ecompress.errors import (
    CompressError,
    InputFileError,
    InvalidTargetError,
    MissingDependencyError,
    OutputValidationError,
    TargetNotAchievableError,
    ToolExecutionError,
    UnsupportedFormatError,
)
from ecompress.reporting import ConsoleReporter, NullReporter, Reporter
from ecompress.result import Attempt, CompressionResult, MediaType
from ecompress.units import BYTES_PER_MB, bytes_to_mb, format_size, mb_to_bytes

__version__ = "2.0.1"

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
