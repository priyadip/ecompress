"""Image backend (Pillow).

Strategy, in order of preference:

1. **Re-encode at full resolution.** Binary-search the encoder's quality knob
   for the highest quality that fits (JPEG, WebP, AVIF).
2. **Lossless first for PNG.** Try a maximum-effort lossless re-deflate, then
   adaptive palette quantisation (still a PNG), before touching resolution.
3. **Downscale.** Walk a resolution ladder, re-running the quality search at
   each step.
4. **Format change, last resort only.** A PNG that cannot reach the target even
   at low resolution falls back to WebP (which keeps alpha). This is reported.

The original is never enlarged: if a candidate ends up bigger than the input it
is still measured honestly, and the orchestrator's "already small enough" check
handles the trivial case before we get here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageFile, UnidentifiedImageError

from compress.backends.base import Backend, Job
from compress.errors import InputFileError
from compress.result import MediaType
from compress.search import SearchOutcome, search_discrete_ladder

__all__ = ["ImageBackend"]

#: Quality ladder, ascending. Values above ~95 cost bytes without visible gain.
QUALITY_LADDER: tuple[int, ...] = (
    10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 82, 85, 88, 90, 92, 95,
)  # fmt: skip

#: Fractions of the original resolution, tried in order.
SCALE_LADDER: tuple[float, ...] = (1.0, 0.75, 0.5, 0.375, 0.25, 0.15, 0.1)

#: Palette sizes for PNG quantisation, ascending in quality.
PALETTE_LADDER: tuple[int, ...] = (8, 16, 32, 64, 96, 128, 192, 256)

MIN_DIMENSION = 16
MAX_ATTEMPTS_PER_LADDER = 8

_JPEG_EXTENSIONS = {".jpg", ".jpeg", ".jpe", ".jfif"}
_LOSSY_SEARCHABLE = {"JPEG", "WEBP", "AVIF"}


class ImageBackend(Backend):
    """Compresses still images with Pillow."""

    name = "image"
    media_type = MediaType.IMAGE

    _source: Image.Image
    _format: str

    def prepare(self, job: Job) -> bool:
        self._source = self._open(job.input_path)
        self._format = _target_format(job.detection.extension, self._source.format)
        job.reporter.step(
            f"Image detected ({self._source.width}x{self._source.height}, "
            f"{self._source.format or self._format})."
        )
        return True

    def run(self, job: Job) -> None:
        source, fmt = self._source, self._format
        try:
            if fmt == "PNG":
                self._compress_png(job, source)
            elif fmt == "GIF":
                self._compress_via_palette(job, source, "GIF", ".gif")
            else:
                self._compress_lossy(job, source, fmt, job.detection.extension)
        finally:
            source.close()

        if not self._outcome.achieved:
            self._outcome.detail = (
                "Even at the lowest quality and smallest sensible resolution the "
                "image stays above the requested size."
            )

    # -- format strategies -------------------------------------------------

    def _compress_lossy(self, job: Job, source: Image.Image, fmt: str, extension: str) -> None:
        """Quality search, then resolution ladder, for JPEG/WebP/AVIF."""
        ext = _normalise_extension(extension, fmt)
        outcome: SearchOutcome[int] = SearchOutcome()

        for scale in SCALE_LADDER:
            frame = _resized(source, scale)
            if frame is None:
                continue
            label = "" if scale == 1.0 else f"{frame.width}x{frame.height}"
            if scale != 1.0:
                self.note(
                    f"Reduced resolution to {frame.width}x{frame.height} to reach the target."
                )

            def encode(quality: int, frame: Image.Image = frame, label: str = label) -> int | None:
                path = job.scratch(f"q{quality}_{frame.width}x{frame.height}{ext}")
                if not _save(frame, path, fmt, quality=quality):
                    return None
                return self.measure(
                    path,
                    parameters={"quality": quality, "width": frame.width, "height": frame.height},
                    label=label,
                    keep_as=f"best{ext}",
                )

            search_discrete_ladder(
                QUALITY_LADDER,
                encode,
                limit=job.target_bytes,
                max_evaluations=MAX_ATTEMPTS_PER_LADDER,
                floor=job.min_bytes,
                outcome=outcome,
            )
            if frame is not source:
                frame.close()
            if outcome.best is not None:
                return
            if min(_scaled_size(source, scale)) <= MIN_DIMENSION:
                break

    def _compress_png(self, job: Job, source: Image.Image) -> None:
        """Lossless -> palette -> downscale -> (last resort) WebP."""
        # 1. Lossless re-deflate at maximum effort.
        lossless = job.scratch("lossless.png")
        if _save(source, lossless, "PNG", optimize=True, compress_level=9):
            size = self.measure(lossless, parameters={"mode": "lossless"}, label="lossless")
            if size is not None and size < job.target_bytes:
                return

        # 2. Adaptive palette quantisation, still a PNG.
        if self._compress_via_palette(job, source, "PNG", ".png"):
            return

        # 3. Downscale, re-quantising at each step.
        for scale in SCALE_LADDER[1:]:
            frame = _resized(source, scale)
            if frame is None:
                continue
            self.note(f"Reduced resolution to {frame.width}x{frame.height} to reach the target.")
            hit = self._compress_via_palette(job, frame, "PNG", ".png")
            frame.close()
            if hit:
                return
            if min(_scaled_size(source, scale)) <= MIN_DIMENSION:
                break

        # 4. Last resort: lossy WebP, which still supports transparency.
        self.note(
            "PNG could not reach the target without unacceptable loss; "
            "switched to WebP (transparency is preserved)."
        )
        self._compress_lossy(job, source, "WEBP", ".webp")
        if self._outcome.achieved:
            self._outcome.format_changed = True

    def _compress_via_palette(self, job: Job, source: Image.Image, fmt: str, ext: str) -> bool:
        """Binary-search the palette size. Returns True when the target is met."""
        base = source.convert("RGBA") if _has_alpha(source) else source.convert("RGB")

        def encode(colors: int) -> int | None:
            try:
                quantised = base.quantize(
                    colors=colors,
                    method=Image.Quantize.MEDIANCUT,
                    dither=Image.Dither.FLOYDSTEINBERG,
                )
            except (ValueError, OSError):
                return None
            path = job.scratch(f"p{colors}_{base.width}x{base.height}{ext}")
            saved = _save(quantised, path, fmt, optimize=True, compress_level=9)
            quantised.close()
            if not saved:
                return None
            return self.measure(
                path,
                parameters={"colors": colors, "width": base.width, "height": base.height},
                label=f"{colors} colours",
                keep_as=f"best{ext}",
            )

        outcome: SearchOutcome[int] = SearchOutcome()
        search_discrete_ladder(
            PALETTE_LADDER,
            encode,
            limit=job.target_bytes,
            max_evaluations=6,
            floor=job.min_bytes,
            outcome=outcome,
        )
        if base is not source:
            base.close()
        return self._outcome.achieved

    # -- helpers -----------------------------------------------------------

    def _open(self, path: Path) -> Image.Image:
        """Read the source, tolerating a damaged tail only as a fallback.

        Pillow's ``LOAD_TRUNCATED_IMAGES`` is process-global, so it is enabled
        for the retry and restored immediately: leaving it on would let a
        truncated *output* pass validation, which is precisely the shortcut
        this package must never take.
        """
        try:
            image = Image.open(path)
        except UnidentifiedImageError as exc:
            raise InputFileError(
                f"{path.name} is not an image this tool can read.\n\n"
                "The file may be corrupt or in an unsupported format."
            ) from exc
        except (OSError, ValueError) as exc:
            raise InputFileError(f"Could not read the image {path.name}: {exc}") from exc

        try:
            image.load()
            return image
        except Image.DecompressionBombError as exc:
            image.close()
            raise InputFileError(
                f"{path.name} is extremely large and was refused as a safety measure.\n\n({exc})"
            ) from exc
        except (OSError, ValueError) as first_error:
            # Closing the failed handle matters: without it the file stays open
            # until the garbage collector runs.
            image.close()
            return self._open_leniently(path, first_error)

    def _open_leniently(self, path: Path, first_error: Exception) -> Image.Image:
        """Second attempt for a source whose tail is damaged."""
        previous = ImageFile.LOAD_TRUNCATED_IMAGES
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        try:
            image = Image.open(path)
        except (OSError, ValueError, UnidentifiedImageError) as exc:
            ImageFile.LOAD_TRUNCATED_IMAGES = previous
            raise InputFileError(f"Could not read the image {path.name}: {first_error}") from exc

        try:
            image.load()
        except (OSError, ValueError) as exc:
            image.close()
            raise InputFileError(f"Could not read the image {path.name}: {first_error}") from exc
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = previous

        self.note(f"{path.name} is damaged; the readable part was compressed.")
        return image


def _target_format(extension: str, source_format: str | None) -> str:
    """Which Pillow format to write, preserving the user's extension."""
    ext = extension.lower()
    if ext in _JPEG_EXTENSIONS:
        return "JPEG"
    if ext == ".png":
        return "PNG"
    if ext == ".webp":
        return "WEBP"
    if ext == ".avif":
        return "AVIF"
    if ext == ".gif":
        return "GIF"
    if ext in {".tif", ".tiff"}:
        return "JPEG"  # TIFF has no useful lossy knob; JPEG is the sane target
    if ext == ".bmp":
        return "JPEG"
    if source_format in _LOSSY_SEARCHABLE:
        return source_format
    return "JPEG"


