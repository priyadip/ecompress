"""Shared fixtures.

Every media fixture is *generated*, not committed, so the repository stays
small and the tests exercise real encoders on real bytes. Generation happens
once per session and each test gets its own copy in ``tmp_path`` so output
naming never collides between tests.
"""

from __future__ import annotations

import math
import random
import shutil
import struct
import subprocess
import wave
from collections.abc import Callable
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from compress.ffmpeg import find_ffmpeg_tools, first_available_encoder

# -- capability detection --------------------------------------------------


def ffmpeg_available() -> bool:
    return find_ffmpeg_tools().available


def has_encoder(name: str) -> bool:
    return first_available_encoder([name]) is not None


requires_ffmpeg = pytest.mark.skipif(
    not ffmpeg_available(), reason="ffmpeg/ffprobe are not installed"
)
requires_x264 = pytest.mark.skipif(
    not (ffmpeg_available() and has_encoder("libx264")),
    reason="libx264 encoder is unavailable",
)
requires_mp3 = pytest.mark.skipif(
    not (ffmpeg_available() and has_encoder("libmp3lame")),
    reason="libmp3lame encoder is unavailable",
)
requires_opus = pytest.mark.skipif(
    not (ffmpeg_available() and has_encoder("libopus")),
    reason="libopus encoder is unavailable",
)
requires_vp9 = pytest.mark.skipif(
    not (ffmpeg_available() and has_encoder("libvpx-vp9")),
    reason="libvpx-vp9 encoder is unavailable",
)


def _pillow_can_write(fmt: str) -> bool:
    Image.init()  # Image.SAVE is populated lazily; force plugin registration.
    return fmt in Image.SAVE


requires_webp = pytest.mark.skipif(
    not _pillow_can_write("WEBP"), reason="Pillow has no WebP support"
)
requires_avif = pytest.mark.skipif(
    not _pillow_can_write("AVIF"), reason="Pillow has no AVIF support"
)


def _reportlab_available() -> bool:
    try:
        import reportlab  # noqa: F401
    except ImportError:
        return False
    return True


requires_reportlab = pytest.mark.skipif(
    not _reportlab_available(), reason="reportlab is not installed"
)


# -- media generation ------------------------------------------------------


def make_noisy_image(width: int, height: int, seed: int = 7) -> Image.Image:
    """A busy, photo-like image that does not compress to nothing."""
    rnd = random.Random(seed)
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    assert pixels is not None
    for y in range(height):
        for x in range(width):
            base = int(127 + 90 * math.sin(x / 23.0) * math.cos(y / 31.0))
            pixels[x, y] = (
                max(0, min(255, base + rnd.randint(-45, 45))),
                max(0, min(255, int(200 * (x / width)) + rnd.randint(-45, 45))),
                max(0, min(255, int(200 * (y / height)) + rnd.randint(-45, 45))),
            )
    draw = ImageDraw.Draw(image)
    for _ in range(30):
        x0, x1 = sorted((rnd.randint(0, width), rnd.randint(0, width)))
        y0, y1 = sorted((rnd.randint(0, height), rnd.randint(0, height)))
        draw.ellipse(
            [x0, y0, x1, y1],
            outline=(rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255)),
            width=3,
        )
    return image


def make_wav(path: Path, seconds: float = 4.0, rate: int = 44_100, channels: int = 2) -> Path:
    """Noisy PCM audio - real content, not silence (silence compresses to nothing)."""
    rnd = random.Random(3)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = bytearray()
        for i in range(int(rate * seconds)):
            t = i / rate
            for channel in range(channels):
                freq = 440 if channel == 0 else 587
                value = int(9000 * math.sin(2 * math.pi * freq * t) + rnd.randint(-2500, 2500))
                frames += struct.pack("<h", max(-32768, min(32767, value)))
        handle.writeframes(bytes(frames))
    return path


def run_ffmpeg(args: list[str]) -> None:
    ffmpeg = find_ffmpeg_tools().ffmpeg
    assert ffmpeg is not None
    subprocess.run(  # noqa: S603 - fixed argument list, shell=False
        [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error", *args],
        check=True,
        capture_output=True,
        shell=False,
    )


def make_video(
    path: Path,
    *,
    seconds: float = 4.0,
    size: str = "640x480",
    fps: int = 30,
    with_audio: bool = True,
    crf: int = 14,
) -> Path:
    args = ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}:duration={seconds}"]
    if with_audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p"]
    if with_audio:
        args += ["-c:a", "aac", "-b:a", "128k"]
    args.append(str(path))
    run_ffmpeg(args)
    return path


# -- session-scoped source media -------------------------------------------


