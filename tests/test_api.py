"""The Python API: input handling, the skip path, and the result object."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from PIL import Image

from compress import compress
from compress.errors import (
    CompressError,
    InputFileError,
    InvalidTargetError,
    UnsupportedFormatError,
)
from compress.reporting import Reporter
from compress.result import Attempt, CompressionResult, MediaType

Copier = Callable[..., Path]


# -- input validation ------------------------------------------------------


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(InputFileError, match="File not found"):
        compress(tmp_path / "nope.jpg", 1)


def test_directory_input(tmp_path: Path) -> None:
    with pytest.raises(InputFileError, match="is a folder"):
        compress(tmp_path, 1)


def test_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.jpg"
    path.touch()
    with pytest.raises(InputFileError, match="empty"):
        compress(path, 1)


def test_blank_path() -> None:
    with pytest.raises(InputFileError, match="No input file"):
        compress("   ", 1)


def test_wrong_path_type() -> None:
    with pytest.raises(InputFileError, match="Expected a file path"):
        compress(42, 1)  # type: ignore[arg-type]


def test_unsupported_type(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("hello", encoding="utf-8")
    with pytest.raises(UnsupportedFormatError):
        compress(path, 1)


@pytest.mark.parametrize("bad", [0, -1, -0.5, "0", "abc", float("nan"), float("inf")])
def test_invalid_targets(tmp_path: Path, source_jpg: Path, bad: object) -> None:
    path = tmp_path / "photo.jpg"
    path.write_bytes(source_jpg.read_bytes())
    with pytest.raises(InvalidTargetError):
        compress(path, bad)  # type: ignore[arg-type]


def test_boolean_target_is_rejected(tmp_path: Path, source_jpg: Path) -> None:
    """`True` is an int in Python; it is not a size."""
    path = tmp_path / "photo.jpg"
    path.write_bytes(source_jpg.read_bytes())
    with pytest.raises(InvalidTargetError):
        compress(path, True)


def test_string_targets_are_accepted(copy_media: Copier, source_jpg: Path) -> None:
    result = compress(copy_media(source_jpg), "0.3")
    assert result.output_size_bytes < 300_000


def test_all_errors_share_a_base_class(tmp_path: Path) -> None:
    with pytest.raises(CompressError):
        compress(tmp_path / "nope.jpg", 1)


def test_accepts_a_string_path(copy_media: Copier, source_jpg: Path) -> None:
    source = copy_media(source_jpg)
    result = compress(str(source), 0.3)
    assert result.output_path.exists()


# -- already small enough --------------------------------------------------


def test_file_already_below_target_is_left_alone(copy_media: Copier, source_jpg: Path) -> None:
    source = copy_media(source_jpg)
    before = source.read_bytes()

    result = compress(source, 100)

    assert result.skipped
    assert result.target_achieved
    assert result.output_path == source
    assert result.output_size_bytes == result.input_size_bytes
    assert result.saved_bytes == 0
    assert result.reduction_percent == 0.0
    assert source.read_bytes() == before, "the original must not be re-encoded"
    assert result.notes


def test_skip_creates_no_new_file(copy_media: Copier, tmp_path: Path, source_jpg: Path) -> None:
    source = copy_media(source_jpg)
    compress(source, 100)
    assert list(tmp_path.iterdir()) == [source]


def test_skip_does_not_need_ffmpeg(copy_media: Copier, source_mp4: Path) -> None:
    """The skip path must not depend on any external tool."""
    result = compress(copy_media(source_mp4), 500)
    assert result.skipped


# -- the result object -----------------------------------------------------


def test_result_fields(copy_media: Copier, source_jpg: Path) -> None:
    source = copy_media(source_jpg)
    result = compress(source, 0.3)

    assert isinstance(result, CompressionResult)
    assert result.input_path == source
    assert result.output_path.is_absolute() or result.output_path.exists()
    assert result.input_size_bytes == source.stat().st_size
    assert result.output_size_bytes == result.output_path.stat().st_size
    assert result.target_size_bytes == 300_000
    assert result.saved_bytes == result.input_size_bytes - result.output_size_bytes
    assert 0 < result.reduction_percent < 100
    assert result.media_type is MediaType.IMAGE
    assert result.backend == "image"
    assert result.target_achieved is True
    assert result.skipped is False
    assert result.attempt_count == len(result.attempts)


def test_result_mb_properties_agree_with_bytes(copy_media: Copier, source_jpg: Path) -> None:
    result = compress(copy_media(source_jpg), 0.3)
    assert result.output_size_mb == result.output_size_bytes / 1_000_000
    assert result.target_size_mb == 0.3
    assert result.output_size_mb < result.target_size_mb


def test_result_is_json_serialisable(copy_media: Copier, source_jpg: Path) -> None:
    import json

    result = compress(copy_media(source_jpg), 0.3)
    payload = json.loads(json.dumps(result.to_dict()))

    assert payload["target_achieved"] is True
    assert payload["output_size_bytes"] < payload["target_size_bytes"]
    assert payload["media_type"] == "image"


def test_result_rejects_negative_sizes() -> None:
    with pytest.raises(ValueError):
        CompressionResult(
            input_path=Path("a"),
            output_path=Path("b"),
            input_size_bytes=-1,
            output_size_bytes=0,
            target_size_bytes=1,
            media_type=MediaType.IMAGE,
        )


def test_reduction_percent_handles_a_zero_input() -> None:
    result = CompressionResult(
        input_path=Path("a"),
        output_path=Path("b"),
        input_size_bytes=0,
        output_size_bytes=0,
        target_size_bytes=1,
        media_type=MediaType.IMAGE,
    )
    assert result.reduction_percent == 0.0


def test_attempt_size_mb() -> None:
    assert Attempt(1, 2_500_000).size_mb == 2.5


# -- reporting -------------------------------------------------------------


class _Collector(Reporter):
    def __init__(self) -> None:
        self.steps: list[str] = []
        self.attempts: list[Attempt] = []
        self.notes: list[str] = []

    def step(self, message: str) -> None:
        self.steps.append(message)

    def attempt(self, attempt: Attempt) -> None:
        self.attempts.append(attempt)

    def note(self, message: str) -> None:
        self.notes.append(message)


def test_reporter_receives_progress(copy_media: Copier, source_jpg: Path) -> None:
    collector = _Collector()
    result = compress(copy_media(source_jpg), 0.3, reporter=collector)

    assert any("Original size" in step for step in collector.steps)
    assert any("Image detected" in step for step in collector.steps)
    assert any("Optimizing" in step for step in collector.steps)
    assert len(collector.attempts) == result.attempt_count


def test_default_api_is_silent(
    copy_media: Copier, source_jpg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    compress(copy_media(source_jpg), 0.3)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


# -- output placement ------------------------------------------------------


def test_overwrite_flag_replaces_an_existing_output(
    copy_media: Copier, tmp_path: Path, source_jpg: Path
) -> None:
    source = copy_media(source_jpg)
    destination = tmp_path / "out.jpg"
    destination.write_bytes(b"old contents")

    result = compress(source, 0.3, output_path=destination, overwrite=True)

    assert result.output_path == destination
    assert destination.read_bytes() != b"old contents"


def test_explicit_output_refuses_to_clobber_the_input(copy_media: Copier, source_jpg: Path) -> None:
    source = copy_media(source_jpg)
    with pytest.raises(InputFileError, match="Refusing to overwrite"):
        compress(source, 0.3, output_path=source)


def test_scratch_directory_is_cleaned_up(
    copy_media: Copier, tmp_path: Path, source_jpg: Path
) -> None:
    compress(copy_media(source_jpg), 0.3)
    leftovers = [p for p in tmp_path.iterdir() if p.is_dir() and p.name.startswith(".compress-")]
    assert leftovers == [], f"scratch directories left behind: {leftovers}"


def test_readonly_source_directory_still_works(tmp_path: Path, source_jpg: Path) -> None:
    """Writing elsewhere must work even if the input folder is awkward."""
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    source = source_dir / "photo.jpg"
    source.write_bytes(source_jpg.read_bytes())

    out_dir = tmp_path / "output"
    out_dir.mkdir()
    result = compress(source, 0.3, output_path=out_dir / "small.jpg")

    assert result.output_path.parent == out_dir


def test_unicode_directory(tmp_path: Path, source_jpg: Path) -> None:
    folder = tmp_path / "фотографии 写真"
    folder.mkdir()
    source = folder / "photo.jpg"
    source.write_bytes(source_jpg.read_bytes())

    result = compress(source, 0.3)
    assert result.output_path.parent == folder
    with Image.open(result.output_path) as image:
        image.verify()
