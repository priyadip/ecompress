"""PDF backend (pikepdf, optionally Ghostscript).

A PDF is not a bag of pixels, so this backend never simply "squeezes" it. The
document is inspected first and then reduced in the least destructive order:

1. **Structural optimisation only** (fully lossless): object streams, maximum
   deflate on every content stream, unreferenced resources dropped. Text,
   vectors, fonts, links and metadata are untouched.
2. **Image re-encoding.** Every raster image is decoded, optionally downsampled,
   and re-encoded as JPEG at a searched quality. Images that would *grow*, image
   masks, and anything that cannot be decoded safely are left exactly as they
   were.
3. **Ghostscript**, when installed, as an alternative engine - it can also
   subset fonts and flatten transparency, which pikepdf cannot.

Every candidate is re-opened with pikepdf and its page count checked before it
can be accepted, so a truncated or broken PDF can never be reported as success.
A text-only PDF that simply cannot shrink far enough produces a clear error
rather than a mangled document.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pikepdf
from PIL import Image

from ecompress.backends.base import Backend, Job
from ecompress.errors import InputFileError, ToolExecutionError
from ecompress.process import run_command
from ecompress.result import MediaType
from ecompress.search import SearchOutcome, search_discrete_ladder

__all__ = ["PdfBackend", "find_ghostscript"]

MAX_ATTEMPTS = 8

#: Images smaller than this are not worth re-encoding.
MIN_IMAGE_BYTES = 4_096

#: Never shrink an image below this on either axis.
MIN_IMAGE_DIMENSION = 32

#: Below this share of image bytes, the document is essentially text/vector.
TEXT_HEAVY_THRESHOLD = 0.15


@dataclass(frozen=True)
class _Setting:
    """One point on the quality ladder."""

    quality: int | None
    """JPEG quality for embedded images; ``None`` means lossless-only."""

    scale: float
    """Fraction of each image's original resolution."""

    def describe(self) -> str:
        if self.quality is None:
            return "lossless"
        if self.scale >= 1.0:
            return f"images q{self.quality}"
        return f"images q{self.quality} @ {int(self.scale * 100)}%"


#: Ascending quality: the search starts at the top and backs off as needed.
LADDER: tuple[_Setting, ...] = (
    _Setting(20, 0.30),
    _Setting(25, 0.40),
    _Setting(30, 0.50),
    _Setting(35, 0.60),
    _Setting(45, 0.70),
    _Setting(55, 0.85),
    _Setting(65, 1.00),
    _Setting(75, 1.00),
    _Setting(85, 1.00),
    _Setting(None, 1.00),
)

#: Ghostscript downsample resolutions / JPEG qualities, ascending quality.
GS_LADDER: tuple[tuple[int, int], ...] = (
    (36, 25), (48, 30), (60, 40), (72, 50), (96, 60), (120, 70), (150, 80), (200, 88), (300, 92),
)  # fmt: skip


