"""Cut and combine PDFs and videos with one small command language.

::

    add_pdf   "D:/a.pdf"[2-9] "D:/b.pdf"[7-16] "D:/out"
    cut_pdf   "C:/My Documents/book.pdf"[2-5,8-12,20] "D:/out"
    add_video "a.mp4"[00:00-00:30] "b.mp4"[01:10-02:00] "D:/out"
    cut_video "movie.mp4"[00:02:10-00:05:30]

Python usage::

    from ecompress import execute, cut_pdf, add_video

    execute('cut_pdf "book.pdf"[7-16] "D:/out"')
    cut_pdf("book.pdf", "2-5,8-12,20", output="D:/out")
    add_video([("a.mp4", "00:00-00:30"), ("b.mp4", "01:10-02:00")])
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import TypeAlias

from ecompress.edit.common import EditResult
from ecompress.edit.grammar import OPERATIONS, Command, Source, parse_command
from ecompress.edit.pdf import run_pdf
from ecompress.edit.video import run_video
from ecompress.errors import CommandSyntaxError
from ecompress.reporting import Reporter

__all__ = [
    "OPERATIONS",
    "Command",
    "EditResult",
    "Source",
    "add_pdf",
    "add_video",
    "cut_pdf",
    "cut_video",
    "execute",
    "parse_command",
    "run",
]

PathLike: TypeAlias = "str | os.PathLike[str]"
#: A range: ``"2-9"``, a page number, or ``(start, end)`` in seconds or clock text.
Spec: TypeAlias = "str | int | tuple[float | str, float | str]"
#: An input for ``add_*``: a path (everything) or ``(path, range)``.
Part: TypeAlias = "PathLike | tuple[PathLike, Spec | None]"


def execute(
    command: str,
    *,
    operation: str | None = None,
    overwrite: bool = False,
    copy: bool = False,
    reporter: Reporter | None = None,
    timeout: float | None = None,
) -> EditResult:
    """Parse and run a command such as ``'cut_pdf "book.pdf"[7-16] "D:/out"'``.

    Args:
        command: the command text. The operation word may be left out if
            ``operation`` is given.
        overwrite: allow a file name given as the output to replace a file.
        copy: ``cut_video`` only - cut without re-encoding (instant, lossless,
            keyframe-aligned).
        reporter: receives progress; silent by default.
        timeout: give up on an FFmpeg run after this many seconds.
    """
    parsed = parse_command(command, operation=operation)
    return run(parsed, overwrite=overwrite, copy=copy, reporter=reporter, timeout=timeout)


def run(
    command: Command,
    *,
    overwrite: bool = False,
    copy: bool = False,
    reporter: Reporter | None = None,
    timeout: float | None = None,
) -> EditResult:
    """Run an already-parsed :class:`Command`."""
    if command.kind == "pdf":
        if copy:
            raise CommandSyntaxError("--copy only applies to cut_video.")
        return run_pdf(command, overwrite=overwrite, reporter=reporter)
    return run_video(command, overwrite=overwrite, copy=copy, reporter=reporter, timeout=timeout)


def cut_pdf(
    path: PathLike,
    pages: str | int,
    *,
    output: PathLike | None = None,
    overwrite: bool = False,
    reporter: Reporter | None = None,
) -> EditResult:
    """Extract ``pages`` (e.g. ``"7-16"`` or ``"2-5,8-12,20"``) into a new PDF."""
    command = Command("cut_pdf", (Source(Path(path), str(pages)),), _optional_path(output))
    return run_pdf(command, overwrite=overwrite, reporter=reporter)


def add_pdf(
    parts: Sequence[Part],
    *,
    output: PathLike | None = None,
    overwrite: bool = False,
    reporter: Reporter | None = None,
) -> EditResult:
    """Merge PDFs, or chosen pages of them, in order."""
    command = Command("add_pdf", _sources(parts), _optional_path(output))
    return run_pdf(command, overwrite=overwrite, reporter=reporter)


def cut_video(
    path: PathLike,
    ranges: str | tuple[float | str, float | str],
    *,
    output: PathLike | None = None,
    overwrite: bool = False,
    copy: bool = False,
    reporter: Reporter | None = None,
    timeout: float | None = None,
) -> EditResult:
    """Extract ``ranges`` (``"00:02:10-00:05:30"``, ``"130-330"`` or ``(130, 330)``)."""
    command = Command("cut_video", (Source(Path(path), _spec(ranges)),), _optional_path(output))
    return run_video(command, overwrite=overwrite, copy=copy, reporter=reporter, timeout=timeout)


def add_video(
    parts: Sequence[Part],
    *,
    output: PathLike | None = None,
    overwrite: bool = False,
    reporter: Reporter | None = None,
    timeout: float | None = None,
) -> EditResult:
    """Join videos, or chosen time ranges of them, in order."""
    command = Command("add_video", _sources(parts), _optional_path(output))
    return run_video(command, overwrite=overwrite, reporter=reporter, timeout=timeout)


def _sources(parts: Sequence[Part]) -> tuple[Source, ...]:
    sources: list[Source] = []
    for part in parts:
        if isinstance(part, tuple):
            path, spec = part
            sources.append(Source(Path(path), _spec(spec)))
        else:
            sources.append(Source(Path(part)))
    return tuple(sources)


def _spec(value: Spec | None) -> str | None:
    """``(130, 330)`` becomes ``"130-330"``; text and page numbers pass through."""
    if value is None:
        return None
    if isinstance(value, tuple):
        return f"{value[0]}-{value[1]}"
    return str(value)


def _optional_path(value: PathLike | None) -> Path | None:
    return None if value is None else Path(value).expanduser()
