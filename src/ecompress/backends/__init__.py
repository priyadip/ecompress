"""Compression backends, one per media family."""

from __future__ import annotations

from ecompress.backends.audio import AudioBackend
from ecompress.backends.base import Backend, BackendOutcome, Job
from ecompress.backends.image import ImageBackend
from ecompress.backends.pdf import PdfBackend
from ecompress.backends.video import VideoBackend

__all__ = [
    "AudioBackend",
    "Backend",
    "BackendOutcome",
    "ImageBackend",
    "Job",
    "PdfBackend",
    "VideoBackend",
]
