"""Media detection: contents first, extension second."""

from __future__ import annotations

from pathlib import Path

import pytest

from ecompress.detect import detect_media_type, sniff_container
from ecompress.errors import InputFileError, UnsupportedFormatError
from ecompress.result import MediaType

from .conftest import requires_ffmpeg, requires_reportlab


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (b"%PDF-1.7\n%\xe2\xe3", "pdf"),
        (b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0d", "png"),
        (b"\xff\xd8\xff\xe0\x00\x10JFIF", "jpeg"),
        (b"GIF89a\x01\x00", "gif"),
        (b"RIFF\x24\x00\x00\x00WEBPVP8 ", "webp"),
        (b"RIFF\x24\x00\x00\x00WAVEfmt ", "wav"),
        (b"RIFF\x24\x00\x00\x00AVI LIST", "avi"),
        (b"\x1a\x45\xdf\xa3\x01\x00\x00\x00", "matroska"),
        (b"OggS\x00\x02\x00\x00", "ogg"),
        (b"fLaC\x00\x00\x00\x22", "flac"),
        (b"ID3\x04\x00\x00\x00\x00", "mp3"),
        (b"\xff\xfb\x90\x64\x00\x00\x00\x00", "mp3"),
        (b"\x00\x00\x00\x20ftypisom", "mp4"),
        (b"\x00\x00\x00\x20ftypM4A ", "m4a"),
        (b"\x00\x00\x00\x20ftypavif", "avif"),
        (b"\x00\x00\x00\x20ftypheic", "heif"),
        (b"BM\x36\x00\x00\x00\x00\x00", "bmp"),
        (b"II*\x00\x08\x00\x00\x00", "tiff"),
    ],
)
def test_sniff_recognises_magic_bytes(header: bytes, expected: str) -> None:
    assert sniff_container(header) == expected


def test_sniff_returns_none_for_unknown_content() -> None:
    assert sniff_container(b"this is just some text at the start") is None


def test_sniff_handles_a_truncated_header() -> None:
    assert sniff_container(b"ab") is None


def test_detects_a_real_jpeg(source_jpg: Path) -> None:
    detection = detect_media_type(source_jpg)
    assert detection.media_type is MediaType.IMAGE
    assert detection.container == "jpeg"
    assert detection.extension == ".jpg"


def test_detects_a_real_png(source_png: Path) -> None:
    assert detect_media_type(source_png).media_type is MediaType.IMAGE


def test_detects_a_real_wav(source_wav: Path) -> None:
    assert detect_media_type(source_wav).media_type is MediaType.AUDIO


@requires_reportlab
def test_detects_a_real_pdf(source_pdf_text: Path) -> None:
    detection = detect_media_type(source_pdf_text)
    assert detection.media_type is MediaType.PDF
    assert detection.container == "pdf"


@requires_ffmpeg
def test_detects_a_real_mp4_as_video(source_mp4: Path) -> None:
    assert detect_media_type(source_mp4).media_type is MediaType.VIDEO


@requires_ffmpeg
def test_mp4_holding_only_audio_is_detected_as_audio(source_audio_only_mp4: Path) -> None:
    """An .mp4 with no video stream must go to the audio backend."""
    assert detect_media_type(source_audio_only_mp4).media_type is MediaType.AUDIO


@requires_ffmpeg
def test_detects_mkv_as_video(source_mkv: Path) -> None:
    assert detect_media_type(source_mkv).media_type is MediaType.VIDEO


def test_contents_win_over_a_lying_extension(tmp_path: Path, source_png: Path) -> None:
    """A PNG named .mp4 must be treated as an image, not handed to FFmpeg."""
    liar = tmp_path / "actually-a-png.mp4"
    liar.write_bytes(source_png.read_bytes())

    detection = detect_media_type(liar)
    assert detection.media_type is MediaType.IMAGE
    assert any("contents are PNG" in note for note in detection.notes)


@requires_reportlab
def test_pdf_named_mp4_is_detected_as_pdf(tmp_path: Path, source_pdf_text: Path) -> None:
    liar = tmp_path / "report.mp4"
    liar.write_bytes(source_pdf_text.read_bytes())
    assert detect_media_type(liar).media_type is MediaType.PDF


def test_unknown_content_falls_back_to_the_extension(tmp_path: Path) -> None:
    path = tmp_path / "mystery.jpg"
    path.write_bytes(b"\x00" * 512)
    detection = detect_media_type(path)
    assert detection.media_type is MediaType.IMAGE
    assert any("extension" in note for note in detection.notes)


def test_unsupported_type_is_rejected_with_guidance(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_bytes(b"just some text")
    with pytest.raises(UnsupportedFormatError) as info:
        detect_media_type(path)
    message = str(info.value)
    assert "Unsupported file type" in message
    assert "Supported types" in message
    assert ".mp4" in message


def test_missing_file_raises_input_error(tmp_path: Path) -> None:
    with pytest.raises(InputFileError):
        detect_media_type(tmp_path / "nope.jpg")
