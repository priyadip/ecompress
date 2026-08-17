"""The report behind ``ecompress --check``.

Answers one question: is everything this package needs actually installed, and
where did it come from? Nothing here changes state - it only looks.
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

from ecompress.backends.pdf import find_ghostscript
from ecompress.errors import MissingDependencyError, ToolExecutionError
from ecompress.ffmpeg import bundled_binary, find_ffmpeg_tools, first_available_encoder
from ecompress.process import run_command

__all__ = ["Diagnostics", "collect", "render"]

#: Encoders worth reporting on, grouped by what they let the user do.
_ENCODER_GROUPS = (
    ("H.264 video (.mp4, .mkv, .mov)", ["libx264", "h264_mf", "libopenh264", "mpeg4"]),
    ("VP9 video (.webm)", ["libvpx-vp9", "libvpx"]),
    ("AAC audio (.m4a)", ["aac", "libfdk_aac", "aac_mf"]),
    ("MP3 audio (.mp3)", ["libmp3lame", "mp3_mf"]),
    ("Opus audio (.opus)", ["libopus"]),
    ("Vorbis audio (.ogg)", ["libvorbis"]),
    ("FLAC audio (.flac)", ["flac"]),
)


@dataclass
class Diagnostics:
    """What was found on this machine."""

    package_version: str
    python_version: str
    platform_name: str
    ffmpeg: Path | None = None
    ffprobe: Path | None = None
    ffmpeg_version: str = ""
    ffmpeg_source: str = ""
    encoders: dict[str, str | None] = field(default_factory=dict)
    pillow_version: str = ""
    image_formats: list[str] = field(default_factory=list)
    pikepdf_version: str = ""
    ghostscript: Path | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _distribution_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return ""


def _tool_version(executable: Path) -> str:
    try:
        result = run_command([executable, "-version"], timeout=30, check=False)
    except (MissingDependencyError, ToolExecutionError):  # pragma: no cover - defensive
        return ""
    first = (result.stdout or result.stderr).strip().splitlines()
    return first[0] if first else ""


def collect() -> Diagnostics:
    """Inspect the environment."""
    report = Diagnostics(
        package_version=_distribution_version("ecompress") or "unknown",
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}."
        f"{sys.version_info.micro}",
        platform_name=f"{platform.system()} {platform.release()} ({platform.machine()})",
    )

    tools = find_ffmpeg_tools()
    report.ffmpeg = tools.ffmpeg
    report.ffprobe = tools.ffprobe

    if tools.ffmpeg is None or tools.ffprobe is None:
        missing = [
            name
            for name, path in (("ffmpeg", tools.ffmpeg), ("ffprobe", tools.ffprobe))
            if path is None
        ]
        report.problems.append(
            f"{' and '.join(missing)} not found - video and audio compression "
            "will not work. Images and PDFs are unaffected."
        )
    else:
        report.ffmpeg_version = _tool_version(tools.ffmpeg)
        report.ffmpeg_source = (
            "bundled with this package"
            if bundled_binary("ffmpeg") == tools.ffmpeg
            else "found on this system"
        )
        for label, candidates in _ENCODER_GROUPS:
            report.encoders[label] = first_available_encoder(candidates)
        unavailable = [label for label, found in report.encoders.items() if found is None]
        if unavailable:
            report.problems.append("this FFmpeg build cannot encode: " + ", ".join(unavailable))

    report.pillow_version = _distribution_version("pillow")
    try:
        from PIL import Image

        Image.init()  # Image.SAVE is populated lazily
        report.image_formats = [
            fmt for fmt in ("JPEG", "PNG", "WEBP", "AVIF", "GIF") if fmt in Image.SAVE
        ]
    except ImportError:  # pragma: no cover - pillow is a hard dependency
        report.problems.append("Pillow is not installed - images cannot be compressed.")

    report.pikepdf_version = _distribution_version("pikepdf")
    if not report.pikepdf_version:  # pragma: no cover - pikepdf is a hard dependency
        report.problems.append("pikepdf is not installed - PDFs cannot be compressed.")

    report.ghostscript = find_ghostscript()
    return report


def render(report: Diagnostics) -> str:
    """Format the report the way ``ecompress --check`` prints it."""
    lines: list[str] = [
        "ecompress " + report.package_version,
        f"Python {report.python_version} on {report.platform_name}",
        "",
        "Video and audio (FFmpeg)",
    ]

    if report.ffmpeg and report.ffprobe:
        lines.append(f"  OK       {report.ffmpeg_version or 'ffmpeg'}")
        lines.append(f"           {report.ffmpeg_source}")
        lines.append(f"           ffmpeg:  {report.ffmpeg}")
        lines.append(f"           ffprobe: {report.ffprobe}")
        lines.append("")
        for label, encoder in report.encoders.items():
            status = f"OK       {label} ({encoder})" if encoder else f"MISSING  {label}"
            lines.append(f"  {status}")
    else:
        lines.append("  MISSING  ffmpeg / ffprobe were not found")
        lines.append("           Images and PDFs still work.")

    lines += [
        "",
        "Images (Pillow)",
        f"  OK       Pillow {report.pillow_version}"
        if report.pillow_version
        else "  MISSING  Pillow",
        f"           formats: {', '.join(report.image_formats) or 'none'}",
        "",
        "PDF (pikepdf)",
        f"  OK       pikepdf {report.pikepdf_version}"
        if report.pikepdf_version
        else "  MISSING  pikepdf",
    ]

    if report.ghostscript:
        lines.append(f"  OK       Ghostscript (optional): {report.ghostscript}")
    else:
        lines.append("  --       Ghostscript not found (optional; only helps stubborn PDFs)")

    lines.append("")
    if report.ok:
        lines.append("Everything is installed. All supported file types will work.")
    else:
        lines.append("Problems found:")
        lines.extend(f"  - {problem}" for problem in report.problems)

    return "\n".join(lines) + "\n"
