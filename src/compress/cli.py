"""The ``compress`` command.

Usage is deliberately two arguments::

    compress "PATH" TARGET_MB

Everything else - codec, CRF, bitrate, quality, resolution, container - is
decided by the package.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import IO

from compress import __version__
from compress.api import compress as compress_file
from compress.errors import (
    CompressError,
    InputFileError,
    InvalidTargetError,
    MissingDependencyError,
    TargetNotAchievableError,
    UnsupportedFormatError,
)
from compress.reporting import ConsoleReporter
from compress.result import CompressionResult
from compress.units import format_size, parse_size_range

__all__ = ["build_parser", "main"]

EXIT_OK = 0
EXIT_TARGET_NOT_ACHIEVED = 1
EXIT_USAGE = 2
EXIT_MISSING_DEPENDENCY = 3
EXIT_INTERRUPTED = 130

_EPILOG = """\
examples:
  compress "D:\\Videos\\movie.mp4" 50        compress a video below 50 MB
  compress "movie.mp4" 40-50                below 50 MB, but not under 40 MB
  compress "photo.jpg" 2                    compress an image below 2 MB
  compress "song.wav" 10                    compress audio below 10 MB
  compress "report.pdf" 5                   compress a PDF below 5 MB

A range keeps quality up by using the budget instead of undershooting it.
40-50, [40,50], 40..50 and --min 40 all mean the same thing.

The output is written next to the original as <name>_compressed<ext>.
The original file is never modified. 1 MB = 1,000,000 bytes.

Run  compress --check  to see what is installed on this machine.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="compress",
        description="Compress a file below a target size in MB.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("path", nargs="?", help="the file to compress")
    parser.add_argument(
        "target_mb",
        nargs="?",
        metavar="TARGET_MB",
        help=(
            "maximum size of the result, in MB (1 MB = 1,000,000 bytes). "
            "A range like 40-50 also sets a minimum, so the budget is used "
            "rather than undershot"
        ),
    )
    parser.add_argument(
        "--min",
        dest="min_mb",
        metavar="MB",
        default=None,
        help="minimum size, as an alternative to the 40-50 range form",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report what is installed (FFmpeg, Pillow, pikepdf) and exit",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        default=None,
        help="write here instead of <name>_compressed<ext>",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="allow --output to replace an existing file",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="only print the output path")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="give up on a single encoder run after this long",
    )
    parser.add_argument("--version", action="version", version=f"compress {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``compress`` console script."""
    parser = build_parser()
    args = parser.parse_args(argv)

    out: IO[str] = sys.stdout
    err: IO[str] = sys.stderr

    if args.check:
        return _run_check(out, as_json=args.json)

    if args.path is None or args.target_mb is None:
        parser.error(
            "both a file and a target size are required.\n"
            'Usage: compress "PATH" TARGET_MB   for example:  compress "video.mp4" 50'
        )

    verbose = not (args.quiet or args.json)
    _validate_target_syntax(args.target_mb, args.min_mb, parser)

    if verbose:
        out.write(f"Compressing:\n{Path(args.path).expanduser()}\n\n")
        out.flush()

    reporter = ConsoleReporter(out) if verbose else None

    try:
        result = compress_file(
            args.path,
            args.target_mb,
            min_mb=args.min_mb,
            output_path=args.output,
            reporter=reporter,
            overwrite=args.overwrite,
            timeout=args.timeout,
        )
    except TargetNotAchievableError as exc:
        _write_error(err, exc)
        return EXIT_TARGET_NOT_ACHIEVED
    except MissingDependencyError as exc:
        _write_error(err, exc)
        return EXIT_MISSING_DEPENDENCY
    except (InputFileError, InvalidTargetError, UnsupportedFormatError) as exc:
        _write_error(err, exc)
        return EXIT_USAGE
    except CompressError as exc:
        _write_error(err, exc)
        return EXIT_USAGE
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        err.write("\nCancelled.\n")
        return EXIT_INTERRUPTED

    if args.json:
        out.write(json.dumps(result.to_dict(), indent=2) + "\n")
    elif args.quiet:
        out.write(f"{result.output_path.resolve()}\n")
    else:
        out.write(_summary(result))
    out.flush()
    return EXIT_OK


def _run_check(stream: IO[str], *, as_json: bool = False) -> int:
    """Handle ``compress --check``. Exit status 0 when nothing is missing."""
    from compress.diagnostics import collect, render

    report = collect()
    if as_json:
        stream.write(
            json.dumps(
                {
                    "package_version": report.package_version,
                    "python_version": report.python_version,
                    "platform": report.platform_name,
                    "ffmpeg": str(report.ffmpeg) if report.ffmpeg else None,
                    "ffprobe": str(report.ffprobe) if report.ffprobe else None,
                    "ffmpeg_version": report.ffmpeg_version,
                    "ffmpeg_source": report.ffmpeg_source,
                    "encoders": report.encoders,
                    "pillow_version": report.pillow_version,
                    "image_formats": report.image_formats,
                    "pikepdf_version": report.pikepdf_version,
                    "ghostscript": str(report.ghostscript) if report.ghostscript else None,
                    "problems": report.problems,
                    "ok": report.ok,
                },
                indent=2,
            )
            + "\n"
        )
    else:
        stream.write(render(report))
    stream.flush()
    return EXIT_OK if report.ok else EXIT_MISSING_DEPENDENCY


def _validate_target_syntax(raw: str, minimum: str | None, parser: argparse.ArgumentParser) -> None:
    """Fail with a usage message rather than a traceback on a malformed size."""
    try:
        parse_size_range(raw, minimum=minimum)
    except ValueError as exc:
        parser.error(
            f"{exc}\n"
            'Usage: compress "PATH" TARGET_MB\n'
            '  compress "video.mp4" 50        below 50 MB\n'
            '  compress "video.mp4" 40-50     below 50 MB but not under 40 MB'
        )


def _summary(result: CompressionResult) -> str:
    lines: list[str] = [""]
    if result.skipped:
        lines += [
            "File is already below the requested target.",
            "",
            f"Original: {format_size(result.input_size_bytes)}",
            f"Target:   {format_size(result.target_size_bytes)}",
            "",
            "No compression necessary; the original was left untouched.",
            "",
            "Output:",
            str(result.output_path.resolve()),
            "",
        ]
        return "\n".join(lines)

    lines += [
        "Compression successful.",
        "",
        f"Original:    {format_size(result.input_size_bytes)}",
        f"Compressed:  {format_size(result.output_size_bytes)}",
        f"Saved:       {format_size(result.saved_bytes)}",
        f"Reduction:   {result.reduction_percent:.1f}%",
        "",
    ]
    if result.min_size_bytes is not None and not result.within_requested_range:
        lines += [
            f"Below the requested {format_size(result.min_size_bytes)} minimum "
            "(see the notes above).",
            "",
        ]
    if result.format_changed:
        lines += [
            f"Format changed to '{result.output_path.suffix}' (see the notes above for why).",
            "",
        ]
    lines += ["Output:", str(result.output_path.resolve()), ""]
    return "\n".join(lines)


def _write_error(stream: IO[str], exc: BaseException) -> None:
    stream.write(f"\nError: {exc}\n")
    stream.flush()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
