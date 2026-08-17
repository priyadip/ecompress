"""FFmpeg discovery, capability detection and probing."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ecompress.errors import MissingDependencyError, ToolExecutionError
from ecompress.ffmpeg import (
    FFmpegTools,
    clear_ffmpeg_cache,
    find_ffmpeg_tools,
    first_available_encoder,
    has_encoder,
    probe,
)

from .conftest import requires_ffmpeg


def test_discovery_returns_a_tools_object() -> None:
    tools = find_ffmpeg_tools()
    assert isinstance(tools, FFmpegTools)
    assert tools.available == (tools.ffmpeg is not None and tools.ffprobe is not None)


def test_missing_tools_raise_an_actionable_error() -> None:
    tools = FFmpegTools(ffmpeg=None, ffprobe=None)
    with pytest.raises(MissingDependencyError) as info:
        tools.require("a video")

    message = str(info.value)
    assert "FFmpeg is not installed" in message
    assert "This file is a video and requires FFmpeg" in message
    assert "COMPRESS_FFMPEG" in message
    # The message must tell the user what to actually do.
    assert any(word in message for word in ("winget", "brew", "apt"))


def test_partial_install_is_reported(tmp_path: Path) -> None:
    tools = FFmpegTools(ffmpeg=tmp_path / "ffmpeg", ffprobe=None)
    with pytest.raises(MissingDependencyError, match="ffprobe not found"):
        tools.require()


@requires_ffmpeg
def test_require_returns_both_paths() -> None:
    ffmpeg, ffprobe = find_ffmpeg_tools().require()
    assert ffmpeg.exists()
    assert ffprobe.exists()


@requires_ffmpeg
def test_encoder_detection_is_honest() -> None:
    """We must never assume an encoder exists."""
    assert not has_encoder("definitely-not-a-real-encoder")
    assert first_available_encoder(["nope-1", "nope-2"]) is None


@requires_ffmpeg
def test_first_available_encoder_prefers_the_earlier_entry() -> None:
    chosen = first_available_encoder(["not-real", "libx264", "mpeg4"])
    assert chosen in {"libx264", "mpeg4", None}


def test_env_var_override_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    fake.write_text("", encoding="utf-8")

    monkeypatch.setenv("COMPRESS_FFMPEG", str(fake))
    clear_ffmpeg_cache()
    try:
        assert find_ffmpeg_tools().ffmpeg == fake
    finally:
        monkeypatch.delenv("COMPRESS_FFMPEG", raising=False)
        clear_ffmpeg_cache()


@requires_ffmpeg
def test_probe_reads_video_properties(source_mp4: Path) -> None:
    info = probe(source_mp4)

    assert info.has_video
    assert info.has_audio
    assert info.duration is not None and 3.5 < info.duration < 4.5

    video = info.primary_video
    assert video is not None
    assert video.width == 640
    assert video.height == 480
    assert video.frame_rate is not None and 29 < video.frame_rate < 31


@requires_ffmpeg
def test_probe_reads_audio_properties(source_wav: Path) -> None:
    info = probe(source_wav)

    assert info.has_audio
    assert not info.has_video
    audio = info.primary_audio
    assert audio is not None
    assert audio.channels == 2
    assert audio.sample_rate == 44_100


@requires_ffmpeg
def test_probe_rejects_a_non_media_file(tmp_path: Path) -> None:
    path = tmp_path / "garbage.mp4"
    path.write_bytes(b"\x00" * 4096)
    with pytest.raises(ToolExecutionError):
        probe(path)


@requires_ffmpeg
def test_attached_cover_art_is_not_treated_as_video(
    tmp_path: Path, source_mp3: Path, source_jpg: Path
) -> None:
    """An MP3 with embedded artwork is audio, not a video."""
    from .conftest import run_ffmpeg

    tagged = tmp_path / "tagged.mp3"
    run_ffmpeg(
        [
            "-i", str(source_mp3), "-i", str(source_jpg),
            "-map", "0:a", "-map", "1:v", "-c", "copy",
            "-disposition:v:0", "attached_pic",
            str(tagged),
        ]
    )  # fmt: skip

    info = probe(tagged)
    assert info.has_audio
    assert not info.has_video, "cover art was mistaken for a video stream"
