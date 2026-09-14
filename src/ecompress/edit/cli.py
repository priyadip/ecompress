"""The ``add_pdf``, ``cut_pdf``, ``add_video`` and ``cut_video`` commands::

    cut_pdf "C:/My Documents/book.pdf"[2-5,8-12,20] -> "D:/out"

Shells get in the way of that line in two places, both verified by running
it:

* ``>`` in ``->`` is output redirection in PowerShell, cmd, bash and zsh. The
  program receives a lone ``-`` while its output goes into a file, so a
  trailing ``-`` is caught and answered with the way to type the arrow.
* PowerShell parses ``"text"[2-9]`` as indexing into a string and fails before
  the program starts; ``--%`` makes it pass the rest of the line untouched.
  zsh treats ``[...]`` as a file pattern, which ``noglob`` switches off.

The arguments are joined back into one line before parsing, so it does not
matter how the shell split them or whether it removed the quotes.
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
#: PowerShell's stop-parsing token. PowerShell removes it; ignore it if another
#: shell passes it through literally.
_STOP_PARSING = "--%"

_SUMMARY = {
    "add_pdf": "Merge PDFs, or chosen pages of them, into one PDF.",
    "cut_pdf": "Extract pages from a PDF into a new PDF.",
    "add_video": "Join videos, or chosen time ranges of them, into one video.",
    "cut_video": "Extract time ranges from a video into a new video.",
}

#: ``(arguments, what they do)``; the first one is used in the shell guide.
_EXAMPLES: dict[str, tuple[tuple[str, str], ...]] = {
    "add_pdf": (
        ('"D:/a.pdf"[2-9] "D:/b.pdf"[7-16] -> "D:/out"', "pages 2-9 of a.pdf, then 7-16 of b.pdf"),
        ('"a.pdf" "b.pdf"', "both whole files, saved next to a.pdf"),
        ('"a.pdf"[1-3] "a.pdf"[10-end]', "the same file twice"),
    ),
    "cut_pdf": (
        ('"C:/My Documents/book.pdf"[2-5,8-12,20] -> "D:/out"', "pages 2-5, 8-12 and 20"),
        ('"book.pdf"[7-16]', "saved next to book.pdf"),
        ('"book.pdf"[10-1]', "pages 10 down to 1"),
    ),
    "add_video": (
        (
            '"a.mp4"[00:00-00:30] "b.mp4"[01:10-02:00] -> "D:/out"',
            "0:00-0:30 of a.mp4, then 1:10-2:00 of b.mp4",
        ),
        ('"a.mp4" "b.mp4"', "both whole files, saved next to a.mp4"),
    ),
    "cut_video": (
        ('"movie.mp4"[00:02:10-00:05:30] -> "D:/out"', "from 2:10 to 5:30"),
        ('"movie.mp4"[130-330]', "the same range in seconds, saved next to movie.mp4"),
        ('"movie.mp4"[00:10-00:20,01:00-end]', "two ranges, joined"),
    ),
}

_RANGES = {
    "pdf": "pages: 7 | 2-9 | 7-end | 16-7 (reversed) | 2-5,8-12,20 | all.  Pages start at 1.",
    "video": (
        "times: 130 (seconds) | 02:10 | 00:02:10 | 1:02:03.5\n"
        "ranges: start-end | 01:20-end | several separated by commas"
    ),
}

_FILE_TYPES = {"pdf": ".pdf", "video": ".mp4, .mkv, .mov, .webm or .avi"}


def _shell_forms(operation: str) -> str:
    """How to type the first example in each shell, arrow included."""
    arguments = _EXAMPLES[operation][0][0]
    inputs, _, target = arguments.partition(" -> ")
    return (
        f"  PowerShell   {operation} --% {arguments}\n"
        f'  cmd          {operation} {inputs} "->" {target}\n'
        f"  bash         {operation} {inputs} '->' {target}\n"
        f"  zsh          noglob {operation} {inputs} '->' {target}\n"
    )


def _help(operation: str) -> str:
    kind = operation.split("_", 1)[1]
    examples = "\n".join(
        f"  {operation} {arguments}\n      {meaning}" for arguments, meaning in _EXAMPLES[operation]
    )
    options = """\
  -q, --quiet     only print the output path
  --json          print the result as JSON
  --overwrite     allow -> "file.ext" to replace an existing file"""
    if operation == "cut_video":
        options += (
            "\n  --copy          cut without re-encoding: instant and lossless, but"
            "\n                  the cut snaps to a keyframe (one range only)"
        )
    return f"""\
usage: {operation} "PATH"[RANGE] ... [-> "FOLDER"]

{_SUMMARY[operation]}

examples:
{examples}

{_RANGES[kind]}

-> "FOLDER" is optional. Without it the result is saved next to the first
input under a new name, and an existing file is never replaced. FOLDER is
created if needed; it may also be a file name ending in {_FILE_TYPES[kind]}.

In a shell, the > in -> means "write output to a file", and PowerShell and
zsh read [ ] themselves. Type the command like this:
{_shell_forms(operation)}
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
    words = [arg for arg in args if arg not in _FLAGS and arg != _STOP_PARSING]
    unknown = [word for word in words if word.startswith("--")]
    usage = f"Run  {operation} --help  for examples."
    if unknown:
        err.write(f"Error: unknown option {unknown[0]}\n{usage}\n")
        return EXIT_USAGE
    if not words:
        err.write(f"Error: no command given.\n\n{_help(operation)}")
        return EXIT_USAGE
    if words[-1] == "-":
        err.write(
            f"Error: the output folder never reached {operation}. Your shell read the '>' "
            "in '->' as 'write output to a file' (and may have created a file with the "
            "folder's name). Nothing was done.\n"
            f"Type the arrow so the shell leaves it alone:\n{_shell_forms(operation)}"
        )
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