class PdfBackend(Backend):
    """Compresses PDF documents."""

    name = "pdf"
    media_type = MediaType.PDF

    _stats: _Stats

    def prepare(self, job: Job) -> bool:
        self._stats = _inspect(job.input_path)
        self.expect_pages(self._stats.pages)
        job.reporter.step(
            f"PDF detected ({self._stats.pages} page"
            f"{'s' if self._stats.pages != 1 else ''}, "
            f"{self._stats.image_count} image"
            f"{'s' if self._stats.image_count != 1 else ''})."
        )
        return True

    def run(self, job: Job) -> None:
        stats = self._stats
        image_share = stats.image_bytes / max(job.input_size_bytes, 1)
        text_heavy = image_share < TEXT_HEAVY_THRESHOLD

        outcome: SearchOutcome[_Setting] = SearchOutcome()

        def encode(setting: _Setting) -> int | None:
            return self._build(job, setting)

        # A text-heavy document gains nothing from image settings, so only the
        # lossless pass is worth an attempt.
        ladder = (LADDER[-1],) if (text_heavy and stats.image_count == 0) else LADDER
        search_discrete_ladder(
            ladder,
            encode,
            limit=job.target_bytes,
            max_evaluations=MAX_ATTEMPTS,
            floor=job.min_bytes,
            outcome=outcome,
        )
        if self._outcome.achieved:
            return

        # pikepdf could not get there; try Ghostscript if it is installed.
        ghostscript = find_ghostscript()
        if ghostscript is not None:
            job.reporter.step("Trying Ghostscript for a deeper rebuild...")
            # Without images, Ghostscript's downsampling knobs change nothing;
            # a single rebuild (font subsetting) is all that can help.
            budget = 1 if stats.image_count == 0 else 6
            if self._ghostscript_search(job, ghostscript, max_evaluations=budget):
                self.note("Ghostscript was used to rebuild the document.")
                return

        self._outcome.detail = _failure_detail(
            stats, text_heavy, image_share, ghostscript is not None
        )

    # -- pikepdf path ------------------------------------------------------

    def _build(self, job: Job, setting: _Setting) -> int | None:
        out = job.scratch(f"pdf_{setting.quality or 'lossless'}_{int(setting.scale * 100)}.pdf")
        if out.exists():
            out.unlink()

        try:
            with pikepdf.open(job.input_path) as pdf:
                if setting.quality is not None:
                    changed = _recompress_images(pdf, setting.quality, setting.scale)
                    if changed == 0 and setting.scale >= 1.0:
                        # Identical to the lossless pass; do not waste an attempt.
                        return None
                _strip_bloat(pdf)
                pdf.save(
                    out,
                    compress_streams=True,
                    recompress_flate=True,
                    object_stream_mode=pikepdf.ObjectStreamMode.generate,
                    linearize=False,
                    deterministic_id=True,
                )
        except (pikepdf.PdfError, OSError, ValueError, RuntimeError) as exc:
            job.reporter.note(f"could not rebuild the PDF at {setting.describe()} ({exc})")
            return None

        return self.measure(
            out,
            parameters={"image_quality": setting.quality, "image_scale": setting.scale},
            label=setting.describe(),
            keep_as="best.pdf",
        )

    # -- Ghostscript path --------------------------------------------------

    def _ghostscript_search(self, job: Job, ghostscript: Path, *, max_evaluations: int = 6) -> bool:
        """Run the Ghostscript ladder. Returns True if it produced a winner."""

        def encode(setting: tuple[int, int]) -> int | None:
            resolution, quality = setting
            out = job.scratch(f"gs_{resolution}_{quality}.pdf")
            if out.exists():
                out.unlink()
            args = [
                str(ghostscript),
                "-sDEVICE=pdfwrite",
                "-dCompatibilityLevel=1.7",
                "-dNOPAUSE",
                "-dBATCH",
                "-dQUIET",
                "-dSAFER",
                "-dDetectDuplicateImages=true",
                "-dCompressFonts=true",
                "-dSubsetFonts=true",
                "-dDownsampleColorImages=true",
                "-dDownsampleGrayImages=true",
                "-dDownsampleMonoImages=true",
                "-dColorImageDownsampleType=/Bicubic",
                "-dGrayImageDownsampleType=/Bicubic",
                "-dMonoImageDownsampleType=/Subsample",
                f"-dColorImageResolution={resolution}",
                f"-dGrayImageResolution={resolution}",
                f"-dMonoImageResolution={max(resolution * 2, 72)}",
                f"-dJPEGQ={quality}",
                f"-sOutputFile={os.fspath(out)}",
                os.fspath(job.input_path),
            ]
            try:
                run_command(args, timeout=job.timeout, check=True, tool="ghostscript")
            except ToolExecutionError as exc:
                job.reporter.note(
                    f"Ghostscript rejected these settings ({_first_line(exc.stderr)})"
                )
                return None
            return self.measure(
                out,
                parameters={"engine": "ghostscript", "dpi": resolution, "jpeg_quality": quality},
                label=f"ghostscript {resolution} dpi q{quality}",
                keep_as="best.pdf",
            )

        search_discrete_ladder(
            GS_LADDER,
            encode,
            limit=job.target_bytes,
            max_evaluations=max_evaluations,
            floor=job.min_bytes,
        )
        return self._outcome.best_path is not None


# -- document inspection ---------------------------------------------------


@dataclass(frozen=True)
class _Stats:
    pages: int
    image_count: int
    image_bytes: int


def _inspect(path: Path) -> _Stats:
    """Count pages and measure how much of the file is raster imagery."""
    try:
        with pikepdf.open(path) as pdf:
            pages = len(pdf.pages)
            count = 0
            total = 0
            for obj in pdf.objects:
                stream = _as_image_stream(obj)
                if stream is None:
                    continue
                count += 1
                try:
                    total += len(stream.read_raw_bytes())
                except (pikepdf.PdfError, ValueError, RuntimeError):
                    continue
    except pikepdf.PasswordError as exc:
        raise InputFileError(
            f"{path.name} is password protected.\n\nRemove the password before compressing it."
        ) from exc
    except (pikepdf.PdfError, OSError, RuntimeError) as exc:
        raise InputFileError(
            f"{path.name} is not a PDF this tool can read.\n\nThe file may be corrupt. ({exc})"
        ) from exc
    return _Stats(pages=pages, image_count=count, image_bytes=total)


def _as_image_stream(obj: Any) -> pikepdf.Stream | None:
    """Return ``obj`` when it is a raster image XObject we may touch."""
    if not isinstance(obj, pikepdf.Stream):
        return None
    try:
        if obj.get("/Subtype") != pikepdf.Name("/Image"):
            return None
        # Stencil masks are 1-bit; JPEG would both corrupt and enlarge them.
        if bool(obj.get("/ImageMask", False)):
            return None
    except (pikepdf.PdfError, ValueError, RuntimeError):
        return None
    return obj