def _normalise_extension(extension: str, fmt: str) -> str:
    ext = extension.lower()
    if fmt == "JPEG":
        return ext if ext in _JPEG_EXTENSIONS else ".jpg"
    if fmt == "WEBP":
        return ".webp"
    if fmt == "AVIF":
        return ".avif"
    if fmt == "PNG":
        return ".png"
    return ext or ".jpg"


def _has_alpha(image: Image.Image) -> bool:
    return image.mode in {"RGBA", "LA", "PA"} or "transparency" in image.info


def _scaled_size(image: Image.Image, scale: float) -> tuple[int, int]:
    return (max(int(image.width * scale), 1), max(int(image.height * scale), 1))


def _resized(image: Image.Image, scale: float) -> Image.Image | None:
    if scale >= 1.0:
        return image
    width, height = _scaled_size(image, scale)
    if width < MIN_DIMENSION or height < MIN_DIMENSION:
        return None
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _prepare(image: Image.Image, fmt: str) -> Image.Image:
    """Convert to a mode the target format accepts."""
    if fmt == "JPEG":
        if image.mode in {"RGBA", "LA", "PA", "P"}:
            background = Image.new("RGB", image.size, (255, 255, 255))
            rgba = image.convert("RGBA")
            background.paste(rgba, mask=rgba.split()[-1])
            if rgba is not image:
                rgba.close()
            return background
        if image.mode not in {"RGB", "L", "CMYK"}:
            return image.convert("RGB")
        return image
    if fmt in {"WEBP", "AVIF"}:
        if image.mode in {"RGB", "RGBA", "L"}:
            return image
        return image.convert("RGBA" if _has_alpha(image) else "RGB")
    if fmt == "GIF" and image.mode != "P":
        return image.convert("P", palette=Image.Palette.ADAPTIVE)
    return image


