"""Audio backend (FFmpeg).

Quality is maximised in three tiers:

1. **Lossless, if it fits.** A WAV or AIFF source is first re-encoded to FLAC.
   That is a perfect-quality result and usually 40-60% smaller, so it is always
   preferred when the target allows it.
2. **Same codec, lower bitrate.** A lossy source keeps its codec and container
   (MP3 stays MP3, M4A stays AAC, Ogg stays Vorbis) so the user's format is
   preserved.
3. **A more efficient codec.** When the budget falls below roughly 48 kbps,
   MP3 and Vorbis fall apart while Opus still sounds usable, so the encoder is
   switched and the change is reported.

Channel count and sample rate are only reduced when the bitrate alone cannot
reach the target.
"""

from __future__ import annotations

from pathlib import Path

from compress.backends.base import Backend, Job
from compress.errors import ToolExecutionError
from compress.ffmpeg import MediaInfo, first_available_encoder, probe, require_ffmpeg
from compress.process import run_command
from compress.result import MediaType
from compress.search import SearchOutcome, search_proportional

__all__ = ["AudioBackend"]

MAX_ENCODES = 6

#: Container overhead (tags, seek tables) as a fraction of the budget.
CONTAINER_OVERHEAD = 0.01

#: Below this, switch to Opus - MP3/Vorbis are unusable down here.
LOW_BITRATE_THRESHOLD = 48_000

#: Extensions whose contents are uncompressed or losslessly compressed.
_LOSSLESS_EXTENSIONS = frozenset({".wav", ".aiff", ".aif", ".flac"})

_LOSSLESS_CODECS = frozenset(
    {"pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_u8", "flac", "alac"}
)

#: encoder -> (extension, min bitrate, max bitrate)
_CODEC_LIMITS = {
    "libmp3lame": (".mp3", 32_000, 320_000),
    "aac": (".m4a", 24_000, 320_000),
    "libfdk_aac": (".m4a", 24_000, 320_000),
    "aac_mf": (".m4a", 24_000, 320_000),
    "libvorbis": (".ogg", 48_000, 320_000),
    "libopus": (".opus", 12_000, 256_000),
}

_MP3 = ["libmp3lame", "mp3_mf"]
_AAC = ["aac", "libfdk_aac", "aac_mf"]
_VORBIS = ["libvorbis"]
_OPUS = ["libopus"]

#: Sample rates Opus actually accepts.
_OPUS_RATES = (8_000, 12_000, 16_000, 24_000, 48_000)


class _Profile:
    """One (codec, container, channels, sample rate) configuration."""

    __slots__ = ("channels", "codec", "extension", "max_bps", "min_bps", "sample_rate")

    def __init__(
        self,
        codec: str,
        extension: str,
        channels: int,
        sample_rate: int | None,
        min_bps: int,
        max_bps: int,
    ) -> None:
        self.codec = codec
        self.extension = extension
        self.channels = channels
        self.sample_rate = sample_rate
        self.min_bps = min_bps
        self.max_bps = max_bps

    def describe(self) -> str:
        parts = [self.codec]
        parts.append("mono" if self.channels == 1 else f"{self.channels}ch")
        if self.sample_rate:
            parts.append(f"{self.sample_rate / 1000:g} kHz")
        return ", ".join(parts)


