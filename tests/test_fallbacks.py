"""Fallback and degraded-input paths.

These are the branches a user only hits on an awkward file: a PNG that will not
shrink far enough as a PNG, a damaged source, a video with no readable
duration. They matter precisely because they are rare, so they are pinned here.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path

import pytest
from PIL import Image

from ecompress import compress
from ecompress.reporting import ConsoleReporter
from ecompress.result import Attempt

from .conftest import requires_webp, requires_x264

Copier = Callable[..., Path]


# -- PNG -> WebP last resort ----------------------------------------------


@requires_webp
def test_png_falls_back_to_webp_when_png_cannot_reach_the_target(
    copy_media: Copier, source_png_alpha: Path
) -> None:
    """A tiny target forces the documented PNG -> WebP switch."""
    result = compress(copy_media(source_png_alpha), 0.0025)  # 2.5 KB

    assert result.output_size_bytes < 2_500
    assert result.output_path.suffix == ".webp"
    assert result.format_changed
    assert any("WebP" in note for note in result.notes)

    with Image.open(result.output_path) as image:
        image.load()
        assert image.format == "WEBP"


@requires_webp
def test_webp_fallback_keeps_transparency(copy_media: Copier, source_png_alpha: Path) -> None:
    result = compress(copy_media(source_png_alpha), 0.0025)

    with Image.open(result.output_path) as image:
        alpha = image.convert("RGBA").getchannel("A")
        assert alpha.getextrema() != (255, 255), "transparency was lost in the fallback"


# -- damaged inputs --------------------------------------------------------


def test_a_damaged_jpeg_is_still_compressed(tmp_path: Path, source_jpg: Path) -> None:
    """A source with a chopped tail should still produce a valid output."""
    damaged = tmp_path / "damaged.jpg"
    data = source_jpg.read_bytes()
    damaged.write_bytes(data[: int(len(data) * 0.85)])

    result = compress(damaged, 0.2)

    assert result.output_size_bytes < 200_000
    with Image.open(result.output_path) as image:
        image.load()  # the *output* must be intact even though the input was not


def test_unreadable_image_gives_a_clear_error(tmp_path: Path) -> None:
    from ecompress.errors import InputFileError

    path = tmp_path / "fake.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20_000)

    with pytest.raises(InputFileError, match=r"not an image this tool can read|Could not read"):
        compress(path, 0.001)


# -- video without a readable duration ------------------------------------


@requires_x264
@pytest.mark.slow
def test_video_without_duration_uses_the_quality_search(
    copy_media: Copier, source_mp4: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bitrate targeting is impossible without a duration; CRF search takes over."""
    from dataclasses import replace

    from ecompress.backends import video as video_module
    from ecompress.ffmpeg import probe as real_probe

    def probe_without_duration(path: Path, **kwargs: object) -> object:
        return replace(real_probe(path), duration=None)

    monkeypatch.setattr(video_module, "probe", probe_without_duration)

    result = compress(copy_media(source_mp4), 0.3)

    assert result.output_size_bytes < 300_000
    assert any("Duration is unknown" in note for note in result.notes)
    accepted = [a for a in result.attempts if a.accepted]
    assert accepted and accepted[-1].parameters.get("crf") is not None


# -- console reporting -----------------------------------------------------


def test_console_reporter_formats_each_event() -> None:
    stream = io.StringIO()
    reporter = ConsoleReporter(stream)

    reporter.step("Video detected.")
    reporter.attempt(Attempt(1, 1_500_000, {}, accepted=False, valid=True, note="3 Mbps"))
    reporter.attempt(Attempt(2, 900_000, {}, accepted=True, valid=True, note="2 Mbps"))
    reporter.attempt(Attempt(3, 40, {}, accepted=False, valid=False, note="file is empty"))
    reporter.note("Container changed.")

    output = stream.getvalue()
    assert "Video detected." in output
    assert "Attempt 1: 1.5 MB [3 Mbps]" in output
    assert "Attempt 2: 900.0 KB [2 Mbps]  <- best so far" in output
    assert "Attempt 3: rejected (file is empty)" in output
    assert "Note: Container changed." in output


def test_console_reporter_defaults_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    ConsoleReporter().step("hello")
    assert "hello" in capsys.readouterr().out


# -- timeouts --------------------------------------------------------------


@requires_x264
@pytest.mark.slow
def test_an_impossible_timeout_fails_without_claiming_success(
    copy_media: Copier, source_mp4: Path
) -> None:
    """A timeout must abort the encode, never produce a bogus "success"."""
    from ecompress.errors import TargetNotAchievableError

    with pytest.raises(TargetNotAchievableError):
        compress(copy_media(source_mp4), 0.25, timeout=0.001)


@requires_x264
@pytest.mark.slow
def test_timeout_leaves_no_partial_output(
    copy_media: Copier, tmp_path: Path, source_mp4: Path
) -> None:
    from ecompress.errors import TargetNotAchievableError

    source = copy_media(source_mp4)
    with pytest.raises(TargetNotAchievableError):
        compress(source, 0.25, timeout=0.001)

    leftovers = [p for p in tmp_path.iterdir() if p != source]
    assert leftovers == [], f"a partial file survived: {leftovers}"
