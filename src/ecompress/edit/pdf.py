"""``cut_pdf`` and ``add_pdf``: take pages from PDFs and write them as one PDF.

Pages are copied as PDF objects - text stays text, images are not re-encoded
- so the output is exactly as sharp as the input.
"""

from __future__ import annotations

import re
import time
from contextlib import ExitStack
from typing import TYPE_CHECKING

from ecompress.edit.common import EditResult, check_sources, reserve_output, staged_output
from ecompress.edit.grammar import Command, parse_page_ranges
from ecompress.errors import InputFileError, OutputValidationError
from ecompress.reporting import NullReporter, Reporter
from ecompress.result import MediaType
from ecompress.validation import validate_output

if TYPE_CHECKING:
    from pathlib import Path

    import pikepdf

__all__ = ["run_pdf"]

#: Longer page lists than this are left out of the output name.
_MAX_NAME_SPEC = 40


def run_pdf(
    command: Command, *, overwrite: bool = False, reporter: Reporter | None = None
) -> EditResult:
    """Execute a ``cut_pdf`` or ``add_pdf`` command."""
    import pikepdf

    reporter = reporter or NullReporter()
    started = time.monotonic()
    paths = check_sources(command)

    with ExitStack() as stack:
        opened: dict[Path, pikepdf.Pdf] = {}
        picks: list[tuple[pikepdf.Pdf, list[int]]] = []
        for source, path in zip(command.sources, paths, strict=True):
            key = path.resolve()
            pdf = opened.get(key)
            if pdf is None:
                pdf = stack.enter_context(_open(path))
                opened[key] = pdf
            # Every range is checked before anything is written.
            pages = parse_page_ranges(source.spec, len(pdf.pages), name=path.name)
            which = f"pages [{source.spec.strip()}]" if source.spec is not None else "all pages"
            reporter.step(f"{path.name}: {which} ({len(pages)} of {len(pdf.pages)})")
            picks.append((pdf, pages))

        total = sum(len(pages) for _, pages in picks)
        reserved = reserve_output(
            command,
            paths,
            suffix=_name_suffix(command),
            extension=".pdf",
            writable=frozenset({".pdf"}),
            overwrite=overwrite,
        )
        with staged_output(reserved) as temp:
            with pikepdf.new() as combined:
                for pdf, pages in picks:
                    for index in pages:
                        combined.pages.append(pdf.pages[index])
                combined.save(temp)
            report = validate_output(temp, MediaType.PDF, expected_pages=total)
            if not report:
                raise OutputValidationError(
                    f"The new PDF failed validation ({report.reason}); nothing was written."
                )

    return EditResult(
        operation=command.operation,
        output_path=reserved.path,
        output_size_bytes=reserved.path.stat().st_size,
        sources=tuple(paths),
        elapsed_seconds=time.monotonic() - started,
        pages=total,
    )


def _open(path: Path) -> pikepdf.Pdf:
    import pikepdf

    try:
        return pikepdf.open(path)
    except pikepdf.PasswordError as exc:
        raise InputFileError(f"{path} is password-protected; remove the password first.") from exc
    except pikepdf.PdfError as exc:
        raise InputFileError(f"{path} is not a readable PDF ({exc}).") from exc


def _name_suffix(command: Command) -> str:
    """``_pages_2-5_8-12_20`` for a cut, ``_merged`` for an add."""
    if command.operation == "add_pdf":
        return "_merged"
    spec = command.sources[0].spec or ""
    compact = re.sub(r"\s+", "", spec).replace(",", "_")
    if not compact or len(compact) > _MAX_NAME_SPEC or not re.fullmatch(r"[\w-]+", compact):
        return "_pages"
    return f"_pages_{compact}"
