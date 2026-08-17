"""Media type detection.

Detection uses the file's *content* first (magic bytes) and only falls back to
the extension when the content is inconclusive. Containers that can hold either
audio or video (MP4, MKV, WebM, Ogg, AVI, MOV) are disambiguated with ffprobe so
that, for example, an ``.mp4`` holding nothing but an AAC track is treated as
audio.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from compress.errors import (
    InputFileError,
    MissingDependencyError,
    ToolExecutionError,
    UnsupportedFormatError,
)
from compress.ffmpeg import find_ffmpeg_tools, probe
from compress.result import MediaType

__all__ = [
    "AUDIO_EXTENSIONS",
    "IMAGE_EXTENSIONS",
    "VIDEO_EXTENSIONS",
    "Detection",
    "detect_media_type",
    "sniff_container",
]

IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".webp", ".avif", ".bmp", ".tif", ".tiff", ".gif"}
)
VIDEO_EXTENSIONS = frozenset(
    {
        ".mp4", ".m4v", ".mkv", ".mov", ".webm", ".avi",
        ".wmv", ".flv", ".mpg", ".mpeg", ".ts", ".3gp",
    }
)  # fmt: skip
AUDIO_EXTENSIONS = frozenset(
    {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wma", ".aiff", ".aif"}
)
PDF_EXTENSIONS = frozenset({".pdf"})

#: Containers whose payload decides whether they are audio or video.
_AMBIGUOUS_EXTENSIONS = frozenset(
    {".mp4", ".m4v", ".mkv", ".mov", ".webm", ".avi", ".ogg", ".oga", ".3gp", ".ts"}
)

_HEADER_BYTES = 4096


@dataclass(frozen=True)
class Detection:
    """What the detector concluded about a file."""

    media_type: MediaType
    container: str
    """Short label for the detected container, e.g. ``"mp4"`` or ``"png"``."""
    extension: str
    """The input's lower-cased extension, including the dot."""
    notes: list[str] = field(default_factory=list)


def _read_header(path: Path) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.read(_HEADER_BYTES)
    except OSError as exc:
        raise InputFileError(f"Could not read {path}: {exc}") from exc


