"""Validation is what makes faking the target size impossible."""

from __future__ import annotations

from pathlib import Path

import pytest

from ecompress.result import MediaType
from ecompress.validation import validate_output

from .conftest import requires_ffmpeg, requires_reportlab


def test_missing_file_is_invalid(tmp_path: Path) -> None:
    report = validate_output(tmp_path / "nope.jpg", MediaType.IMAGE)
    assert not report.valid
    assert "not created" in report.reason


def test_empty_file_is_invalid(tmp_path: Path) -> None:
    path = tmp_path / "empty.jpg"
    path.touch()
    report = validate_output(path, MediaType.IMAGE)
    assert not report.valid
    assert "empty" in report.reason


def test_directory_is_invalid(tmp_path: Path) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    assert not validate_output(folder, MediaType.IMAGE).valid


def test_a_real_image_passes(source_jpg: Path) -> None:
    assert validate_output(source_jpg, MediaType.IMAGE).valid


def test_garbage_named_as_an_image_fails(tmp_path: Path) -> None:
    path = tmp_path / "fake.jpg"
    path.write_bytes(b"\xff\xd8\xff" + b"\x00" * 5000)
    assert not validate_output(path, MediaType.IMAGE).valid


def test_a_truncated_image_fails(tmp_path: Path, source_jpg: Path) -> None:
    """Chopping bytes off to hit a size target must never validate."""
    truncated = tmp_path / "truncated.jpg"
    data = source_jpg.read_bytes()
    truncated.write_bytes(data[: len(data) // 3])
    assert not validate_output(truncated, MediaType.IMAGE).valid


@requires_ffmpeg
def test_a_real_video_passes(source_mp4: Path) -> None:
    assert validate_output(source_mp4, MediaType.VIDEO).valid


@requires_ffmpeg
def test_a_truncated_video_fails(tmp_path: Path, source_mp4: Path) -> None:
    truncated = tmp_path / "truncated.mp4"
    data = source_mp4.read_bytes()
    truncated.write_bytes(data[: len(data) // 4])
    assert not validate_output(truncated, MediaType.VIDEO).valid


@requires_ffmpeg
def test_video_validation_rejects_a_shortened_duration(source_mp4: Path) -> None:
    """A 4 s source that came back as 1 s is not a compression of it."""
    report = validate_output(source_mp4, MediaType.VIDEO, expected_duration=60.0)
    assert not report.valid
    assert "duration changed" in report.reason


@requires_ffmpeg
def test_video_validation_allows_small_duration_drift(source_mp4: Path) -> None:
    report = validate_output(source_mp4, MediaType.VIDEO, expected_duration=4.0)
    assert report.valid


@requires_ffmpeg
def test_an_audio_file_is_not_a_valid_video(source_wav: Path) -> None:
    report = validate_output(source_wav, MediaType.VIDEO)
    assert not report.valid
    assert "no video stream" in report.reason


@requires_ffmpeg
def test_a_real_wav_passes_audio_validation(source_wav: Path) -> None:
    assert validate_output(source_wav, MediaType.AUDIO).valid


@requires_reportlab
def test_a_real_pdf_passes(source_pdf_text: Path) -> None:
    assert validate_output(source_pdf_text, MediaType.PDF).valid


@requires_reportlab
def test_a_truncated_pdf_fails(tmp_path: Path, source_pdf_text: Path) -> None:
    truncated = tmp_path / "truncated.pdf"
    data = source_pdf_text.read_bytes()
    truncated.write_bytes(data[: len(data) // 3])
    assert not validate_output(truncated, MediaType.PDF).valid


@requires_reportlab
def test_pdf_validation_rejects_a_changed_page_count(source_pdf_text: Path) -> None:
    report = validate_output(source_pdf_text, MediaType.PDF, expected_pages=99)
    assert not report.valid
    assert "page count changed" in report.reason


@requires_reportlab
def test_pdf_validation_accepts_the_right_page_count(source_pdf_text: Path) -> None:
    assert validate_output(source_pdf_text, MediaType.PDF, expected_pages=3).valid


def test_report_is_falsy_when_invalid(tmp_path: Path) -> None:
    assert not validate_output(tmp_path / "missing.pdf", MediaType.PDF)


@pytest.mark.parametrize("media", list(MediaType))
def test_every_media_type_rejects_an_empty_file(tmp_path: Path, media: MediaType) -> None:
    path = tmp_path / "empty.bin"
    path.touch()
    assert not validate_output(path, media).valid
