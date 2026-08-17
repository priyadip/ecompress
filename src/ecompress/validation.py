"""Output validation.

No file is ever reported as a success before it has been re-opened and parsed
by an independent reader:

* images  - Pillow (``verify()`` then a full ``load()``)
* video   - ffprobe (streams present, duration preserved)
* audio   - ffprobe (audio stream present, duration preserved)
* PDF     - pikepdf (opens, page count preserved, every page parses)

This is what makes truncation, byte-stripping or container corruption
impossible to pass off as compression.
"""

from __future__ import annotations

from pathlib import Path

from ecompress.errors import MissingDependencyError, ToolExecutionError
from ecompress.ffmpeg import probe
from ecompress.result import MediaType

__all__ = ["ValidationReport", "validate_output"]

#: Encoders may trim or pad the tail by a little; anything beyond this is a bug.
DURATION_TOLERANCE_SECONDS = 1.0
DURATION_TOLERANCE_RATIO = 0.05


class ValidationReport:
    """Result of validating one candidate file."""

    __slots__ = ("reason", "valid")

    def __init__(self, valid: bool, reason: str = "") -> None:
        self.valid = valid
        self.reason = reason

    def __bool__(self) -> bool:
        return self.valid

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        state = "valid" if self.valid else f"invalid: {self.reason}"
        return f"<ValidationReport {state}>"


_OK = ValidationReport(True)


def validate_output(
    path: Path,
    media_type: MediaType,
    *,
    expected_duration: float | None = None,
    expected_pages: int | None = None,
) -> ValidationReport:
    """Check that ``path`` is a genuinely readable file of the expected kind."""
    if not path.exists():
        return ValidationReport(False, "file was not created")
    if not path.is_file():
        return ValidationReport(False, "output is not a regular file")

    try:
        size = path.stat().st_size
    except OSError as exc:
        return ValidationReport(False, f"could not stat output: {exc}")
    if size <= 0:
        return ValidationReport(False, "file is empty")

    if media_type is MediaType.IMAGE:
        return _validate_image(path)
    if media_type is MediaType.PDF:
        return _validate_pdf(path, expected_pages)
    return _validate_av(path, media_type, expected_duration)


def _validate_image(path: Path) -> ValidationReport:
    from PIL import Image, ImageFile, UnidentifiedImageError

    # Truncation tolerance is process-global in Pillow and must be off here:
    # a half-written file has to fail, or "compression" could be faked by
    # simply chopping bytes off the end.
    previous = ImageFile.LOAD_TRUNCATED_IMAGES
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    try:
        try:
            with Image.open(path) as image:
                image.verify()
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
            return ValidationReport(False, f"not a readable image ({exc})")

        # verify() leaves the file unusable, so decode the pixels in a second pass.
        try:
            with Image.open(path) as image:
                image.load()
                if image.size[0] <= 0 or image.size[1] <= 0:
                    return ValidationReport(False, "image has zero dimensions")
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
            return ValidationReport(False, f"image pixels could not be decoded ({exc})")
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = previous
    return _OK


def _validate_pdf(path: Path, expected_pages: int | None) -> ValidationReport:
    try:
        import pikepdf
    except ImportError:  # pragma: no cover - pikepdf is a hard dependency
        return ValidationReport(False, "pikepdf is not installed")

    try:
        with pikepdf.open(path) as pdf:
            pages = len(pdf.pages)
            if pages == 0:
                return ValidationReport(False, "PDF has no pages")
            for page in pdf.pages:
                # Touching the resources forces the page object to be parsed.
                _ = page.obj.get("/Resources")
    except Exception as exc:
        return ValidationReport(False, f"PDF could not be opened ({exc})")

    if expected_pages is not None and pages != expected_pages:
        return ValidationReport(
            False, f"page count changed: expected {expected_pages}, got {pages}"
        )
    return _OK


def _validate_av(
    path: Path, media_type: MediaType, expected_duration: float | None
) -> ValidationReport:
    try:
        info = probe(path)
    except MissingDependencyError:  # pragma: no cover - guarded before we get here
        return ValidationReport(False, "ffprobe is unavailable")
    except ToolExecutionError as exc:
        return ValidationReport(False, f"output is not playable ({_first_line(str(exc))})")

    if media_type is MediaType.VIDEO:
        if not info.has_video:
            return ValidationReport(False, "output contains no video stream")
    elif not info.has_audio:
        return ValidationReport(False, "output contains no audio stream")

    if expected_duration and expected_duration > 0:
        actual = info.duration
        if actual is None:
            return ValidationReport(False, "output has no readable duration")
        tolerance = max(DURATION_TOLERANCE_SECONDS, expected_duration * DURATION_TOLERANCE_RATIO)
        if abs(actual - expected_duration) > tolerance:
            return ValidationReport(
                False,
                f"duration changed: expected ~{expected_duration:.2f}s, got {actual:.2f}s",
            )
    return _OK


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return "unreadable"
