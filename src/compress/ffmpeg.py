"""FFmpeg / FFprobe discovery, capability detection and media probing."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from typing import Any

from compress.errors import MissingDependencyError, ToolExecutionError
from compress.process import run_command

__all__ = [
    "FFmpegTools",
    "MediaInfo",
    "StreamInfo",
    "bundled_binary",
    "clear_ffmpeg_cache",
    "find_ffmpeg_tools",
    "probe",
    "require_ffmpeg",
]

#: Honoured first so users can point at a private build.
ENV_FFMPEG = "COMPRESS_FFMPEG"
ENV_FFPROBE = "COMPRESS_FFPROBE"

#: Distribution that ships ffmpeg *and* ffprobe; a dependency of this package.
_BUNDLED_DISTRIBUTION = "ffmpeg-binaries"

_SYSTEM_HINT = {
    "win32": "winget install Gyan.FFmpeg",
    "darwin": "brew install ffmpeg",
    "linux": "sudo apt install ffmpeg      (or: sudo dnf install ffmpeg)",
}
_SYSTEM_HINT_DEFAULT = "install ffmpeg with your system package manager"


def _install_hint() -> str:
    """Two ways out, easiest first."""
    system = _SYSTEM_HINT.get(sys.platform, _SYSTEM_HINT_DEFAULT)
    return (
        "Normally FFmpeg is installed automatically with this package. This\n"
        "platform has no prebuilt FFmpeg wheel, so install it one of these ways:\n\n"
        f'  1.  pip install "compress-cli[ffmpeg]"\n'
        f"  2.  {system}"
    )


def _extra_search_dirs() -> list[Path]:
    """Well-known install locations that are not always on PATH."""
    dirs: list[Path] = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            winget = Path(local) / "Microsoft" / "WinGet" / "Packages"
            if winget.is_dir():
                try:
                    for package in winget.iterdir():
                        if not package.is_dir() or "FFmpeg" not in package.name:
                            continue
                        dirs.extend(p for p in package.rglob("bin") if p.is_dir())
                except OSError:  # pragma: no cover - permission edge case
                    pass
        for base in (r"C:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin"):
            dirs.append(Path(base))
    else:
        dirs.extend([Path("/usr/local/bin"), Path("/opt/homebrew/bin"), Path("/snap/bin")])
    return dirs


def _ensure_executable(path: Path) -> Path | None:
    """Make sure ``path`` can actually be run, adding the bit if it is missing."""
    if os.access(path, os.X_OK):
        return path
    try:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:  # pragma: no cover - read-only site-packages
        return None
    return path if os.access(path, os.X_OK) else None


def bundled_binary(binary: str) -> Path | None:
    """Find ``binary`` inside the ``ffmpeg-binaries`` distribution, if installed.

    ``ffmpeg-binaries`` is a normal dependency of this package, so on the common
    platforms ``pip install compress-cli`` already put ffmpeg and ffprobe on
    disk. They are located through the installed distribution's file list rather
    than by importing it: that package's import name is ``ffmpeg``, which
    collides with the unrelated ``ffmpeg-python`` package, and metadata lookup
    sidesteps the ambiguity entirely.
    """
    wanted = {binary.lower(), f"{binary.lower()}.exe"}
    try:
        dist = metadata.distribution(_BUNDLED_DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return None
    except (OSError, ValueError):  # pragma: no cover - damaged metadata
        return None

    fallback: Path | None = None
    for entry in dist.files or ():
        if entry.name.lower() not in wanted:
            continue
        try:
            # locate_file() is typed as a minimal path protocol, so go via str().
            path = Path(str(dist.locate_file(entry))).resolve()
        except (OSError, ValueError, TypeError):  # pragma: no cover - defensive
            continue
        if not path.is_file():
            continue
        # Prefer the real executable shipped inside the package directory over
        # the console-script shim pip drops into Scripts/ or bin/.
        if "binaries" in {part.lower() for part in path.parts}:
            return _ensure_executable(path)
        fallback = fallback or path
    return _ensure_executable(fallback) if fallback is not None else None


def _locate(binary: str, env_var: str) -> Path | None:
    """Find one FFmpeg executable.

    Order: explicit override, then the copy installed alongside this package,
    then ``PATH``, then well-known install locations. The bundled copy is
    preferred over ``PATH`` so behaviour is identical on every machine; set
    ``COMPRESS_FFMPEG`` / ``COMPRESS_FFPROBE`` to override that.
    """
    override = os.environ.get(env_var)
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return candidate
        found = shutil.which(override)
        if found:
            return Path(found)

    bundled = bundled_binary(binary)
    if bundled is not None:
        return bundled

    found = shutil.which(binary)
    if found:
        return Path(found)

    exe = f"{binary}.exe" if sys.platform == "win32" else binary
    for directory in _extra_search_dirs():
        candidate = directory / exe
        if candidate.is_file():
            return candidate
    return None


@dataclass(frozen=True)
class FFmpegTools:
    """Paths to the FFmpeg tool-chain, if present."""

    ffmpeg: Path | None
    ffprobe: Path | None

    @property
    def available(self) -> bool:
        return self.ffmpeg is not None and self.ffprobe is not None

    def require(self, media_kind: str = "media") -> tuple[Path, Path]:
        """Return ``(ffmpeg, ffprobe)`` or raise a friendly error."""
        missing = [
            name
            for name, path in (("ffmpeg", self.ffmpeg), ("ffprobe", self.ffprobe))
            if path is None
        ]
        if missing:
            tools = " and ".join(missing)
            raise MissingDependencyError(
                "ffmpeg",
                f"FFmpeg is not installed ({tools} not found).\n\n"
                f"This file is {media_kind} and requires FFmpeg.\n\n"
                f"{_install_hint()}\n\n"
                f"You can also set {ENV_FFMPEG} / {ENV_FFPROBE} to the full "
                "paths of the executables.",
            )
        assert self.ffmpeg is not None and self.ffprobe is not None  # noqa: S101 - narrowing
        return self.ffmpeg, self.ffprobe


@lru_cache(maxsize=1)
def find_ffmpeg_tools() -> FFmpegTools:
    """Locate ffmpeg and ffprobe (cached)."""
    return FFmpegTools(
        ffmpeg=_locate("ffmpeg", ENV_FFMPEG),
        ffprobe=_locate("ffprobe", ENV_FFPROBE),
    )


def require_ffmpeg(media_kind: str = "media") -> tuple[Path, Path]:
    """Shorthand for ``find_ffmpeg_tools().require(...)``."""
    return find_ffmpeg_tools().require(media_kind)


@lru_cache(maxsize=1)
def _encoder_names() -> frozenset[str]:
    tools = find_ffmpeg_tools()
    if tools.ffmpeg is None:
        return frozenset()
    try:
        result = run_command([tools.ffmpeg, "-hide_banner", "-encoders"], timeout=60, check=False)
    except (MissingDependencyError, ToolExecutionError):  # pragma: no cover - defensive
        return frozenset()
    names: set[str] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        # Encoder rows look like: " V....D libx264   libx264 H.264 / AVC ..."
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return frozenset(names)


def has_encoder(name: str) -> bool:
    """Whether FFmpeg was built with the named encoder."""
    return name in _encoder_names()


def first_available_encoder(candidates: list[str]) -> str | None:
    """The first encoder in ``candidates`` that this FFmpeg build supports."""
    available = _encoder_names()
    for name in candidates:
        if name in available:
            return name
    return None


def clear_ffmpeg_cache() -> None:
    """Reset cached discovery (used by the test-suite)."""
    find_ffmpeg_tools.cache_clear()
    _encoder_names.cache_clear()


@dataclass(frozen=True)
class StreamInfo:
    """One stream inside a container."""

    index: int
    codec_type: str
    codec_name: str
    width: int | None = None
    height: int | None = None
    channels: int | None = None
    sample_rate: int | None = None
    bit_rate: int | None = None
    frame_rate: float | None = None
    disposition: dict[str, int] = field(default_factory=dict)
    nb_frames: int | None = None

    @property
    def is_attached_picture(self) -> bool:
        """Cover art embedded in an audio file, not real video."""
        if self.codec_type != "video":
            return False
        if self.disposition.get("attached_pic"):
            return True
        return self.codec_name in {"mjpeg", "png", "bmp"} and (self.nb_frames or 0) <= 1


@dataclass(frozen=True)
class MediaInfo:
    """Normalised view of ``ffprobe`` output."""

    path: Path
    format_name: str
    duration: float | None
    size_bytes: int | None
    bit_rate: int | None
    streams: list[StreamInfo] = field(default_factory=list)

    @property
    def video_streams(self) -> list[StreamInfo]:
        return [s for s in self.streams if s.codec_type == "video" and not s.is_attached_picture]

    @property
    def audio_streams(self) -> list[StreamInfo]:
        return [s for s in self.streams if s.codec_type == "audio"]

    @property
    def has_video(self) -> bool:
        return bool(self.video_streams)

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_streams)

    @property
    def primary_video(self) -> StreamInfo | None:
        streams = self.video_streams
        return streams[0] if streams else None

    @property
    def primary_audio(self) -> StreamInfo | None:
        streams = self.audio_streams
        return streams[0] if streams else None


def _as_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None  # drop NaN


def _parse_rate(value: Any) -> float | None:
    """Parse ffprobe's ``"30000/1001"`` rational frame-rate strings."""
    if not value or not isinstance(value, str):
        return None
    if "/" in value:
        num, _, den = value.partition("/")
        try:
            numerator, denominator = float(num), float(den)
        except ValueError:
            return None
        if denominator == 0:
            return None
        return numerator / denominator
    return _as_float(value)