def _recompress_images(pdf: pikepdf.Pdf, quality: int, scale: float) -> int:
    """Re-encode raster images in place. Returns how many were replaced."""
    changed = 0
    for obj in pdf.objects:
        stream = _as_image_stream(obj)
        if stream is None:
            continue
        try:
            original = len(stream.read_raw_bytes())
        except (pikepdf.PdfError, ValueError, RuntimeError):
            continue
        if original < MIN_IMAGE_BYTES:
            continue
        if _replace_image(stream, quality, scale, original):
            changed += 1
    return changed


def _replace_image(stream: pikepdf.Stream, quality: int, scale: float, original: int) -> bool:
    """Swap one image's pixel data for a smaller JPEG. Returns True if replaced."""
    try:
        pdf_image = pikepdf.PdfImage(stream)
        pil = pdf_image.as_pil_image()
    except (
        pikepdf.PdfError,
        pikepdf.UnsupportedImageTypeError,
        NotImplementedError,
        ValueError,
        OSError,
        RuntimeError,
        KeyError,
    ):
        return False

    try:
        with pil:
            gray = pil.mode in {"L", "1", "I;16", "I"}
            converted = pil.convert("L" if gray else "RGB")
            try:
                if scale < 1.0:
                    width = max(int(converted.width * scale), MIN_IMAGE_DIMENSION)
                    height = max(int(converted.height * scale), MIN_IMAGE_DIMENSION)
                    if width < converted.width and height < converted.height:
                        resized = converted.resize((width, height), Image.Resampling.LANCZOS)
                        if converted is not pil:
                            converted.close()
                        converted = resized

                buffer = io.BytesIO()
                converted.save(
                    buffer, format="JPEG", quality=quality, optimize=True, progressive=False
                )
                data = buffer.getvalue()
                new_width, new_height = converted.width, converted.height
            finally:
                if converted is not pil:
                    converted.close()
    except (OSError, ValueError, MemoryError):
        return False

    # Never make an image bigger than it already was.
    if len(data) >= original:
        return False

    try:
        stream.write(data, filter=pikepdf.Name("/DCTDecode"))
        stream.ColorSpace = pikepdf.Name("/DeviceGray" if gray else "/DeviceRGB")
        stream.BitsPerComponent = 8
        stream.Width = new_width
        stream.Height = new_height
        for key in ("/Decode", "/DecodeParms", "/Interpolate"):
            if key in stream:
                del stream[key]
    except (pikepdf.PdfError, ValueError, RuntimeError, KeyError):
        return False
    return True


def _strip_bloat(pdf: pikepdf.Pdf) -> None:
    """Drop resources that carry no meaning for the reader."""
    with contextlib.suppress(pikepdf.PdfError, RuntimeError, AttributeError):
        pdf.remove_unreferenced_resources()


# -- Ghostscript discovery -------------------------------------------------


def find_ghostscript() -> Path | None:
    """Locate a Ghostscript executable, or ``None`` when it is not installed."""
    override = os.environ.get("COMPRESS_GHOSTSCRIPT")
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return candidate
        found = shutil.which(override)
        if found:
            return Path(found)

    names = ["gswin64c", "gswin32c", "gs"] if sys.platform == "win32" else ["gs", "gsc"]
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)

    if sys.platform == "win32":
        for root in (r"C:\Program Files\gs", r"C:\Program Files (x86)\gs"):
            base = Path(root)
            if not base.is_dir():
                continue
            try:
                candidates = sorted(base.glob("gs*/bin/gswin*c.exe"), reverse=True)
            except OSError:  # pragma: no cover - permission edge case
                continue
            if candidates:
                return candidates[0]
    return None


def _failure_detail(
    stats: _Stats, text_heavy: bool, image_share: float, had_ghostscript: bool
) -> str:
    lines: list[str] = []
    if stats.image_count == 0:
        lines.append(
            "This PDF contains no raster images, so only lossless structural "
            "optimisation was possible. Text and vector content cannot be "
            "reduced further without destroying the document."
        )
    elif text_heavy:
        lines.append(
            f"Only {image_share * 100:.0f}% of this PDF is raster imagery; the rest is "
            "text, fonts and vector content that cannot be compressed further "
            "without making the document unusable."
        )
    else:
        lines.append(
            "The embedded images were re-encoded down to the lowest quality "
            "this tool will produce and the document still exceeds the "
            "requested size."
        )
    if not had_ghostscript:
        lines.append(
            "Installing Ghostscript may help: it can also subset fonts and flatten transparency."
        )
    return "\n\n".join(lines)


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:160]
    return "unknown error"
