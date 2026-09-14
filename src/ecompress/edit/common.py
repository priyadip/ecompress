"""Pieces shared by the PDF and video editors: results and output placement."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from ecompress.edit.grammar import Command
from ecompress.errors import InputFileError
from ecompress.naming import ReservedPath, reserve_output_path
from ecompress.units import bytes_to_mb

__all__ = ["EditResult", "check_sources", "reserve_output", "staged_output"]

#: Extensions that clearly name a file. An ``output(...)`` ending in one of
#: these that the operation cannot write is an error, not a folder name.
_FILE_EXTENSIONS = frozenset(
    {
        ".pdf", ".mp4", ".m4v", ".mkv", ".mov", ".webm", ".avi", ".flv", ".wmv",
        ".mpg", ".mpeg", ".ts", ".3gp", ".mp3", ".wav", ".m4a", ".aac", ".flac",
        ".ogg", ".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".txt", ".docx",
    }
)  # fmt: skip


@dataclass(frozen=True)
class EditResult:
    """What an editing command produced."""

    operation: str
    output_path: Path
    output_size_bytes: int
    sources: tuple[Path, ...]
    elapsed_seconds: float
    #: PDF operations: pages in the output.
    pages: int | None = None
    #: Video operations: duration of the output as measured by ffprobe.
    duration_seconds: float | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def output_size_mb(self) -> float:
        return bytes_to_mb(self.output_size_bytes)

    def to_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "output_path": str(self.output_path.resolve()),
            "output_size_bytes": self.output_size_bytes,
            "output_size_mb": round(self.output_size_mb, 6),
            "sources": [str(path) for path in self.sources],
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "pages": self.pages,
            "duration_seconds": self.duration_seconds,
            "notes": list(self.notes),
        }


def check_sources(command: Command) -> list[Path]:
    """Every input must be an existing, non-empty file."""
    paths: list[Path] = []
    for source in command.sources:
        path = source.path
        if not path.exists():
            raise InputFileError(f"File not found: {path}")
        if not path.is_file():
            raise InputFileError(f"Not a file: {path}")
        if path.stat().st_size == 0:
            raise InputFileError(f"File is empty: {path}")
        paths.append(path)
    return paths


def reserve_output(
    command: Command,
    sources: Sequence[Path],
    *,
    suffix: str,
    extension: str,
    writable: frozenset[str],
    overwrite: bool,
) -> ReservedPath:
    """Choose and reserve where the result goes.

    * no ``output(...)``: next to the first input, ``<name><suffix><extension>``
    * ``output("folder")``: inside that folder (created if needed), same name
    * ``output("file.ext")``: exactly that file, if ``.ext`` is in ``writable``

    Existing files are never replaced unless ``overwrite`` is set for an
    explicit file, and an input is never replaced at all.
    """
    first = sources[0]
    target = command.output
    if target is None:
        return reserve_output_path(first, extension=extension, suffix=suffix)

    suffix_lower = target.suffix.lower()
    if not target.is_dir() and suffix_lower in _FILE_EXTENSIONS:
        if suffix_lower not in writable:
            allowed = ", ".join(sorted(writable))
            raise InputFileError(
                f"{command.operation} cannot write '{suffix_lower}' files; use one of: {allowed}."
            )
        resolved = target.resolve()
        if any(path.resolve() == resolved for path in sources):
            raise InputFileError(
                f"Refusing to overwrite an input file: {target}. Choose a different output."
            )
        _make_dir(target.parent)
        return reserve_output_path(first, explicit=target, overwrite=overwrite)

    _make_dir(target)
    return reserve_output_path(target / first.name, extension=extension, suffix=suffix)


def _make_dir(directory: Path) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InputFileError(f"Could not create the output folder {directory}: {exc}") from exc


@contextmanager
def staged_output(reserved: ReservedPath) -> Iterator[Path]:
    """Yield a temporary file beside the output; move it into place on success.

    Work and validation happen on the temporary file, so a failure (or Ctrl+C)
    never leaves a half-written result under the real name - and never damages
    a file that ``overwrite`` was about to replace.
    """
    handle, name = tempfile.mkstemp(
        prefix=".ecompress-", suffix=reserved.path.suffix, dir=reserved.path.parent
    )
    os.close(handle)  # FFmpeg and pikepdf open the file by name
    temp = Path(name)
    try:
        yield temp
        temp.replace(reserved.path)
    except BaseException:
        temp.unlink(missing_ok=True)
        reserved.release()
        raise