def probe(path: Path, *, timeout: float = 120.0) -> MediaInfo:
    """Inspect a media file with ``ffprobe``.

    Raises:
        MissingDependencyError: FFmpeg is not installed.
        ToolExecutionError: the file could not be parsed as media.
    """
    _, ffprobe = require_ffmpeg()
    result = run_command(
        [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            "-i",
            path,
        ],
        timeout=timeout,
        check=True,
        tool="ffprobe",
    )
    try:
        payload: dict[str, Any] = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ToolExecutionError("ffprobe", 0, f"unreadable ffprobe output: {exc}") from exc

    fmt: dict[str, Any] = payload.get("format") or {}
    streams: list[StreamInfo] = []
    for raw in payload.get("streams") or []:
        streams.append(
            StreamInfo(
                index=_as_int(raw.get("index")) or 0,
                codec_type=str(raw.get("codec_type") or "unknown"),
                codec_name=str(raw.get("codec_name") or "unknown"),
                width=_as_int(raw.get("width")),
                height=_as_int(raw.get("height")),
                channels=_as_int(raw.get("channels")),
                sample_rate=_as_int(raw.get("sample_rate")),
                bit_rate=_as_int(raw.get("bit_rate")),
                frame_rate=_parse_rate(raw.get("avg_frame_rate"))
                or _parse_rate(raw.get("r_frame_rate")),
                disposition=dict(raw.get("disposition") or {}),
                nb_frames=_as_int(raw.get("nb_frames")),
            )
        )

    duration = _as_float(fmt.get("duration"))
    if duration is None:
        # Some containers only carry per-stream durations.
        candidates = [
            _as_float(raw.get("duration"))
            for raw in (payload.get("streams") or [])
            if _as_float(raw.get("duration")) is not None
        ]
        duration = max((c for c in candidates if c is not None), default=None)

    return MediaInfo(
        path=path,
        format_name=str(fmt.get("format_name") or ""),
        duration=duration if (duration or 0) > 0 else None,
        size_bytes=_as_int(fmt.get("size")),
        bit_rate=_as_int(fmt.get("bit_rate")),
        streams=streams,
    )
