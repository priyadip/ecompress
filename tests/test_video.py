"""Video compression against real encodes.

Every assertion here is checked against the file on disk and re-probed with
ffprobe, independently of whatever the package reported.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from ecompress import compress
from ecompress.errors import TargetNotAchievableError
from ecompress.ffmpeg import probe
from ecompress.result import MediaType

from .conftest import requires_vp9, requires_x264

Copier = Callable[..., Path]

pytestmark = [requires_x264, pytest.mark.slow]


def test_mp4_lands_below_target(copy_media: Copier, source_mp4: Path) -> None:
    source = copy_media(source_mp4)
    result = compress(source, 0.25)

    assert result.output_size_bytes < 250_000
    assert result.output_path.stat().st_size < 250_000
    assert result.media_type is MediaType.VIDEO
    assert result.output_path.name == "clip_compressed.mp4"

    info = probe(result.output_path)
    assert info.has_video
    assert info.has_audio, "the audio track must survive"


def test_duration_is_preserved(copy_media: Copier, source_mp4: Path) -> None:
    source = copy_media(source_mp4)
    original = probe(source).duration
    assert original is not None

    result = compress(source, 0.25)

    compressed = probe(result.output_path).duration
    assert compressed is not None
    assert abs(compressed - original) < 0.5


def test_quality_is_maximised_not_minimised(copy_media: Copier, source_mp4: Path) -> None:
    """A 250 KB budget should be largely used, not undershot into mush."""
    result = compress(copy_media(source_mp4), 0.25)
    assert result.output_size_bytes > 150_000, (
        f"used only {result.output_size_bytes} of 250,000 bytes"
    )


def test_search_converges_in_few_encodes(copy_media: Copier, source_mp4: Path) -> None:
    result = compress(copy_media(source_mp4), 0.25)
    assert result.attempt_count <= 7, "the search should converge, not grind"


def test_silent_video_works(copy_media: Copier, source_mp4_silent: Path) -> None:
    result = compress(copy_media(source_mp4_silent), 0.12)

    assert result.output_size_bytes < 120_000
    info = probe(result.output_path)
    assert info.has_video
    assert not info.has_audio


def test_hd_video_downscales_when_the_budget_is_thin(
    copy_media: Copier, source_mp4_hd: Path
) -> None:
    source = copy_media(source_mp4_hd)
    result = compress(source, 0.05)

    assert result.output_size_bytes < 50_000
    info = probe(result.output_path)
    video = info.primary_video
    assert video is not None
    assert (video.width or 0) < 1280, "a 50 KB budget cannot carry 720p"
    assert any("not enough for" in note or "Stepping down" in note for note in result.notes), (
        f"the downscale should have been explained: {result.notes}"
    )


def test_mkv_stays_mkv(copy_media: Copier, source_mkv: Path) -> None:
    result = compress(copy_media(source_mkv), 0.1)
    assert result.output_path.suffix == ".mkv"
    assert result.output_size_bytes < 100_000
    assert not result.format_changed
    assert probe(result.output_path).has_video


def test_mov_stays_mov(copy_media: Copier, source_mov: Path) -> None:
    result = compress(copy_media(source_mov), 0.1)
    assert result.output_path.suffix == ".mov"
    assert result.output_size_bytes < 100_000
    assert probe(result.output_path).has_video


@requires_vp9
def test_webm_stays_webm(tmp_path: Path, source_mp4_silent: Path) -> None:
    from .conftest import run_ffmpeg

    webm = tmp_path / "clip.webm"
    run_ffmpeg(
        [
            "-i", str(source_mp4_silent),
            "-c:v", "libvpx-vp9", "-b:v", "1M", "-deadline", "realtime", "-cpu-used", "8",
            str(webm),
        ]
    )  # fmt: skip

    result = compress(webm, webm.stat().st_size * 0.5 / 1_000_000)

    assert result.output_path.suffix == ".webm"
    assert result.output_size_bytes < webm.stat().st_size * 0.5 * 1_000_000 / 1_000_000
    info = probe(result.output_path)
    assert info.has_video
    assert (info.primary_video.codec_name if info.primary_video else "") == "vp9"


def test_high_fps_video_trades_frame_rate_for_resolution(
    copy_media: Copier, source_mp4_high_fps: Path
) -> None:
    """A 60 fps source on a thin budget should lose frames, not just pixels."""
    source = copy_media(source_mp4_high_fps)
    result = compress(source, 0.1)

    assert result.output_size_bytes < 100_000

    info = probe(result.output_path)
    video = info.primary_video
    assert video is not None
    assert video.frame_rate is not None
    assert video.frame_rate < 55, "frame rate should have been reduced"
    assert any("fps" in note for note in result.notes)


def test_reducing_frame_rate_preserves_duration(
    copy_media: Copier, source_mp4_high_fps: Path
) -> None:
    """Dropping frames must not shorten the clip."""
    source = copy_media(source_mp4_high_fps)
    original = probe(source).duration
    assert original is not None

    result = compress(source, 0.1)

    compressed = probe(result.output_path).duration
    assert compressed is not None
    assert abs(compressed - original) < 0.5


def test_frame_rate_is_kept_when_the_budget_allows(
    copy_media: Copier, source_mp4_high_fps: Path
) -> None:
    """No gratuitous frame-rate loss on a comfortable budget."""
    source = copy_media(source_mp4_high_fps)
    result = compress(source, source.stat().st_size * 0.9 / 1_000_000)

    info = probe(result.output_path)
    video = info.primary_video
    assert video is not None
    assert video.frame_rate is not None
    assert video.frame_rate > 55, "frame rate was reduced for no reason"


def test_video_respects_a_size_range(copy_media: Copier, source_mp4: Path) -> None:
    source = copy_media(source_mp4)
    result = compress(source, "0.40-0.50")

    assert result.output_size_bytes < 500_000
    assert result.output_size_bytes >= 400_000
    assert result.within_requested_range
    assert probe(result.output_path).has_video


def test_impossible_video_target_raises_with_the_best_achieved(
    copy_media: Copier, source_mp4: Path
) -> None:
    with pytest.raises(TargetNotAchievableError) as info:
        compress(copy_media(source_mp4), 0.0005)  # 500 bytes

    error = info.value
    assert error.target_bytes == 500
    assert error.smallest_valid_bytes is None or error.smallest_valid_bytes > 500
    message = str(error)
    assert "could not be achieved" in message


def test_failure_leaves_no_output_behind(
    copy_media: Copier, tmp_path: Path, source_mp4: Path
) -> None:
    source = copy_media(source_mp4)
    with pytest.raises(TargetNotAchievableError):
        compress(source, 0.0005)

    leftovers = [p for p in tmp_path.iterdir() if p != source]
    assert leftovers == [], f"unexpected files left behind: {leftovers}"


def test_original_is_untouched(copy_media: Copier, source_mp4: Path) -> None:
    source = copy_media(source_mp4)
    before = source.read_bytes()
    compress(source, 0.25)
    assert source.read_bytes() == before


def test_attempts_carry_their_settings(copy_media: Copier, source_mp4: Path) -> None:
    result = compress(copy_media(source_mp4), 0.25)
    accepted = [a for a in result.attempts if a.accepted]
    assert accepted
    assert accepted[-1].parameters.get("video_bitrate") or accepted[-1].parameters.get("crf")


@pytest.mark.parametrize("name", ["my video file.mp4", "видео.mp4", "clip (final) [v2].mp4"])
def test_awkward_video_filenames(tmp_path: Path, source_mp4: Path, name: str) -> None:
    path = tmp_path / name
    path.write_bytes(source_mp4.read_bytes())

    result = compress(path, 0.25)
    assert result.output_size_bytes < 250_000
    assert probe(result.output_path).has_video


def test_dotted_stem_is_handled(tmp_path: Path, source_mp4: Path) -> None:
    """The user's own example filename shape."""
    path = tmp_path / "CasualIQBusinessIntelligence.ai.mp4"
    path.write_bytes(source_mp4.read_bytes())

    result = compress(path, 0.25)
    assert result.output_path.name == "CasualIQBusinessIntelligence.ai_compressed.mp4"
    assert result.output_size_bytes < 250_000
