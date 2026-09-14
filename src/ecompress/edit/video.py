"""``cut_video`` and ``add_video``: take time ranges from videos and join them.

Every segment is decoded and re-encoded through one FFmpeg filter graph. That
is what makes cuts frame-accurate (a stream copy can only start on a
keyframe, often seconds early) and what lets clips with different
resolutions, frame rates or audio layouts be joined at all.

``copy=True`` is the exception for a single cut: no re-encode, so it is nearly
instant and lossless, at the price of keyframe-aligned edges.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from ecompress.backends.video import (
    _AAC_ENCODERS,
    _H264_ENCODERS,
    _MP3_ENCODERS,
    _OPUS_ENCODERS,
    _VP9_ENCODERS,
)
from ecompress.edit.common import EditResult, check_sources, reserve_output, staged_output
from ecompress.edit.grammar import Command, Segment, format_timestamp, parse_time_ranges
from ecompress.errors import (
    CommandSyntaxError,
    InputFileError,
    MissingDependencyError,
    OutputValidationError,
    ToolExecutionError,
)
from ecompress.ffmpeg import MediaInfo, first_available_encoder, probe, require_ffmpeg
from ecompress.process import run_with_progress
from ecompress.reporting import NullReporter, Reporter
from ecompress.result import MediaType
from ecompress.validation import validate_output

__all__ = ["VIDEO_EXTENSIONS", "run_video"]

#: Containers the editor can write.
VIDEO_EXTENSIONS = frozenset({".mp4", ".m4v", ".mkv", ".mov", ".webm", ".avi"})

#: Visually lossless for H.264; editing should not visibly cost quality.
H264_CRF = 18
VP9_CRF = 31
AUDIO_RATE = 48_000

_FASTSTART = frozenset({".mp4", ".m4v", ".mov"})


@dataclass(frozen=True)
class _Clip:
    path: Path
    info: MediaInfo
    segment: Segment


def run_video(
    command: Command,
    *,
    overwrite: bool = False,
    copy: bool = False,
    reporter: Reporter | None = None,
    timeout: float | None = None,
) -> EditResult:
    """Execute a ``cut_video`` or ``add_video`` command."""
    reporter = reporter or NullReporter()
    started = time.monotonic()
    ffmpeg, _ = require_ffmpeg("video")
    paths = check_sources(command)

    infos: dict[Path, MediaInfo] = {}
    clips: list[_Clip] = []
    for source, path in zip(command.sources, paths, strict=True):
        key = path.resolve()
        info = infos.get(key)
        if info is None:
            info = infos[key] = _probe_video(path)
        # Every range is checked before anything is written.
        for segment in parse_time_ranges(source.spec, info.duration, name=path.name):
            clips.append(_Clip(path, info, segment))
            reporter.step(
                f"{path.name}: {format_timestamp(segment.start)} to "
                f"{format_timestamp(segment.end)} ({format_timestamp(segment.length)})"
            )

    if copy and len(clips) != 1:
        raise CommandSyntaxError(
            "--copy works for cutting a single range only; joining clips needs a re-encode."
        )

    first = paths[0]
    extension = first.suffix.lower() if first.suffix.lower() in VIDEO_EXTENSIONS else ".mp4"
    reserved = reserve_output(
        command,
        paths,
        suffix="_cut" if command.operation == "cut_video" else "_joined",
        extension=first.suffix.lower() if copy else extension,
        writable=frozenset({first.suffix.lower()}) if copy else VIDEO_EXTENSIONS,
        overwrite=overwrite,
    )

    total = sum(clip.segment.length for clip in clips)
    notes: list[str] = []
    with staged_output(reserved) as temp:
        if copy:
            args = _copy_args(ffmpeg, clips[0], temp)
            notes.append(
                "Stream copy cuts on keyframes, so the clip may start slightly "
                "before the requested time."
            )
            reporter.step(f"Copying {format_timestamp(total)} without re-encoding.")
        else:
            args = _encode_args(ffmpeg, clips, temp)
            reporter.step(f"Encoding {format_timestamp(total)} of video.")

        try:
            run_with_progress(
                args,
                total_seconds=total,
                on_progress=lambda done: reporter.progress(done, label=temp.suffix[1:]),
                timeout=timeout,
                tool="ffmpeg",
            )
        finally:
            reporter.progress_done()

        # A stream copy legitimately runs long by up to a GOP, so only an
        # encode is held to the requested duration.
        report = validate_output(temp, MediaType.VIDEO, expected_duration=None if copy else total)
        if not report:
            raise OutputValidationError(
                f"The new video failed validation ({report.reason}); nothing was written."
            )
        duration = probe(temp).duration

    return EditResult(
        operation=command.operation,
        output_path=reserved.path,
        output_size_bytes=reserved.path.stat().st_size,
        sources=tuple(paths),
        elapsed_seconds=time.monotonic() - started,
        duration_seconds=duration,
        notes=tuple(notes),
    )


def _probe_video(path: Path) -> MediaInfo:
    try:
        info = probe(path)
    except ToolExecutionError as exc:
        raise InputFileError(f"{path} is not a readable video.") from exc
    if not info.has_video:
        raise InputFileError(f"{path} has no video stream.")
    return info


def _copy_args(ffmpeg: Path, clip: _Clip, output: Path) -> list[str | Path]:
    video = clip.info.primary_video
    assert video is not None  # noqa: S101 - guaranteed by _probe_video
    args: list[str | Path] = [ffmpeg, "-hide_banner", "-nostdin", "-y"]
    args += ["-ss", f"{clip.segment.start:.6f}", "-i", clip.path]
    args += ["-t", f"{clip.segment.length:.6f}", "-map", f"0:{video.index}", "-map", "0:a?"]
    args += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
    if output.suffix.lower() in _FASTSTART:
        args += ["-movflags", "+faststart"]
    return [*args, "-progress", "pipe:1", "-nostats", output]


def _encode_args(ffmpeg: Path, clips: list[_Clip], output: Path) -> list[str | Path]:
    args: list[str | Path] = [ffmpeg, "-hide_banner", "-nostdin", "-y"]
    for clip in clips:
        if clip.segment.start > 0:
            args += ["-ss", f"{clip.segment.start:.6f}"]
        args += ["-t", f"{clip.segment.length:.6f}", "-i", clip.path]

    with_audio = any(clip.info.has_audio for clip in clips)
    args += ["-filter_complex", filter_graph(clips, with_audio=with_audio), "-map", "[v]"]
    if with_audio:
        args += ["-map", "[a]"]
    args += _codec_args(output.suffix.lower(), with_audio=with_audio)
    return [*args, "-progress", "pipe:1", "-nostats", output]


def filter_graph(clips: list[_Clip], *, with_audio: bool) -> str:
    """One filter graph that normalises every clip and concatenates them.

    Clips are fitted inside the first clip's frame (letterboxed, never
    stretched) and, when they come from different files, brought to its frame
    rate. A clip with no audio gets silence of its own length so the audio
    stays in sync with the picture.
    """
    first = clips[0].info.primary_video
    assert first is not None  # noqa: S101 - guaranteed by _probe_video
    shown_width, shown_height = first.display_size
    width, height = max(shown_width // 2 * 2, 2), max(shown_height // 2 * 2, 2)
    fps = first.frame_rate or 30.0
    mixed_sources = len({clip.path.resolve() for clip in clips}) > 1

    audio_format = f"aformat=sample_fmts=fltp:sample_rates={AUDIO_RATE}:channel_layouts=stereo"
    chains: list[str] = []
    inputs = ""
    for k, clip in enumerate(clips):
        video = clip.info.primary_video
        assert video is not None  # noqa: S101 - guaranteed by _probe_video
        chain = (
            f"[{k}:{video.index}]"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1"
        )
        if mixed_sources:
            chain += f",fps={fps:.6g}"
        chains.append(f"{chain},format=yuv420p[v{k}]")
        inputs += f"[v{k}]"

        if with_audio:
            audio = clip.info.primary_audio
            if audio is not None:
                chains.append(f"[{k}:{audio.index}]aresample={AUDIO_RATE},{audio_format}[a{k}]")
            else:
                chains.append(
                    f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_RATE},"
                    f"atrim=duration={clip.segment.length:.6f},{audio_format}[a{k}]"
                )
            inputs += f"[a{k}]"

    outputs = "[v][a]" if with_audio else "[v]"
    chains.append(f"{inputs}concat=n={len(clips)}:v=1:a={int(with_audio)}{outputs}")
    return ";".join(chains)


def _codec_args(extension: str, *, with_audio: bool) -> list[str]:
    if extension == ".webm":
        video = first_available_encoder(_VP9_ENCODERS)
        audio = first_available_encoder(_OPUS_ENCODERS)
        if video is None:
            raise MissingDependencyError(
                "ffmpeg", "This FFmpeg build cannot write WebM (no VP9 encoder); use .mp4 instead."
            )
        if video == "libvpx-vp9":
            args = ["-c:v", video, "-crf", str(VP9_CRF), "-b:v", "0"]
            args += ["-deadline", "good", "-cpu-used", "4", "-row-mt", "1"]
        else:
            args = ["-c:v", video, "-b:v", "4M"]
        audio_bitrate = "160k"
    else:
        video = first_available_encoder(_H264_ENCODERS)
        if video is None:
            raise MissingDependencyError(
                "ffmpeg", "This FFmpeg build has no H.264 or MPEG-4 video encoder."
            )
        if video == "libx264":
            args = ["-c:v", video, "-preset", "fast", "-crf", str(H264_CRF)]
        elif video == "mpeg4":
            args = ["-c:v", video, "-q:v", "2"]
        else:
            args = ["-c:v", video, "-b:v", "8M"]
        audio = first_available_encoder(_MP3_ENCODERS if extension == ".avi" else _AAC_ENCODERS)
        audio_bitrate = "192k"

    args += ["-pix_fmt", "yuv420p"]
    if with_audio:
        if audio is None:
            raise MissingDependencyError(
                "ffmpeg", f"This FFmpeg build has no audio encoder for {extension} files."
            )
        args += ["-c:a", audio, "-b:a", audio_bitrate]
    if extension in _FASTSTART:
        args += ["-movflags", "+faststart"]
    return args
