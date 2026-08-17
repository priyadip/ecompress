"""End-to-end tests that mimic a real user at a real terminal.

Nothing here is mocked. Each test runs the installed ``compress`` entry point
and then inspects the produced file with an *independent* tool - ffprobe,
Pillow or pikepdf - rather than trusting anything the package reported.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from compress.cli import EXIT_OK, main
from compress.ffmpeg import find_ffmpeg_tools

from .conftest import requires_ffmpeg, requires_mp3, requires_reportlab, requires_x264

Copier = Callable[..., Path]
Capsys = pytest.CaptureFixture[str]

pytestmark = pytest.mark.integration


def ffprobe_json(path: Path) -> dict[str, Any]:
    """Probe with ffprobe directly, bypassing the package entirely."""
    ffprobe = find_ffmpeg_tools().ffprobe
    assert ffprobe is not None
    completed = subprocess.run(  # noqa: S603 - fixed argument list, shell=False
        [
            str(ffprobe), "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", "-i", str(path),
        ],
        capture_output=True,
        check=True,
        shell=False,
    )  # fmt: skip
    return cast("dict[str, Any]", json.loads(completed.stdout))


@requires_x264
@pytest.mark.slow
def test_user_compresses_a_video(copy_media: Copier, source_mp4: Path, capsys: Capsys) -> None:
    """compress "test_video.mp4" 0.2 - then check it with ffprobe."""
    source = copy_media(source_mp4, "test_video.mp4")
    original = ffprobe_json(source)
    original_duration = float(original["format"]["duration"])

    exit_code = main([str(source), "0.2"])
    out = capsys.readouterr().out

    assert exit_code == EXIT_OK
    output = source.parent / "test_video_compressed.mp4"

    assert output.exists(), "output was not created"
    assert output.stat().st_size > 0
    assert output.stat().st_size < 200_000, "the hard target was not met"
    assert str(output.resolve()) in out

    probed = ffprobe_json(output)
    streams = probed["streams"]
    assert any(s["codec_type"] == "video" for s in streams), "not playable as video"
    assert abs(float(probed["format"]["duration"]) - original_duration) < 0.5


@requires_mp3
@pytest.mark.slow
def test_user_compresses_audio(copy_media: Copier, source_wav: Path, capsys: Capsys) -> None:
    """compress "test.wav" 0.2"""
    source = copy_media(source_wav, "test.wav")
    original_duration = float(ffprobe_json(source)["format"]["duration"])

    exit_code = main([str(source), "0.2"])
    out = capsys.readouterr().out

    assert exit_code == EXIT_OK
    outputs = list(source.parent.glob("test_compressed.*"))
    assert len(outputs) == 1
    output = outputs[0]

    assert output.stat().st_size < 200_000
    assert str(output.resolve()) in out

    probed = ffprobe_json(output)
    assert any(s["codec_type"] == "audio" for s in probed["streams"])
    assert abs(float(probed["format"]["duration"]) - original_duration) < 0.5


def test_user_compresses_an_image(copy_media: Copier, source_jpg: Path, capsys: Capsys) -> None:
    """compress "test.jpg" 0.3 - then open it with Pillow."""
    from PIL import Image

    source = copy_media(source_jpg, "test.jpg")
    exit_code = main([str(source), "0.3"])
    out = capsys.readouterr().out

    assert exit_code == EXIT_OK
    output = source.parent / "test_compressed.jpg"

    assert output.exists()
    assert output.stat().st_size < 300_000
    assert str(output.resolve()) in out

    with Image.open(output) as image:
        image.verify()
    with Image.open(output) as image:
        image.load()
        assert image.format == "JPEG"
        assert image.size[0] > 0


@requires_reportlab
def test_user_compresses_a_pdf(copy_media: Copier, source_pdf_images: Path, capsys: Capsys) -> None:
    """compress "test.pdf" 0.2 - then open it with pikepdf."""
    import pikepdf

    source = copy_media(source_pdf_images, "test.pdf")
    with pikepdf.open(source) as pdf:
        original_pages = len(pdf.pages)

    exit_code = main([str(source), "0.2"])
    out = capsys.readouterr().out

    assert exit_code == EXIT_OK
    output = source.parent / "test_compressed.pdf"

    assert output.exists()
    assert output.stat().st_size < 200_000
    assert str(output.resolve()) in out

    with pikepdf.open(output) as pdf:
        assert len(pdf.pages) == original_pages
        for page in pdf.pages:
            _ = page.obj.get("/Resources")


# -- the real console script -----------------------------------------------


def _console_script() -> str | None:
    return shutil.which("compress")


@pytest.mark.skipif(_console_script() is None, reason="the compress script is not on PATH")
def test_installed_console_script_works(copy_media: Copier, source_jpg: Path) -> None:
    """Run the actual `compress` executable, exactly as a user would."""
    source = copy_media(source_jpg, "console test.jpg")
    script = _console_script()
    assert script is not None

    completed = subprocess.run(  # noqa: S603 - fixed argument list, shell=False
        [script, str(source), "0.3"],
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )

    assert completed.returncode == 0, completed.stderr
    output = source.parent / "console test_compressed.jpg"
    assert output.exists()
    assert output.stat().st_size < 300_000
    assert str(output.resolve()) in completed.stdout


def test_python_m_compress_works(copy_media: Copier, source_jpg: Path) -> None:
    source = copy_media(source_jpg, "module test.jpg")
    completed = subprocess.run(  # noqa: S603 - fixed argument list, shell=False
        [sys.executable, "-m", "compress", str(source), "0.3", "--quiet"],
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )

    assert completed.returncode == 0, completed.stderr
    output = Path(completed.stdout.strip())
    assert output.exists()
    assert output.stat().st_size < 300_000


@requires_ffmpeg
def test_readme_promise_holds(copy_media: Copier, source_jpg: Path) -> None:
    """The exact snippet the README shows to users."""
    from compress import compress

    result = compress(copy_media(source_jpg), 0.5)

    assert result.output_path.exists()
    assert result.output_size_mb < 0.5
    assert result.output_size_bytes < 500_000
