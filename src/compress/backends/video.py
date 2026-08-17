"""Video backend (FFmpeg).

The size of an encode is close to linear in the video bitrate, so rather than
blindly binary-searching we seed the search analytically and then correct from
measurements:

1. Probe duration, resolution, frame rate and the audio track.
2. Split the byte budget into ``audio + container overhead + video``.
3. Pick the largest resolution whose bits-per-pixel stays in a watchable range
   for the video budget - dropping resolution beats starving a large frame.
4. Encode, **measure the real file**, and correct the bitrate from the measured
   payload (:func:`compress.search.search_proportional`).
5. If nothing fits at that resolution, step down the ladder and repeat.

Codec choice follows the container so the user's extension is preserved:
MP4/MOV/MKV get H.264 + AAC, WebM gets VP9 + Opus, AVI gets H.264 + MP3.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from compress.backends.base import Backend, Job
from compress.errors import ToolExecutionError
from compress.ffmpeg import MediaInfo, first_available_encoder, probe, require_ffmpeg
from compress.process import run_command
from compress.result import MediaType
from compress.search import SearchOutcome, search_discrete_ladder, search_proportional

__all__ = ["VideoBackend"]

#: Encode budget across every resolution step.
MAX_ENCODES = 7

#: Below this the picture stops being watchable at any resolution.
MIN_VIDEO_BITRATE = 24_000

#: Fraction of the byte budget reserved for container overhead (indexes, moov).
CONTAINER_OVERHEAD = 0.015

#: Standard heights to step down through, descending.
HEIGHT_LADDER: tuple[int, ...] = (2160, 1440, 1080, 900, 720, 540, 480, 360, 270, 180, 144)

#: Bits per pixel per frame that still looks acceptable for H.264 at `medium`.
ACCEPTABLE_BPP = 0.05

#: CRF ladder used only when the duration is unknown and bitrate maths is impossible.
CRF_LADDER: tuple[int, ...] = (51, 48, 45, 42, 39, 36, 33, 30, 28, 26, 24, 22)

_H264_ENCODERS = ["libx264", "h264_mf", "libopenh264", "mpeg4"]
_VP9_ENCODERS = ["libvpx-vp9", "libvpx"]
_AAC_ENCODERS = ["aac", "libfdk_aac", "aac_mf"]
_OPUS_ENCODERS = ["libopus", "libvorbis"]
_MP3_ENCODERS = ["libmp3lame", "mp3_mf"]


class _Plan:
    """Codec and container decisions for one job."""

    __slots__ = ("audio_codec", "audio_max", "container_ext", "extra", "video_codec")

    def __init__(
        self,
        video_codec: str,
        audio_codec: str | None,
        container_ext: str,
        audio_max: int,
        extra: Sequence[str] = (),
    ) -> None:
        self.video_codec = video_codec
        self.audio_codec = audio_codec
        self.container_ext = container_ext
        self.audio_max = audio_max
        self.extra = list(extra)


class VideoBackend(Backend):
    """Compresses video with FFmpeg."""

    name = "video"
    media_type = MediaType.VIDEO

    _ffmpeg: Path
    _info: MediaInfo

    def prepare(self, job: Job) -> bool:
        ffmpeg, _ = require_ffmpeg("a video")
        self._ffmpeg = ffmpeg
        self._info = probe(job.input_path)

        video = self._info.primary_video
        if video is None:  # pragma: no cover - detection routes these to audio
            self._outcome.detail = "This file does not contain a video stream."
            return False

        self.expect_duration(self._info.duration)
        job.reporter.step(
            f"Video detected ({video.width or 0}x{video.height or 0}"
            + (f", {self._info.duration:.1f}s" if self._info.duration else "")
            + (f", {video.frame_rate:.0f} fps" if video.frame_rate else "")
            + ")."
        )
        return True

    def run(self, job: Job) -> None:
        info = self._info
        video = info.primary_video
        assert video is not None  # noqa: S101 - guaranteed by prepare()
        width = video.width or 0
        height = video.height or 0
        fps = video.frame_rate or 30.0
        duration = info.duration

        plan = self._plan(job, info)
        if plan.video_codec == "":
            self._outcome.detail = (
                "This FFmpeg build has no usable video encoder "
                "(looked for libx264, libvpx-vp9, mpeg4)."
            )
            return
        if plan.container_ext != job.detection.extension:
            self._outcome.format_changed = True
            self.note(
                f"Container changed to '{plan.container_ext}' because "
                f"'{job.detection.extension}' cannot hold the chosen codecs."
            )

        if duration is None or duration <= 0:
            self.note("Duration is unknown; falling back to a quality-based search.")
            self._crf_search(job, plan, width, height)
            return

        self._bitrate_search(job, plan, info, width, height, fps, duration)

    # -- planning ----------------------------------------------------------

    def _plan(self, job: Job, info: MediaInfo) -> _Plan:
        ext = job.detection.extension or ".mp4"
        has_audio = info.has_audio

        if ext == ".webm":
            video = first_available_encoder(_VP9_ENCODERS) or ""
            audio = first_available_encoder(_OPUS_ENCODERS) if has_audio else None
            if video:
                return _Plan(
                    video,
                    audio,
                    ".webm",
                    128_000,
                    ["-deadline", "good", "-cpu-used", "4", "-row-mt", "1"],
                )
            # No VP9: fall through to an MP4 with H.264.
            ext = ".mp4"

        video = first_available_encoder(_H264_ENCODERS) or ""
        if ext == ".avi":
            audio = first_available_encoder(_MP3_ENCODERS) if has_audio else None
            return _Plan(video, audio, ".avi", 128_000)

        audio = first_available_encoder(_AAC_ENCODERS) if has_audio else None
        if ext in {".mp4", ".m4v", ".mov", ".mkv"}:
            return _Plan(video, audio, ext, 128_000)
        # Anything else (flv, wmv, mpg, ts, 3gp...) is remuxed into MP4.
        return _Plan(video, audio, ".mp4", 128_000)

    def _audio_bitrate(self, job: Job, info: MediaInfo, plan: _Plan, duration: float) -> int:
        """Carve an audio budget out of the total, leaving room for video."""
        if plan.audio_codec is None:
            return 0
        stream = info.primary_audio
        channels = stream.channels if stream and stream.channels else 2
        total_bps = job.aim_bytes * 8 / duration
        floor = 32_000 if channels > 1 else 24_000
        ceiling = plan.audio_max if channels > 1 else int(plan.audio_max * 0.7)
        if stream and stream.bit_rate:
            ceiling = min(ceiling, stream.bit_rate)
        chosen = int(total_bps * 0.15)
        chosen = max(floor, min(chosen, ceiling))
        # Never let audio eat so much that no watchable video budget remains.
        if total_bps - chosen < MIN_VIDEO_BITRATE * 2:
            chosen = max(16_000, int(total_bps * 0.25))
        return chosen

    # -- search strategies -------------------------------------------------

    def _bitrate_search(
        self,
        job: Job,
        plan: _Plan,
        info: MediaInfo,
        width: int,
        height: int,
        fps: float,
        duration: float,
    ) -> None:
        audio_bps = self._audio_bitrate(job, info, plan, duration)
        audio_bytes = int(audio_bps * duration / 8)
        overhead_bytes = int(job.target_bytes * CONTAINER_OVERHEAD)
        fixed_bytes = audio_bytes + overhead_bytes

        video_budget_bytes = job.aim_bytes - fixed_bytes
        if video_budget_bytes <= 0:
            self._outcome.detail = (
                f"A {duration:.1f}s clip needs at least "
                f"{_kbps(audio_bps)} for audio alone, which already exceeds the "
                "requested size."
            )
            return

        initial_bps = int(video_budget_bytes * 8 / duration)
        source_bps = _source_video_bitrate(info)
        maximum_bps = min(source_bps, initial_bps * 4) if source_bps else initial_bps * 4

        ladder = _resolution_ladder(width, height)
        start = _choose_resolution_index(ladder, fps, initial_bps)
        if start > 0:
            target_w, target_h = ladder[start]
            self.note(
                f"Scaling down to {target_w}x{target_h}: "
                f"{_kbps(initial_bps)} is not enough for {width}x{height}."
            )

        outcome: SearchOutcome[int] = SearchOutcome()
        used = 0
        for index in range(start, len(ladder)):
            remaining = MAX_ENCODES - used
            if remaining <= 0:
                break
            dims = ladder[index]
            if index > start:
                self.note(f"Reduced resolution to {dims[0]}x{dims[1]} to reach the target.")

            def encode(bitrate: int, dims: tuple[int, int] = dims) -> int | None:
                return self._encode(job, plan, bitrate=bitrate, audio_bps=audio_bps, dims=dims)

            before = outcome.evaluations
            search_proportional(
                encode,
                limit=job.target_bytes,
                initial=initial_bps,
                minimum=MIN_VIDEO_BITRATE,
                maximum=max(maximum_bps, MIN_VIDEO_BITRATE),
                fixed_overhead_bytes=fixed_bytes,
                max_evaluations=remaining,
                outcome=outcome,
            )
            used += outcome.evaluations - before
            if outcome.best is not None:
                return

        if not self._outcome.achieved:
            self._outcome.detail = (
                f"Even at {ladder[-1][0]}x{ladder[-1][1]} and the minimum usable "
                f"bitrate ({_kbps(MIN_VIDEO_BITRATE)} video"
                + (f" + {_kbps(audio_bps)} audio" if audio_bps else "")
                + f"), a {duration:.1f}s clip cannot fit in the requested size."
            )

    def _crf_search(self, job: Job, plan: _Plan, width: int, height: int) -> None:
        """Quality-based fallback for sources with no readable duration."""
        dims = (width, height) if width and height else None

        def encode(crf: int) -> int | None:
            return self._encode(job, plan, crf=crf, audio_bps=96_000, dims=dims)

        search_discrete_ladder(
            CRF_LADDER,
            encode,
            limit=job.target_bytes,
            max_evaluations=MAX_ENCODES,
        )
        if not self._outcome.achieved:
            self._outcome.detail = (
                "Even at the lowest usable quality this video stays above the requested size."
            )

    # -- encoding ----------------------------------------------------------

    def _encode(
        self,
        job: Job,
        plan: _Plan,
        *,
        audio_bps: int,
        bitrate: int | None = None,
        crf: int | None = None,
        dims: tuple[int, int] | None = None,
    ) -> int | None:
        tag = f"b{bitrate}" if bitrate is not None else f"crf{crf}"
        if dims:
            tag += f"_{dims[0]}x{dims[1]}"
        out = job.scratch(f"v_{tag}{plan.container_ext}")
        if out.exists():
            out.unlink()

        args: list[str] = [
            str(self._ffmpeg),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(job.input_path),
            "-map",
            "0:v:0",
        ]
        if plan.audio_codec:
            args += ["-map", "0:a:0?"]
        args += ["-sn", "-dn", "-map_chapters", "-1"]

        args += ["-c:v", plan.video_codec]
        if plan.video_codec.startswith("libx26"):
            args += ["-preset", "medium"]
        args += list(plan.extra)

        if bitrate is not None:
            args += [
                "-b:v",
                str(bitrate),
                "-maxrate",
                str(int(bitrate * 1.45)),
                "-bufsize",
                str(int(bitrate * 2)),
            ]
        else:
            args += ["-crf", str(crf)]

        if dims:
            args += ["-vf", f"scale={dims[0]}:{dims[1]}:flags=lanczos"]
        args += ["-pix_fmt", "yuv420p"]

        if plan.audio_codec:
            args += ["-c:a", plan.audio_codec, "-b:a", str(max(audio_bps, 16_000))]
        else:
            args += ["-an"]

        if plan.container_ext in {".mp4", ".m4v", ".mov"}:
            args += ["-movflags", "+faststart"]
        args.append(str(out))

        try:
            run_command(args, timeout=job.timeout, check=True, tool="ffmpeg")
        except ToolExecutionError as exc:
            job.reporter.note(f"encoder rejected these settings ({_first_line(exc.stderr)})")
            return None

        label = _kbps(bitrate) if bitrate is not None else f"crf {crf}"
        if dims:
            label += f" @ {dims[0]}x{dims[1]}"
        return self.measure(
            out,
            parameters={
                "video_bitrate": bitrate,
                "crf": crf,
                "audio_bitrate": audio_bps or None,
                "width": dims[0] if dims else None,
                "height": dims[1] if dims else None,
            },
            label=label,
            keep_as=f"best{plan.container_ext}",
        )


def _source_video_bitrate(info: MediaInfo) -> int | None:
    video = info.primary_video
    if video and video.bit_rate:
        return video.bit_rate
    if info.bit_rate:
        audio = info.primary_audio
        audio_bps = audio.bit_rate if audio and audio.bit_rate else 0
        remainder = info.bit_rate - audio_bps
        if remainder > 0:
            return remainder
    return None


def _even(value: int) -> int:
    """H.264 and VP9 need even dimensions for 4:2:0 chroma."""
    return max(2, value - (value % 2))


def _resolution_ladder(width: int, height: int) -> list[tuple[int, int]]:
    """Source resolution first, then progressively smaller standard heights."""
    if width <= 0 or height <= 0:
        return [(0, 0)]
    aspect = width / height
    ladder: list[tuple[int, int]] = [(_even(width), _even(height))]
    for target_h in HEIGHT_LADDER:
        if target_h >= height:
            continue
        ladder.append((_even(round(target_h * aspect)), _even(target_h)))
    return ladder


def _choose_resolution_index(ladder: Sequence[tuple[int, int]], fps: float, video_bps: int) -> int:
    """Largest resolution whose bits-per-pixel stays watchable at this bitrate."""
    rate = fps if fps and fps > 0 else 30.0
    for index, (width, height) in enumerate(ladder):
        if width <= 0 or height <= 0:
            return index
        bpp = video_bps / (width * height * rate)
        if bpp >= ACCEPTABLE_BPP:
            return index
    return len(ladder) - 1


def _kbps(bits_per_second: int) -> str:
    if bits_per_second >= 1_000_000:
        return f"{bits_per_second / 1_000_000:.2f} Mbps"
    return f"{bits_per_second / 1000:.0f} kbps"


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:160]
    return "unknown error"