def sniff_container(header: bytes) -> str | None:
    """Identify a container from its magic bytes, or ``None`` if unknown."""
    if len(header) < 4:
        return None

    if header[:5] == b"%PDF-" or b"%PDF-" in header[:1024]:
        return "pdf"
    if header[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if header[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if header[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if header[:2] == b"BM":
        return "bmp"
    if header[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if header[:4] == b"RIFF" and len(header) >= 12:
        riff = header[8:12]
        if riff == b"WEBP":
            return "webp"
        if riff == b"WAVE":
            return "wav"
        if riff == b"AVI ":
            return "avi"
    if header[:4] == b"\x1a\x45\xdf\xa3":
        return "matroska"
    if header[:4] == b"OggS":
        return "ogg"
    if header[:4] == b"fLaC":
        return "flac"
    if header[:4] == b"FORM" and len(header) >= 12 and header[8:12] in (b"AIFF", b"AIFC"):
        return "aiff"
    if header[:3] == b"ID3":
        return "mp3"
    if header[:2] in (b"\xff\xfb", b"\xff\xfa", b"\xff\xf3", b"\xff\xf2", b"\xff\xe3"):
        return "mp3"
    if header[:4] == b"\xff\xf1" or header[:2] == b"\xff\xf9":
        return "aac"
    if header[:4] == b"\x30\x26\xb2\x75":
        return "asf"

    if len(header) >= 12 and header[4:8] == b"ftyp":
        brand = header[8:12]
        major = brand.decode("ascii", errors="replace").strip().lower()
        if major in {"avif", "avis"}:
            return "avif"
        if major in {"heic", "heix", "heif", "hevc", "mif1", "msf1"}:
            return "heif"
        if major.startswith("m4a"):
            return "m4a"
        return "mp4"

    return None


_CONTAINER_TO_TYPE = {
    "pdf": MediaType.PDF,
    "png": MediaType.IMAGE,
    "jpeg": MediaType.IMAGE,
    "gif": MediaType.IMAGE,
    "bmp": MediaType.IMAGE,
    "tiff": MediaType.IMAGE,
    "webp": MediaType.IMAGE,
    "avif": MediaType.IMAGE,
    "heif": MediaType.IMAGE,
    "wav": MediaType.AUDIO,
    "flac": MediaType.AUDIO,
    "mp3": MediaType.AUDIO,
    "aac": MediaType.AUDIO,
    "m4a": MediaType.AUDIO,
    "aiff": MediaType.AUDIO,
    "avi": MediaType.VIDEO,
    "asf": MediaType.VIDEO,
}

#: Containers that need a probe before we know audio vs. video.
_CONTAINER_AMBIGUOUS = frozenset({"mp4", "matroska", "ogg", "avi", "m4a"})

#: What an ambiguous container most likely is when probing is unavailable.
_CONTAINER_ASSUMED = {
    "mp4": MediaType.VIDEO,
    "matroska": MediaType.VIDEO,
    "ogg": MediaType.AUDIO,
}


def _type_from_extension(extension: str) -> MediaType | None:
    if extension in PDF_EXTENSIONS:
        return MediaType.PDF
    if extension in IMAGE_EXTENSIONS:
        return MediaType.IMAGE
    if extension in VIDEO_EXTENSIONS:
        return MediaType.VIDEO
    if extension in AUDIO_EXTENSIONS:
        return MediaType.AUDIO
    return None


def detect_media_type(path: Path, *, allow_probe: bool = True) -> Detection:
    """Work out which backend should handle ``path``.

    Args:
        path: the file to inspect.
        allow_probe: run ``ffprobe`` to separate audio from video inside shared
            containers. Set to ``False`` for a contents-only check that never
            needs FFmpeg installed.

    Raises:
        UnsupportedFormatError: the file is not a supported media type.
        InputFileError: the file could not be read.
    """
    extension = path.suffix.lower()
    header = _read_header(path)
    container = sniff_container(header)
    notes: list[str] = []

    ext_type = _type_from_extension(extension)
    sniffed_type = _CONTAINER_TO_TYPE.get(container or "")

    if container is None and ext_type is None:
        raise UnsupportedFormatError(_unsupported_message(path, extension))

    # Content wins over the extension when the two disagree unambiguously.
    if (
        sniffed_type is not None
        and ext_type is not None
        and sniffed_type is not ext_type
        and container is not None
        and container not in _CONTAINER_AMBIGUOUS
    ):
        notes.append(
            f"The extension says '{extension}' but the contents are {container.upper()}; "
            f"treating it as {sniffed_type.value}."
        )
        return Detection(sniffed_type, container or "", extension, notes)

    needs_probe = (container in _CONTAINER_AMBIGUOUS) or (
        container is None and extension in _AMBIGUOUS_EXTENSIONS
    )
    if needs_probe and allow_probe:
        probed = _probe_media_type(path)
        if probed is not None:
            return Detection(probed, container or extension.lstrip("."), extension, notes)

    resolved = sniffed_type or ext_type or _CONTAINER_ASSUMED.get(container or "")
    if resolved is None:
        raise UnsupportedFormatError(_unsupported_message(path, extension))

    if container is None:
        notes.append(
            f"Could not recognise the contents of this file; going by the '{extension}' extension."
        )
    return Detection(resolved, container or extension.lstrip("."), extension, notes)


def _probe_media_type(path: Path) -> MediaType | None:
    """Use ffprobe to decide audio vs. video, or ``None`` if it cannot say."""
    if not find_ffmpeg_tools().available:
        return None
    try:
        info = probe(path)
    except (MissingDependencyError, ToolExecutionError):
        return None
    if info.has_video:
        return MediaType.VIDEO
    if info.has_audio:
        return MediaType.AUDIO
    return None


def _unsupported_message(path: Path, extension: str) -> str:
    label = f"'{extension}' files" if extension else "this file"
    return (
        f"Unsupported file type: {path.name}\n\n"
        f"{label} cannot be compressed by this tool.\n\n"
        "Supported types:\n"
        "  Images: .jpg .jpeg .png .webp .avif .bmp .tif .tiff .gif\n"
        "  Video:  .mp4 .mkv .mov .webm .avi .m4v .wmv .flv .mpg .ts .3gp\n"
        "  Audio:  .mp3 .wav .m4a .aac .flac .ogg .opus .wma .aiff\n"
        "  PDF:    .pdf"
    )
