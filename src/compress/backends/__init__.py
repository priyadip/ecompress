"""Compression backends, one per media family."""

from __future__ import annotations

from compress.backends.audio import AudioBackend
from compress.backends.base import Backend, BackendOutcome, Job
from compress.backends.image import ImageBackend
from compress.backends.pdf import PdfBackend
from compress.backends.video import VideoBackend

__all__ = [
    "AudioBackend",
    "Backend",
    "BackendOutcome",
    "ImageBackend",
    "Job",
    "PdfBackend",
    "VideoBackend",
]
