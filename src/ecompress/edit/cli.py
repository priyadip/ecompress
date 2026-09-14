"""The ``add_pdf``, ``cut_pdf``, ``add_video`` and ``cut_video`` commands.

Quote the whole command in a shell - ``(``, ``[`` and ``>`` all mean something
to bash, PowerShell and cmd::

    cut_pdf 'pdf("C:/My Documents/book.pdf")[7-16] -> output("D:/out")'

The arguments are joined back into one string before parsing, so it does not
matter how the shell split them, or whether it stripped the inner quotes.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from typing import IO

from ecompress import __version__
from ecompress.edit import execute
from ecompress.edit.common import EditResult
from ecompress.edit.grammar import format_timestamp
from ecompress.errors import (
    CommandSyntaxError,
    CompressError,
    InputFileError,
    MissingDependencyError,
    UnsupportedFormatError,
)
from ecompress.reporting import ConsoleReporter
from ecompress.units import format_size

__all__ = ["main_add_pdf", "main_add_video", "main_cut_pdf", "main_cut_video"]

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_MISSING_DEPENDENCY = 3
EXIT_INTERRUPTED = 130

_FLAGS = frozenset({"-q", "--quiet", "--json", "--overwrite", "--copy"})

_SUMMARY = {
    "add_pdf": "Merge PDFs, or chosen pages of them, into one PDF.",
    "cut_pdf": "Extract pages from a PDF into a new PDF.",
    "add_video": "Join videos, or chosen time ranges of them, into one video.",
    "cut_video": "Extract time ranges from a video into a new video.",
}

_EXAMPLES = {
    "add_pdf": """\
  add_pdf 'pdf1("a.pdf")[2-9] pdf2("b.pdf")[7-16] -> output("D:/out")'
  add_pdf 'pdf1("a.pdf") pdf2("b.pdf")'                  whole files
  add_pdf 'pdf1("a.pdf")[1-3] pdf2("a.pdf")[10-end]'     one file twice""",
    "cut_pdf": """\
  cut_pdf 'pdf("C:/My Documents/book.pdf")[7-16] -> output("D:/out")'
  cut_pdf 'pdf("book.pdf")[2-5,8-12,20]'
  cut_pdf 'pdf("book.pdf")[10-1]'                        pages in reverse""",
    "add_video": """\
  add_video 'video1("a.mp4")[00:00-00:30] video2("b.mp4")[01:10-02:00] -> output("D:/out")'
  add_video 'video1("a.mp4") video2("b.mp4")'            whole files""",
    "cut_video": """\
  cut_video 'video("movie.mp4")[00:02:10-00:05:30] -> output("D:/out")'
  cut_video 'video("movie.mp4")[130-330]'                seconds
  cut_video 'video("movie.mp4")[00:10-00:20,01:00-end]'  several ranges, joined
  cut_video --copy 'video("movie.mp4")[01:20-05:40]'     no re-encode""",
}

_RANGES = {
    "pdf": "pages: 7 | 2-9 | 7-end | 16-7 (reversed) | 2-5,8-12,20 | all.  Pages start at 1.",
    "video": (
        "times: 130 (seconds) | 02:10 | 00:02:10 | 1:02:03.5\n"
        "ranges: start-end | 01:20-end | several separated by commas"
    ),
}


def _help(operation: str) -> str:
    kind = operation.split("_", 1)[1]
    options = """\
  -q, --quiet     only print the output path
  --json          print the result as JSON
  --overwrite     allow output("file.ext") to replace an existing file"""
    if operation == "cut_video":
        options += (
            "\n  --copy          cut without re-encoding: instant and lossless, but"
            "\n                  the cut snaps to a keyframe (one range only)"
        )
    return f"""\
usage: {operation} '{kind}("PATH")[RANGE] ... -> output("FOLDER")'

{_SUMMARY[operation]}

examples:
{_EXAMPLES[operation]}

{_RANGES[kind]}

Without output(...) the result is written next to the first input. output(...)
may name a folder (created if needed) or a file ending in the right extension.
The originals are never modified.

Quote the whole command: ( [ and > are special characters in every shell.

options:
{options}
"""


def main_add_pdf(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``add_pdf``."""
    return _main("add_pdf", argv)


def main_cut_pdf(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``cut_pdf``."""
    return _main("cut_pdf", argv)


def main_add_video(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``add_video``."""
    return _main("add_video", argv)


def main_cut_video(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``cut_video``."""
    return _main("cut_video", argv)


def _main(operation: str, argv: Sequence[str] | None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    out: IO[str] = sys.stdout
    err: IO[str] = sys.stderr

    if "-h" in args or "--help" in args:
        out.write(_help(operation))
        return EXIT_OK
    if "--version" in args:
        out.write(f"{operation} (ecompress {__version__})\n")
        return EXIT_OK

    flags = {arg for arg in args if arg in _FLAGS}
    words = [arg for arg in args if arg not in _FLAGS]
    unknown = [word for word in words if word.startswith("--")]
    usage = f"Run  {operation} --help  for examples."
    if unknown:
        err.write(f"Error: unknown option {unknown[0]}\n{usage}\n")
        return EXIT_USAGE
    if not words:
        err.write(f"Error: no command given.\n\n{_help(operation)}")
        return EXIT_USAGE
    if "--copy" in flags and operation != "cut_video":
        err.write(f"Error: --copy only applies to cut_video.\n{usage}\n")
        return EXIT_USAGE

    as_json = "--json" in flags
    quiet = "-q" in flags or "--quiet" in flags
    verbose = not (as_json or quiet)

    try:
        result = execute(
            " ".join(words),
            operation=operation,
            overwrite="--overwrite" in flags,
            copy="--copy" in flags,
            reporter=ConsoleReporter(out) if verbose else None,
        )
    except CommandSyntaxError as exc:
        err.write(f"\nError: {exc}\n{usage}\n")
        return EXIT_USAGE
    except (InputFileError, UnsupportedFormatError) as exc:
        err.write(f"\nError: {exc}\n")
        return EXIT_USAGE
    except MissingDependencyError as exc:
        err.write(f"\nError: {exc}\n")
        return EXIT_MISSING_DEPENDENCY
    except CompressError as exc:
        err.write(f"\nError: {exc}\n")
        return EXIT_FAILED
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        err.write("\nCancelled.\n")
        return EXIT_INTERRUPTED

    if as_json:
        out.write(json.dumps(result.to_dict(), indent=2) + "\n")
    elif quiet:
        out.write(f"{result.output_path.resolve()}\n")
    else:
        out.write(_summary(result))
    out.flush()
    return EXIT_OK


def _summary(result: EditResult) -> str:
    lines = ["", "Done.", ""]
    if result.pages is not None:
        lines.append(f"Pages:     {result.pages}")
    if result.duration_seconds is not None:
        lines.append(f"Duration:  {format_timestamp(result.duration_seconds)}")
    lines.append(f"Size:      {format_size(result.output_size_bytes)}")
    lines.append(f"Time:      {result.elapsed_seconds:.1f}s")
    lines += [f"Note: {note}" for note in result.notes]
    lines += ["", "Output:", str(result.output_path.resolve()), ""]
    return "\n".join(lines)