def _save(image: Image.Image, path: Path, fmt: str, **options: Any) -> bool:
    """Write ``image`` to ``path``. Returns False if the encoder refused."""
    prepared = _prepare(image, fmt)
    params: dict[str, Any] = {}

    if fmt == "JPEG":
        params.update(
            quality=options.get("quality", 85),
            optimize=True,
            progressive=True,
            subsampling="4:2:0" if options.get("quality", 85) < 90 else "4:4:4",
        )
        _carry_over(image, params, ("icc_profile", "exif"))
    elif fmt == "WEBP":
        params.update(quality=options.get("quality", 80), method=6)
        _carry_over(image, params, ("icc_profile", "exif"))
    elif fmt == "AVIF":
        params.update(quality=options.get("quality", 60))
    elif fmt == "PNG":
        params.update(
            optimize=options.get("optimize", True),
            compress_level=options.get("compress_level", 9),
        )
    elif fmt == "GIF":
        params.update(optimize=True)

    try:
        prepared.save(path, format=fmt, **params)
    except (OSError, ValueError, KeyError) as exc:
        _cleanup(path)
        if "encoder" in str(exc).lower() or isinstance(exc, KeyError):
            return False
        return False
    finally:
        if prepared is not image:
            prepared.close()
    return path.exists() and path.stat().st_size > 0


def _carry_over(image: Image.Image, params: dict[str, Any], keys: tuple[str, ...]) -> None:
    """Preserve colour management and camera metadata when present."""
    for key in keys:
        value = image.info.get(key)
        if value:
            params[key] = value


def _cleanup(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:  # pragma: no cover - best effort
        pass
