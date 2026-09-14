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
from ecompress.edit import EditResult, add_pdf, add_video, cut_pdf, cut_video, execute
from ecompress.errors import (
    CommandSyntaxError,
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

__version__ = "2.5.0"

__all__ = [
    "BYTES_PER_MB",
    "Attempt",
    "CommandSyntaxError",
    "CompressError",
    "CompressionResult",
    "ConsoleReporter",
    "EditResult",
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
    "add_pdf",
    "add_video",
    "bytes_to_mb",
    "compress",
    "cut_pdf",
    "cut_video",
    "execute",
    "format_size",
    "mb_to_bytes",
]