@pytest.fixture(scope="session")
def media_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("source-media")


@pytest.fixture(scope="session")
def source_jpg(media_root: Path) -> Path:
    path = media_root / "photo.jpg"
    make_noisy_image(1200, 900).save(path, quality=97, subsampling="4:4:4")
    return path


@pytest.fixture(scope="session")
def source_png(media_root: Path) -> Path:
    path = media_root / "photo.png"
    make_noisy_image(900, 700).save(path, compress_level=1)
    return path


@pytest.fixture(scope="session")
def source_png_alpha(media_root: Path) -> Path:
    path = media_root / "alpha.png"
    image = make_noisy_image(600, 500).convert("RGBA")
    alpha = Image.linear_gradient("L").resize(image.size)
    image.putalpha(alpha)
    image.save(path, compress_level=1)
    return path


@pytest.fixture(scope="session")
def source_webp(media_root: Path) -> Path:
    path = media_root / "photo.webp"
    make_noisy_image(1000, 800).save(path, quality=98)
    return path


@pytest.fixture(scope="session")
def source_wav(media_root: Path) -> Path:
    return make_wav(media_root / "sound.wav", seconds=6.0)


@pytest.fixture(scope="session")
def source_mp3(media_root: Path, source_wav: Path) -> Path:
    path = media_root / "sound.mp3"
    run_ffmpeg(["-i", str(source_wav), "-c:a", "libmp3lame", "-b:a", "320k", str(path)])
    return path


@pytest.fixture(scope="session")
def source_m4a(media_root: Path, source_wav: Path) -> Path:
    path = media_root / "sound.m4a"
    run_ffmpeg(["-i", str(source_wav), "-c:a", "aac", "-b:a", "256k", str(path)])
    return path


@pytest.fixture(scope="session")
def source_flac(media_root: Path, source_wav: Path) -> Path:
    path = media_root / "sound.flac"
    run_ffmpeg(["-i", str(source_wav), "-c:a", "flac", str(path)])
    return path


@pytest.fixture(scope="session")
def source_mp4(media_root: Path) -> Path:
    return make_video(media_root / "clip.mp4", seconds=4.0, size="640x480")


@pytest.fixture(scope="session")
def source_mp4_silent(media_root: Path) -> Path:
    return make_video(media_root / "silent.mp4", seconds=3.0, size="480x360", with_audio=False)


@pytest.fixture(scope="session")
def source_mp4_hd(media_root: Path) -> Path:
    return make_video(media_root / "hd.mp4", seconds=3.0, size="1280x720")


@pytest.fixture(scope="session")
def source_mkv(media_root: Path) -> Path:
    return make_video(media_root / "clip.mkv", seconds=3.0, size="480x360")


@pytest.fixture(scope="session")
def source_mov(media_root: Path) -> Path:
    return make_video(media_root / "clip.mov", seconds=3.0, size="480x360")


@pytest.fixture(scope="session")
def source_audio_only_mp4(media_root: Path, source_wav: Path) -> Path:
    """An .mp4 that holds nothing but an audio track."""
    path = media_root / "audio-only.mp4"
    run_ffmpeg(["-i", str(source_wav), "-vn", "-c:a", "aac", "-b:a", "192k", str(path)])
    return path


@pytest.fixture(scope="session")
def source_pdf_images(media_root: Path, source_png: Path) -> Path:
    """An image-heavy PDF."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    path = media_root / "images.pdf"
    pdf = canvas.Canvas(str(path), pagesize=letter)
    reader = ImageReader(str(source_png))
    for page in range(3):
        pdf.drawImage(reader, 40, 280, width=530, height=400)
        pdf.setFont("Helvetica", 12)
        pdf.drawString(40, 220, f"Page {page + 1}: image-heavy document.")
        pdf.showPage()
    pdf.save()
    return path


@pytest.fixture(scope="session")
def source_pdf_text(media_root: Path) -> Path:
    """A text-only PDF that genuinely cannot shrink far."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    path = media_root / "text.pdf"
    pdf = canvas.Canvas(str(path), pagesize=letter)
    for page in range(3):
        pdf.setFont("Helvetica", 11)
        for line in range(50):
            pdf.drawString(40, 740 - line * 14, f"Line {line} of page {page}: plain text.")
        pdf.showPage()
    pdf.save()
    return path


# -- per-test working copies ----------------------------------------------


@pytest.fixture
def copy_media(tmp_path: Path) -> Callable[[Path, str | None], Path]:
    """Copy a session fixture into this test's own directory."""

    def _copy(source: Path, name: str | None = None) -> Path:
        destination = tmp_path / (name or source.name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return destination

    return _copy
