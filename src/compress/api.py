"""The public :func:`compress` entry point.

Orchestration order:

1. Validate the input path and the requested target.
2. Short-circuit when the file is already small enough.
3. Detect the media type from content (falling back to the extension).
4. Run the matching backend inside a private scratch directory.
5. Re-validate the winning candidate and move it to its final name.

The returned :class:`~compress.result.CompressionResult` always satisfies
``output_size_bytes < target_size_bytes``. When that cannot be achieved a
:class:`~compress.errors.TargetNotAchievableError` is raised - a result object
is never used to report a missed target.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
from pathlib import Path

from compress.backends.audio import AudioBackend
from compress.backends.base import Backend, BackendOutcome, Job
from compress.backends.image import ImageBackend
from compress.backends.pdf import PdfBackend
from compress.backends.video import VideoBackend
from compress.detect import detect_media_type
from compress.errors import (
    InputFileError,
    InvalidTargetError,
    OutputValidationError,
    TargetNotAchievableError,
)
from compress.naming import ReservedPath, reserve_output_path
from compress.reporting import NullReporter, Reporter
from compress.result import CompressionResult, MediaType
from compress.units import SizeRange, format_size, parse_size_range
from compress.validation import validate_output

__all__ = ["compress"]

#: Aim this far below the ceiling so ordinary variation cannot cross it.
#: 3% of the target, capped at 2 MB - for a 50 MB request we aim at ~48.5 MB.
SAFETY_MARGIN_RATIO = 0.03
SAFETY_MARGIN_CAP = 2_000_000

_BACKENDS = {
    MediaType.IMAGE: ImageBackend,
    MediaType.VIDEO: VideoBackend,
    MediaType.AUDIO: AudioBackend,
    MediaType.PDF: PdfBackend,
}


def compress(
    path: str | Path,
    target_mb: int | float | str | tuple[float, float] | list[float],
    *,
    min_mb: int | float | str | None = None,
    output_path: str | Path | None = None,
    reporter: Reporter | None = None,
    overwrite: bool = False,
    timeout: float | None = None,
) -> CompressionResult:
    """Compress ``path`` so the result is strictly smaller than ``target_mb``.

    Args:
        path: the file to compress. It is never modified or overwritten.
        target_mb: the maximum size of the output in decimal megabytes
            (``1 MB == 1_000_000 bytes``). Fractional values are allowed. A
            range may be given instead - ``"40-50"``, ``"[40,50]"`` or
            ``(40, 50)`` - meaning "below 50 MB but not below 40 MB".
        min_mb: a quality floor, as an alternative to passing a range. The
            search keeps raising quality until the output reaches it, so the
            budget is used rather than undershot. It is a goal, not a hard
            constraint: when even maximum quality lands below it (the source
            is simply small, and inflating it would add bytes without adding
            quality) the result is still returned, with an explanatory note.
        output_path: write here instead of the automatic
            ``<name>_compressed<ext>`` next to the input.
        reporter: receives progress events; defaults to silence.
        overwrite: allow ``output_path`` to replace an existing file.
        timeout: seconds allowed for each individual encoder invocation.

    Returns:
        A :class:`~compress.result.CompressionResult` whose
        ``output_size_bytes`` is strictly below ``target_size_bytes``. When the
        input was already small enough, ``skipped`` is ``True`` and
        ``output_path`` is the untouched original.

    Raises:
        InputFileError: the input is missing, empty, unreadable or a directory.
        InvalidTargetError: ``target_mb`` is not a positive, finite number.
        UnsupportedFormatError: the file type has no backend.
        MissingDependencyError: FFmpeg is needed but not installed.
        TargetNotAchievableError: no valid output could be brought under the
            target.
    """
    reporter = reporter or NullReporter()
    input_path = _validate_input(path)
    size_range = _validate_target(target_mb, min_mb)
    target_bytes = size_range.maximum
    input_size = input_path.stat().st_size

    reporter.step(f"Original size: {format_size(input_size)}")
    if size_range.minimum is None:
        reporter.step(f"Target size:   {format_size(target_bytes)}")
    else:
        reporter.step(
            f"Target size:   {format_size(size_range.minimum)} to {format_size(target_bytes)}"
        )
    reporter.step("")

    # Check the file type before the shortcut below, so an unsupported file is
    # rejected rather than being waved through as "already small enough".
    # ``allow_probe=False`` keeps this step free of any FFmpeg dependency.
    if input_size < target_bytes:
        notes = ["The file is already below the requested target; it was left untouched."]
        if size_range.minimum is not None and input_size < size_range.minimum:
            notes.append(
                f"It is also below the {format_size(size_range.minimum)} minimum. "
                "Padding it out would add bytes without adding quality, so it was "
                "left as it is."
            )
        return CompressionResult(
            input_path=input_path,
            output_path=input_path,
            input_size_bytes=input_size,
            output_size_bytes=input_size,
            target_size_bytes=target_bytes,
            min_size_bytes=size_range.minimum,
            media_type=detect_media_type(input_path, allow_probe=False).media_type,
            attempts=[],
            target_achieved=True,
            skipped=True,
            backend="none",
            notes=notes,
        )

    reporter.step("Detecting media type...")
    detection = detect_media_type(input_path)
    for note in detection.notes:
        reporter.note(note)

    backend: Backend = _BACKENDS[detection.media_type]()
    aim_bytes = _aim_bytes(target_bytes)

    workdir, cleanup_root = _make_workdir(input_path, output_path)
    try:
        job = Job(
            input_path=input_path,
            input_size_bytes=input_size,
            target_bytes=target_bytes,
            aim_bytes=aim_bytes,
            detection=detection,
            workdir=workdir,
            reporter=reporter,
            timeout=timeout,
            min_bytes=size_range.minimum,
        )
        outcome = backend.compress(job)

        if not outcome.achieved or outcome.best_path is None:
            raise TargetNotAchievableError(
                input_path=input_path,
                target_bytes=target_bytes,
                smallest_valid_bytes=outcome.smallest_valid_bytes,
                attempts=len(outcome.attempts),
                detail=outcome.detail,
            )

        final = _finalise(
            input_path=input_path,
            outcome=outcome,
            detection_extension=detection.extension,
            explicit_output=output_path,
            overwrite=overwrite,
            target_bytes=target_bytes,
            media_type=detection.media_type,
        )
        final_size = final.stat().st_size
        notes = list(outcome.notes)
        if size_range.minimum is not None and final_size < size_range.minimum:
            notes.append(
                f"The result came out below the {format_size(size_range.minimum)} "
                "minimum: this is the largest valid output the source and format "
                "can produce, and padding it would add bytes without adding quality."
            )
        return CompressionResult(
            input_path=input_path,
            output_path=final,
            input_size_bytes=input_size,
            output_size_bytes=final_size,
            target_size_bytes=target_bytes,
            min_size_bytes=size_range.minimum,
            media_type=detection.media_type,
            attempts=list(outcome.attempts),
            target_achieved=True,
            skipped=False,
            format_changed=outcome.format_changed,
            backend=backend.name,
            notes=notes,
        )
    finally:
        shutil.rmtree(cleanup_root, ignore_errors=True)


# -- input validation ------------------------------------------------------


def _validate_input(path: str | Path) -> Path:
    if isinstance(path, Path):
        candidate = path
    elif isinstance(path, str):
        if not path.strip():
            raise InputFileError("No input file was given.")
        candidate = Path(path)
    else:
        raise InputFileError(f"Expected a file path, got {type(path).__name__}.")

    candidate = candidate.expanduser()
    try:
        exists = candidate.exists()
    except OSError as exc:
        raise InputFileError(f"Could not access {candidate}: {exc}") from exc

    if not exists:
        raise InputFileError(f"File not found: {candidate}\n\nCheck the path and try again.")
    if candidate.is_dir():
        raise InputFileError(
            f"{candidate} is a folder, not a file.\n\nGive the path to a single file."
        )
    if not candidate.is_file():
        raise InputFileError(f"{candidate} is not a regular file.")

    try:
        size = candidate.stat().st_size
    except OSError as exc:
        raise InputFileError(f"Could not read {candidate}: {exc}") from exc
    if size == 0:
        raise InputFileError(f"{candidate} is empty; there is nothing to compress.")

    try:
        with candidate.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        raise InputFileError(f"No permission to read {candidate}: {exc}") from exc

    return candidate


def _validate_target(target_mb: object, min_mb: object = None) -> SizeRange:
    # bool is an int subclass; reject it explicitly.
    if isinstance(target_mb, bool) or isinstance(min_mb, bool):
        raise InvalidTargetError("The target size must be a number of megabytes, e.g. 50.")
    try:
        return parse_size_range(target_mb, minimum=min_mb)  # type: ignore[arg-type]
    except ValueError as exc:
        raise InvalidTargetError(
            f"{exc}\n\nGive the target as a number of megabytes, for example:\n"
            '  compress "video.mp4" 50        below 50 MB\n'
            '  compress "video.mp4" 40-50     below 50 MB but not under 40 MB'
        ) from exc


def _aim_bytes(target_bytes: int) -> int:
    margin = min(int(target_bytes * SAFETY_MARGIN_RATIO), SAFETY_MARGIN_CAP)
    return max(target_bytes - margin, 1)


# -- workspace and finalisation --------------------------------------------


def _make_workdir(input_path: Path, output_path: str | Path | None) -> tuple[Path, Path]:
    """A private scratch directory, preferably on the destination volume.

    Keeping candidates on the same volume as the final file turns the last step
    into a rename instead of a multi-gigabyte copy.
    """
    preferred = Path(output_path).expanduser().parent if output_path else input_path.parent
    for base in (preferred, None):
        try:
            root = Path(tempfile.mkdtemp(prefix=".compress-", dir=base))
        except (OSError, ValueError):
            continue
        return root, root
    raise InputFileError(  # pragma: no cover - only if even the system temp fails
        "Could not create a temporary working directory."
    )


def _finalise(
    *,
    input_path: Path,
    outcome: BackendOutcome,
    detection_extension: str,
    explicit_output: str | Path | None,
    overwrite: bool,
    target_bytes: int,
    media_type: MediaType,
) -> Path:
    """Re-check the winner, then move it into place under a unique name."""
    best = outcome.best_path
    assert best is not None  # noqa: S101 - guarded by the caller

    extension = outcome.output_extension or detection_extension
    reserved: ReservedPath = reserve_output_path(
        input_path,
        extension=extension,
        explicit=Path(explicit_output) if explicit_output is not None else None,
        overwrite=overwrite,
    )
    try:
        shutil.move(str(best), str(reserved.path))
    except OSError as exc:
        reserved.release()
        raise InputFileError(f"Could not write the output file: {exc}") from exc

    # Final independent check on the file that will actually be handed over.
    final_size = reserved.path.stat().st_size
    report = validate_output(reserved.path, media_type)
    if not report.valid or final_size >= target_bytes or final_size == 0:
        reason = report.reason if not report.valid else f"final size {format_size(final_size)}"
        with contextlib.suppress(OSError):
            reserved.path.unlink()
        raise OutputValidationError(
            "The compressed file failed its final check and was discarded "
            f"({reason}). Nothing was written."
        )
    return reserved.path