class AudioBackend(Backend):
    """Compresses audio with FFmpeg."""

    name = "audio"
    media_type = MediaType.AUDIO

    _ffmpeg: Path
    _info: MediaInfo

    def prepare(self, job: Job) -> bool:
        ffmpeg, _ = require_ffmpeg("an audio file")
        self._ffmpeg = ffmpeg
        self._info = probe(job.input_path)

        stream = self._info.primary_audio
        if stream is None:  # pragma: no cover - detection guards this
            self._outcome.detail = "This file does not contain an audio stream."
            return False

        self.expect_duration(self._info.duration)
        job.reporter.step(
            f"Audio detected ({stream.codec_name}, "
            + (f"{self._info.duration:.1f}s, " if self._info.duration else "")
            + f"{stream.channels or 2}ch, {(stream.sample_rate or 44_100) / 1000:g} kHz)."
        )
        return True

    def run(self, job: Job) -> None:
        info = self._info
        stream = info.primary_audio
        assert stream is not None  # noqa: S101 - guaranteed by prepare()
        duration = info.duration
        channels = stream.channels or 2
        sample_rate = stream.sample_rate or 44_100

        if duration is None or duration <= 0:
            self._outcome.detail = (
                "This audio file has no readable duration, so a target bitrate "
                "cannot be calculated."
            )
            return

        if self._try_lossless(job, stream.codec_name, sample_rate):
            return

        self._lossy_search(job, duration, channels, sample_rate)

    # -- tier 1: lossless --------------------------------------------------

    def _try_lossless(self, job: Job, codec_name: str, sample_rate: int) -> bool:
        """Re-encode to FLAC when the source is lossless and the budget allows."""
        is_lossless = (
            job.detection.extension in _LOSSLESS_EXTENSIONS or codec_name in _LOSSLESS_CODECS
        )
        if not is_lossless or first_available_encoder(["flac"]) is None:
            return False

        # FLAC lands around 50-60% of PCM; below that it is not worth an encode.
        ratio = job.aim_bytes / max(job.input_size_bytes, 1)
        floor = 0.85 if codec_name == "flac" else 0.40
        if ratio < floor:
            return False

        out = job.scratch("lossless.flac")
        args = [
            str(self._ffmpeg), "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", str(job.input_path),
            "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-c:a", "flac", "-compression_level", "12",
            "-sample_fmt", "s16" if sample_rate <= 48_000 else "s32",
            str(out),
        ]  # fmt: skip
        try:
            run_command(args, timeout=job.timeout, check=True, tool="ffmpeg")
        except ToolExecutionError:
            return False

        size = self.measure(
            out,
            parameters={"codec": "flac", "lossless": True},
            label="lossless FLAC",
            keep_as="best.flac",
        )
        if size is None or size >= job.target_bytes:
            return False

        if job.detection.extension != ".flac":
            self._outcome.format_changed = True
            self.note(
                "Converted to FLAC, which is lossless: the audio is bit-for-bit "
                "identical to the original, just stored more efficiently."
            )
        return True

    # -- tiers 2 and 3: lossy ---------------------------------------------

    def _lossy_search(self, job: Job, duration: float, channels: int, sample_rate: int) -> None:
        overhead = int(job.target_bytes * CONTAINER_OVERHEAD)
        budget_bytes = job.aim_bytes - overhead
        if budget_bytes <= 0:
            self._outcome.detail = "The requested size leaves no room for any audio data."
            return

        needed_bps = int(budget_bytes * 8 / duration)
        profiles = self._profiles(job, needed_bps, channels, sample_rate)
        if not profiles:
            self._outcome.detail = (
                "This FFmpeg build has no usable audio encoder "
                "(looked for libmp3lame, aac, libvorbis, libopus)."
            )
            return

        outcome: SearchOutcome[int] = SearchOutcome()
        used = 0
        for index, profile in enumerate(profiles):
            remaining = MAX_ENCODES - used
            if remaining <= 0:
                break
            if index > 0:
                self.note(f"Reduced audio settings to {profile.describe()} to reach the target.")

            def encode(bitrate: int, profile: _Profile = profile) -> int | None:
                return self._encode(job, profile, bitrate)

            before = outcome.evaluations
            search_proportional(
                encode,
                limit=job.target_bytes,
                initial=max(min(needed_bps, profile.max_bps), profile.min_bps),
                minimum=profile.min_bps,
                maximum=profile.max_bps,
                fixed_overhead_bytes=overhead,
                max_evaluations=remaining,
                floor=job.min_bytes,
                outcome=outcome,
            )
            used += outcome.evaluations - before
            if outcome.best is not None:
                self._announce_format(job, profile)
                return

        if not self._outcome.achieved:
            lowest = profiles[-1]
            self._outcome.detail = (
                f"Even at {lowest.describe()} and {lowest.min_bps // 1000} kbps, "
                f"{duration:.1f}s of audio cannot fit in the requested size."
            )

    def _profiles(
        self, job: Job, needed_bps: int, channels: int, sample_rate: int
    ) -> list[_Profile]:
        """Configurations to try, best quality first."""
        primary = self._primary_codec(job)
        profiles: list[_Profile] = []

        if primary is not None and needed_bps >= LOW_BITRATE_THRESHOLD:
            ext, lo, hi = _CODEC_LIMITS[primary]
            profiles.append(_Profile(primary, ext, channels, sample_rate, lo, min(hi, 320_000)))
            if channels > 1:
                profiles.append(_Profile(primary, ext, 1, sample_rate, lo, hi))

        opus = first_available_encoder(_OPUS)
        if opus is not None:
            ext, lo, hi = _CODEC_LIMITS[opus]
            profiles.append(_Profile(opus, ext, min(channels, 2), 48_000, lo, hi))
            if channels > 1:
                profiles.append(_Profile(opus, ext, 1, 48_000, lo, hi))
            profiles.append(_Profile(opus, ext, 1, 24_000, 8_000, hi))
            profiles.append(_Profile(opus, ext, 1, 16_000, 6_000, hi))
        elif primary is not None:
            ext, lo, hi = _CODEC_LIMITS[primary]
            profiles.append(_Profile(primary, ext, 1, min(sample_rate, 22_050), 8_000, hi))
        return profiles

    def _primary_codec(self, job: Job) -> str | None:
        """Encoder that preserves the user's container, when one exists."""
        ext = job.detection.extension
        if ext == ".mp3":
            return first_available_encoder(_MP3)
        if ext in {".m4a", ".aac", ".mp4"}:
            return first_available_encoder(_AAC)
        if ext in {".ogg", ".oga"}:
            return first_available_encoder(_VORBIS) or first_available_encoder(_OPUS)
        if ext == ".opus":
            return first_available_encoder(_OPUS)
        # Lossless or exotic source: MP3 is the most universally playable target.
        return first_available_encoder(_MP3) or first_available_encoder(_AAC)

    def _announce_format(self, job: Job, profile: _Profile) -> None:
        actual = _extension_for(profile)
        if actual == job.detection.extension:
            return
        self._outcome.format_changed = True
        self.note(
            f"Converted to {actual.lstrip('.').upper()} "
            f"({profile.codec}), which reaches the requested size at far better "
            f"quality than '{job.detection.extension}' could."
        )

    # -- encoding ----------------------------------------------------------

    def _encode(self, job: Job, profile: _Profile, bitrate: int) -> int | None:
        ext = _extension_for(profile)
        out = job.scratch(f"a_{profile.codec}_{bitrate}_{profile.channels}ch{ext}")
        if out.exists():
            out.unlink()

        args: list[str] = [
            str(self._ffmpeg), "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", str(job.input_path),
            "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-c:a", profile.codec, "-b:a", str(bitrate),
            "-ac", str(profile.channels),
        ]  # fmt: skip
        rate = _sample_rate_for(profile)
        if rate:
            args += ["-ar", str(rate)]
        if profile.codec == "libopus":
            args += ["-vbr", "on", "-application", "audio"]
        args.append(str(out))

        try:
            run_command(args, timeout=job.timeout, check=True, tool="ffmpeg")
        except ToolExecutionError as exc:
            job.reporter.note(f"encoder rejected these settings ({_first_line(exc.stderr)})")
            return None

        return self.measure(
            out,
            parameters={
                "codec": profile.codec,
                "bitrate": bitrate,
                "channels": profile.channels,
                "sample_rate": rate,
            },
            label=f"{bitrate // 1000} kbps {profile.describe()}",
            keep_as=f"best{ext}",
        )


def _extension_for(profile: _Profile) -> str:
    return profile.extension


def _sample_rate_for(profile: _Profile) -> int | None:
    rate = profile.sample_rate
    if rate is None:
        return None
    if profile.codec == "libopus":
        # Opus only accepts a fixed set of input rates.
        return min(_OPUS_RATES, key=lambda candidate: abs(candidate - rate))
    return rate


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:160]
    return "unknown error"
