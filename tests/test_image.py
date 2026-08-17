"""Image compression against real files."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from PIL import Image

from ecompress import compress
from ecompress.errors import TargetNotAchievableError
from ecompress.result import MediaType

from .conftest import requires_avif, requires_webp

Copier = Callable[..., Path]


def assert_valid_image(path: Path) -> Image.Image:
    """Independently re-open the result, exactly as a user's viewer would."""
    with Image.open(path) as image:
        image.verify()
    reopened = Image.open(path)
    reopened.load()
    return reopened


def test_jpeg_lands_below_target(copy_media: Copier, source_jpg: Path) -> None:
    source = copy_media(source_jpg)
    result = compress(source, 0.4)

    assert result.output_size_bytes < 400_000
    assert result.media_type is MediaType.IMAGE
    assert result.output_path.suffix == ".jpg"
    assert result.output_path.name == "photo_compressed.jpg"
    with assert_valid_image(result.output_path) as image:
        assert image.size == (1200, 900), "resolution kept when quality alone suffices"


def test_jpeg_maximises_quality_under_the_ceiling(copy_media: Copier, source_jpg: Path) -> None:
    """A 400 KB budget should come back near 400 KB, not near zero."""
    result = compress(copy_media(source_jpg), 0.4)
    assert result.output_size_bytes > 300_000, (
        f"only used {result.output_size_bytes} of 400,000 bytes - quality was thrown away"
    )


def test_png_stays_a_png(copy_media: Copier, source_png: Path) -> None:
    source = copy_media(source_png)
    result = compress(source, 0.3)

    assert result.output_size_bytes < 300_000
    assert result.output_path.suffix == ".png"
    assert not result.format_changed
    with assert_valid_image(result.output_path) as image:
        assert image.format == "PNG"


def test_png_with_alpha_keeps_transparency(copy_media: Copier, source_png_alpha: Path) -> None:
    result = compress(copy_media(source_png_alpha), 0.05)

    assert result.output_size_bytes < 50_000
    with assert_valid_image(result.output_path) as image:
        rgba = image.convert("RGBA")
        alpha = rgba.getchannel("A")
        assert alpha.getextrema() != (255, 255), "transparency was lost"


@requires_webp
def test_webp_stays_a_webp(copy_media: Copier, source_webp: Path) -> None:
    result = compress(copy_media(source_webp), 0.2)

    assert result.output_size_bytes < 200_000
    assert result.output_path.suffix == ".webp"
    with assert_valid_image(result.output_path) as image:
        assert image.format == "WEBP"


@requires_avif
def test_avif_round_trips(copy_media: Copier, tmp_path: Path, source_jpg: Path) -> None:
    avif = tmp_path / "photo.avif"
    with Image.open(source_jpg) as image:
        image.save(avif, quality=95)

    result = compress(avif, 0.05)
    assert result.output_size_bytes < 50_000
    assert result.output_path.suffix == ".avif"
    assert_valid_image(result.output_path).close()


def test_downscales_only_when_quality_alone_is_not_enough(
    copy_media: Copier, source_jpg: Path
) -> None:
    """A very small target forces the resolution ladder."""
    result = compress(copy_media(source_jpg), 0.012)

    assert result.output_size_bytes < 12_000
    with assert_valid_image(result.output_path) as image:
        assert image.width < 1200, "expected a downscale for this budget"
    assert any("Reduced resolution" in note for note in result.notes)


def test_impossible_image_target_raises(copy_media: Copier, source_jpg: Path) -> None:
    with pytest.raises(TargetNotAchievableError) as info:
        compress(copy_media(source_jpg), 0.00002)  # 20 bytes

    error = info.value
    assert error.smallest_valid_bytes is not None
    assert error.smallest_valid_bytes > 20
    assert "Target size could not be achieved" in str(error)


def test_no_output_file_is_left_behind_on_failure(
    copy_media: Copier, tmp_path: Path, source_jpg: Path
) -> None:
    source = copy_media(source_jpg)
    with pytest.raises(TargetNotAchievableError):
        compress(source, 0.00002)

    leftovers = [p for p in tmp_path.iterdir() if p != source]
    assert leftovers == [], f"unexpected files left behind: {leftovers}"


