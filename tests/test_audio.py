"""Audio compression against real encodes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from compress import compress
from compress.errors import TargetNotAchievableError
from compress.ffmpeg import probe
from compress.result import MediaType

from .conftest import requires_ffmpeg, requires_mp3, requires_opus

Copier = Callable[..., Path]

pytestmark = [requires_ffmpeg, pytest.mark.slow]


@requires_mp3
def test_wav_converts_and_lands_below_target(copy_media: Copier, source_wav: Path) -> None:
    source = copy_media(source_wav)
    result = compress(source, 0.25)

    assert result.output_size_bytes < 250_000
    assert result.media_type is MediaType.AUDIO
    assert result.format_changed, "a WAV cannot shrink while staying a WAV"
    assert result.output_path.suffix in {".mp3", ".opus", ".flac"}
    assert result.notes, "a format change must be explained"

    info = probe(result.output_path)
    assert info.has_audio


@requires_mp3
def test_duration_is_preserved(copy_media: Copier, source_wav: Path) -> None:
    source = copy_media(source_wav)
    original = probe(source).duration
    assert original is not None

    result = compress(source, 0.25)

    compressed = probe(result.output_path).duration
    assert compressed is not None
    assert abs(compressed - original) < 0.5


@requires_ffmpeg
def test_lossless_flac_is_preferred_when_it_fits(copy_media: Copier, source_wav: Path) -> None:
    """A generous target should buy a bit-perfect result, not a lossy one."""
    source = copy_media(source_wav)
    target_mb = source.stat().st_size * 0.95 / 1_000_000

    result = compress(source, target_mb)

    assert result.output_path.suffix == ".flac"
    assert result.output_size_bytes < int(target_mb * 1_000_000)
    assert any("lossless" in note.lower() for note in result.notes)


@requires_mp3
def test_mp3_stays_an_mp3(copy_media: Copier, source_mp3: Path) -> None:
    source = copy_media(source_mp3)
    result = compress(source, source.stat().st_size * 0.4 / 1_000_000)

    assert result.output_path.suffix == ".mp3"
    assert not result.format_changed
    info = probe(result.output_path)
    assert (info.primary_audio.codec_name if info.primary_audio else "") == "mp3"


def test_m4a_stays_aac(copy_media: Copier, source_m4a: Path) -> None:
    source = copy_media(source_m4a)
    result = compress(source, source.stat().st_size * 0.4 / 1_000_000)

    assert result.output_path.suffix == ".m4a"
    assert not result.format_changed
    info = probe(result.output_path)
    assert (info.primary_audio.codec_name if info.primary_audio else "") == "aac"


def test_flac_input_is_handled(copy_media: Copier, source_flac: Path) -> None:
    source = copy_media(source_flac)
    result = compress(source, source.stat().st_size * 0.3 / 1_000_000)

    assert result.output_size_bytes < source.stat().st_size * 0.3
    assert probe(result.output_path).has_audio


@requires_opus
def test_very_small_budget_switches_to_opus(copy_media: Copier, source_wav: Path) -> None:
    """Below roughly 48 kbps, MP3 is unusable and Opus should take over."""
    result = compress(copy_media(source_wav), 0.02)  # 20 KB for 6 s ≈ 26 kbps

    assert result.output_size_bytes < 20_000
    assert result.output_path.suffix == ".opus"
    info = probe(result.output_path)
    assert (info.primary_audio.codec_name if info.primary_audio else "") == "opus"


@requires_ffmpeg
def test_audio_only_mp4_uses_the_audio_backend(
    copy_media: Copier, source_audio_only_mp4: Path
) -> None:
    source = copy_media(source_audio_only_mp4)
    result = compress(source, source.stat().st_size * 0.5 / 1_000_000)

    assert result.media_type is MediaType.AUDIO
    info = probe(result.output_path)
    assert info.has_audio
    assert not info.has_video


def test_impossible_audio_target_raises(copy_media: Copier, source_wav: Path) -> None:
    with pytest.raises(TargetNotAchievableError) as info:
        compress(copy_media(source_wav), 0.0002)  # 200 bytes for 6 s

    assert "could not be achieved" in str(info.value)


def test_failure_leaves_no_output_behind(
    copy_media: Copier, tmp_path: Path, source_wav: Path
) -> None:
    source = copy_media(source_wav)
    with pytest.raises(TargetNotAchievableError):
        compress(source, 0.0002)

    leftovers = [p for p in tmp_path.iterdir() if p != source]
    assert leftovers == [], f"unexpected files left behind: {leftovers}"


@requires_mp3
def test_original_is_untouched(copy_media: Copier, source_wav: Path) -> None:
    source = copy_media(source_wav)
    before = source.read_bytes()
    compress(source, 0.25)
    assert source.read_bytes() == before


@requires_mp3
@pytest.mark.parametrize("name", ["my song.wav", "песня.wav", "track (1) [remix].wav"])
def test_awkward_audio_filenames(tmp_path: Path, source_wav: Path, name: str) -> None:
    path = tmp_path / name
    path.write_bytes(source_wav.read_bytes())

    result = compress(path, 0.25)
    assert result.output_size_bytes < 250_000
    assert probe(result.output_path).has_audio
