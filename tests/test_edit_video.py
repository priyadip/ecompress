"""cut_video / add_video on real encoded clips, verified with ffprobe."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from ecompress import CommandSyntaxError, InputFileError, add_video, cut_video, execute
from ecompress.edit.grammar import Segment
from ecompress.edit.video import _Clip, filter_graph
from ecompress.errors import MissingDependencyError
from ecompress.ffmpeg import MediaInfo, StreamInfo, _rotation, probe
from ecompress.reporting import Reporter
from tests.conftest import requires_ffmpeg, requires_opus, requires_vp9, requires_x264, run_ffmpeg

CopyMedia = Callable[..., Path]


def assert_duration(path: Path, expected: float, tolerance: float = 0.15) -> None:
    duration = probe(path).duration
    assert duration is not None
    assert abs(duration - expected) <= tolerance, f"{duration} != {expected}"


class Recorder(Reporter):
    def __init__(self) -> None:
        self.steps: list[str] = []
        self.fractions: list[float] = []

    def step(self, message: str) -> None:
        self.steps.append(message)

    def progress(self, fraction: float, *, label: str = "") -> None:
        self.fractions.append(fraction)


# -- real encodes ---------------------------------------------------------------


@requires_x264
def test_cut_seconds_range(copy_media: CopyMedia, source_mp4: Path) -> None:
    clip = copy_media(source_mp4)
    reporter = Recorder()
    result = cut_video(clip, "1-2.5", reporter=reporter)

    assert result.output_path == clip.with_name("clip_cut.mp4")
    assert_duration(result.output_path, 1.5)
    info = probe(result.output_path)
    assert info.has_audio
    assert info.primary_video is not None
    assert (info.primary_video.width, info.primary_video.height) == (640, 480)
    assert result.duration_seconds == pytest.approx(1.5, abs=0.15)
    assert reporter.steps[0] == "clip.mp4: 0:01 to 0:02.5 (0:01.5)"
    assert reporter.fractions and reporter.fractions[-1] > 0.8


@requires_x264
def test_cut_clock_format_into_output_folder(
    copy_media: CopyMedia, source_mp4: Path, tmp_path: Path
) -> None:
    clip = copy_media(source_mp4)
    out = tmp_path / "exports"
    result = execute(f'cut_video "{clip}"[00:00:01-00:03] -> "{out}"')
    assert result.output_path == out / "clip_cut.mp4"
    assert_duration(result.output_path, 2.0)


@requires_x264
def test_cut_several_ranges_joins_them(copy_media: CopyMedia, source_mp4: Path) -> None:
    clip = copy_media(source_mp4)
    result = cut_video(clip, "0-1,2.5-end")
    assert_duration(result.output_path, 2.5)


@requires_x264
def test_add_clips_of_different_size_and_audio(
    copy_media: CopyMedia, source_mp4: Path, source_mp4_silent: Path, source_mp4_hd: Path
) -> None:
    first = copy_media(source_mp4)  # 640x480 with audio
    silent = copy_media(source_mp4_silent)  # 480x360, no audio
    wide = copy_media(source_mp4_hd)  # 1280x720 with audio

    result = add_video([(first, "0-1.5"), (silent, (0.5, 2)), (wide, "0-1")])

    assert result.output_path == first.with_name("clip_joined.mp4")
    info = probe(result.output_path)
    assert info.primary_video is not None
    assert (info.primary_video.width, info.primary_video.height) == (640, 480)
    assert info.has_audio  # the silent clip got silence, not a dropped track
    assert_duration(result.output_path, 4.0, tolerance=0.25)


@requires_x264
def test_add_silent_clips_has_no_audio(copy_media: CopyMedia, source_mp4_silent: Path) -> None:
    silent = copy_media(source_mp4_silent)
    result = add_video([(silent, "0-1"), (silent, "1-2")])
    info = probe(result.output_path)
    assert not info.has_audio
    assert_duration(result.output_path, 2.0)


@requires_x264
def test_rotated_phone_video_keeps_its_orientation(
    copy_media: CopyMedia, source_mp4: Path, tmp_path: Path
) -> None:
    rotated = tmp_path / "portrait.mp4"
    try:
        run_ffmpeg(["-display_rotation", "90", "-i", str(source_mp4), "-c", "copy", str(rotated)])
    except Exception:  # pragma: no cover - FFmpeg older than 6.0
        pytest.skip("this FFmpeg cannot set display rotation")
    source_video = probe(rotated).primary_video
    assert source_video is not None
    assert source_video.display_size == (480, 640)

    result = add_video([(rotated, "0-1"), (copy_media(source_mp4), "0-1")])
    output_video = probe(result.output_path).primary_video
    assert output_video is not None
    assert output_video.display_size == (480, 640)


@requires_ffmpeg
def test_copy_mode_cuts_without_reencoding(copy_media: CopyMedia, source_mp4: Path) -> None:
    clip = copy_media(source_mp4)
    result = cut_video(clip, "1-3", copy=True)
    info = probe(result.output_path)
    assert info.primary_video is not None
    assert info.primary_video.codec_name == "h264"
    assert result.duration_seconds is not None and result.duration_seconds >= 1.9
    assert "keyframes" in result.notes[0]


@requires_ffmpeg
def test_copy_mode_refuses_what_it_cannot_do(copy_media: CopyMedia, source_mp4: Path) -> None:
    clip = copy_media(source_mp4)
    before = sorted(clip.parent.iterdir())
    with pytest.raises(CommandSyntaxError, match="single range only"):
        cut_video(clip, "0-1,2-3", copy=True)
    with pytest.raises(InputFileError, match=r"cannot write '\.webm'"):
        cut_video(clip, "0-1", copy=True, output=clip.with_name("x.webm"))
    assert sorted(clip.parent.iterdir()) == before


@requires_vp9
@requires_opus
def test_webm_output(copy_media: CopyMedia, source_mp4: Path, tmp_path: Path) -> None:
    clip = copy_media(source_mp4)
    result = cut_video(clip, "0-1", output=tmp_path / "short.webm")
    info = probe(result.output_path)
    assert "webm" in info.format_name
    assert info.primary_video is not None and info.primary_video.codec_name == "vp9"


@requires_ffmpeg
def test_range_past_the_end_writes_nothing(copy_media: CopyMedia, source_mp4: Path) -> None:
    clip = copy_media(source_mp4)
    before = sorted(clip.parent.iterdir())
    with pytest.raises(CommandSyntaxError, match="past the end; it is only 0:04 long"):
        cut_video(clip, "3-9")
    assert sorted(clip.parent.iterdir()) == before


@requires_ffmpeg
def test_not_a_video(tmp_path: Path, source_wav: Path) -> None:
    fake = tmp_path / "fake.mp4"
    fake.write_bytes(b"definitely not a video" * 100)
    with pytest.raises(InputFileError, match="not a readable video"):
        cut_video(fake, "0-1")
    with pytest.raises(InputFileError, match="no video stream"):
        cut_video(source_wav, "0-1")


def test_missing_ffmpeg_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def missing(media_kind: str = "media") -> tuple[Path, Path]:
        raise MissingDependencyError("ffmpeg", "FFmpeg is not installed.")

    monkeypatch.setattr("ecompress.edit.video.require_ffmpeg", missing)
    with pytest.raises(MissingDependencyError):
        cut_video(tmp_path / "any.mp4", "0-1")


# -- filter graph and probing, no FFmpeg needed ---------------------------------


def _clip(path: str, width: int, height: int, *, audio: bool, fps: float = 30.0) -> _Clip:
    streams = [StreamInfo(0, "video", "h264", width=width, height=height, frame_rate=fps)]
    if audio:
        streams.append(StreamInfo(1, "audio", "aac", channels=2, sample_rate=44_100))
    info = MediaInfo(Path(path), "mp4", 10.0, 1000, None, streams)
    return _Clip(Path(path), info, Segment(2.0, 5.0))


def test_filter_graph_one_file_keeps_its_frame_rate() -> None:
    graph = filter_graph([_clip("a.mp4", 641, 481, audio=True)] * 2, with_audio=True)
    assert "scale=640:480" in graph  # odd sizes rounded down for yuv420p
    assert "fps=" not in graph
    assert graph.endswith("[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]")


def test_filter_graph_mixed_files() -> None:
    clips = [_clip("a.mp4", 1280, 720, audio=False, fps=25), _clip("b.mp4", 640, 480, audio=True)]
    graph = filter_graph(clips, with_audio=True)
    assert "[1:0]scale=1280:720" in graph
    assert graph.count("fps=25") == 2
    assert "anullsrc=channel_layout=stereo:sample_rate=48000,atrim=duration=3.000000" in graph
    assert "[1:1]aresample=48000" in graph


def test_filter_graph_without_audio() -> None:
    graph = filter_graph([_clip("a.mp4", 320, 240, audio=False)], with_audio=False)
    assert graph.endswith("[v0]concat=n=1:v=1:a=0[v]")
    assert "anullsrc" not in graph


@pytest.mark.parametrize(
    ("stream", "degrees"),
    [
        ({"side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}]}, 270),
        ({"side_data_list": [{"side_data_type": "Other"}], "tags": {"rotate": "90"}}, 90),
        ({"tags": {}}, 0),
        ({}, 0),
    ],
)
def test_rotation_parsing(stream: dict[str, object], degrees: int) -> None:
    assert _rotation(stream) == degrees


def test_display_size_swaps_for_portrait() -> None:
    assert StreamInfo(0, "video", "h264", width=1920, height=1080, rotation=90).display_size == (
        1080,
        1920,
    )
    assert StreamInfo(0, "video", "h264", width=1920, height=1080, rotation=180).display_size == (
        1920,
        1080,
    )