def test_every_attempt_is_recorded(copy_media: Copier, source_jpg: Path) -> None:
    result = compress(copy_media(source_jpg), 0.4)
    assert result.attempt_count >= 1
    for attempt in result.attempts:
        assert attempt.size_bytes >= 0
        assert (
            "quality" in attempt.parameters
            or "colors" in attempt.parameters
            or ("mode" in attempt.parameters)
        )
    accepted = [a for a in result.attempts if a.accepted]
    assert accepted, "at least one attempt must be the accepted one"
    assert accepted[-1].size_bytes == result.output_size_bytes


def test_bmp_is_converted_to_jpeg(tmp_path: Path, source_jpg: Path) -> None:
    """BMP has no lossy knob, so JPEG is the sensible target."""
    bmp = tmp_path / "image.bmp"
    with Image.open(source_jpg) as image:
        image.save(bmp)

    result = compress(bmp, 0.1)
    assert result.output_size_bytes < 100_000
    assert_valid_image(result.output_path).close()


def test_gif_stays_a_gif(tmp_path: Path, source_png: Path) -> None:
    gif = tmp_path / "image.gif"
    with Image.open(source_png) as image:
        image.convert("P", palette=Image.Palette.ADAPTIVE).save(gif)

    original = gif.stat().st_size
    result = compress(gif, (original - 20_000) / 1_000_000)
    assert result.output_path.suffix == ".gif"
    with assert_valid_image(result.output_path) as image:
        assert image.format == "GIF"


@pytest.mark.parametrize("name", ["photo with spaces.jpg", "фото.jpg", "photo (1) [x].jpg"])
def test_awkward_filenames(tmp_path: Path, source_jpg: Path, name: str) -> None:
    path = tmp_path / name
    path.write_bytes(source_jpg.read_bytes())

    result = compress(path, 0.3)
    assert result.output_size_bytes < 300_000
    assert result.output_path.exists()
    assert_valid_image(result.output_path).close()


def test_output_goes_next_to_the_input(tmp_path: Path, source_jpg: Path) -> None:
    nested = tmp_path / "some folder" / "deeper"
    nested.mkdir(parents=True)
    source = nested / "photo.jpg"
    source.write_bytes(source_jpg.read_bytes())

    result = compress(source, 0.3)
    assert result.output_path.parent == nested


def test_second_run_does_not_overwrite_the_first(copy_media: Copier, source_jpg: Path) -> None:
    source = copy_media(source_jpg)
    first = compress(source, 0.3)
    first_bytes = first.output_path.read_bytes()

    second = compress(source, 0.3)

    assert second.output_path != first.output_path
    assert second.output_path.name == "photo_compressed_1.jpg"
    assert first.output_path.read_bytes() == first_bytes


def test_input_file_is_never_modified(copy_media: Copier, source_jpg: Path) -> None:
    source = copy_media(source_jpg)
    before = source.read_bytes()

    compress(source, 0.3)

    assert source.read_bytes() == before


def test_explicit_output_path(copy_media: Copier, tmp_path: Path, source_jpg: Path) -> None:
    destination = tmp_path / "chosen name.jpg"
    result = compress(copy_media(source_jpg), 0.3, output_path=destination)
    assert result.output_path == destination
    assert destination.exists()


def test_grayscale_image(tmp_path: Path, source_jpg: Path) -> None:
    gray = tmp_path / "gray.jpg"
    with Image.open(source_jpg) as image:
        image.convert("L").save(gray, quality=97)

    result = compress(gray, 0.05)
    assert result.output_size_bytes < 50_000
    assert_valid_image(result.output_path).close()


def test_tiny_image_that_is_already_small(tmp_path: Path) -> None:
    path = tmp_path / "small.png"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(path)

    result = compress(path, 1)
    assert result.skipped
    assert result.output_path == path
