"""Human-friendly exception hierarchy.

Every exception carries a message that is safe to show directly to a
non-technical user; the CLI prints ``str(exc)`` verbatim.
"""

from __future__ import annotations

from pathlib import Path

from ecompress.units import format_size

__all__ = [
    "CompressError",
    "InputFileError",
    "InvalidTargetError",
    "MissingDependencyError",
    "OutputValidationError",
    "TargetNotAchievableError",
    "ToolExecutionError",
    "UnsupportedFormatError",
]


class CompressError(Exception):
    """Base class for every error raised by this package."""


class InputFileError(CompressError):
    """The input path is missing, unreadable, empty or not a file."""


class InvalidTargetError(CompressError):
    """The requested target size is not a usable number."""


class UnsupportedFormatError(CompressError):
    """The file type is not supported by any backend."""


class MissingDependencyError(CompressError):
    """An external tool or optional library is required but unavailable."""

    def __init__(self, tool: str, reason: str) -> None:
        self.tool = tool
        self.reason = reason
        super().__init__(reason)


class ToolExecutionError(CompressError):
    """An external tool exited with a non-zero status."""

    def __init__(self, tool: str, returncode: int, stderr: str) -> None:
        self.tool = tool
        self.returncode = returncode
        self.stderr = stderr
        detail = stderr.strip().splitlines()
        tail = "\n".join(detail[-6:]) if detail else "(no output)"
        super().__init__(f"{tool} failed with exit code {returncode}.\n\n{tail}")


class OutputValidationError(CompressError):
    """A produced file did not survive validation and was discarded."""


class TargetNotAchievableError(CompressError):
    """The target size could not be reached with a valid, usable output."""

    def __init__(
        self,
        *,
        input_path: Path,
        target_bytes: int,
        smallest_valid_bytes: int | None,
        attempts: int,
        detail: str | None = None,
    ) -> None:
        self.input_path = input_path
        self.target_bytes = target_bytes
        self.smallest_valid_bytes = smallest_valid_bytes
        self.attempts = attempts
        self.detail = detail

        lines = ["Target size could not be achieved.", ""]
        lines.append(f"Target:               {format_size(target_bytes)}")
        if smallest_valid_bytes is None:
            lines.append("Smallest valid output: none (no usable output could be produced)")
        else:
            lines.append(f"Smallest valid output: {format_size(smallest_valid_bytes)}")
        lines.append("")
        lines.append(
            detail
            or (
                "The file cannot be reduced further without producing an invalid "
                "or unusable result."
            )
        )
        super().__init__("\n".join(lines))
