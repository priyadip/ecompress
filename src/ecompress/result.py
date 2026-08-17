"""Public result types returned by :func:`compress.compress`."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ecompress.units import bytes_to_mb

__all__ = ["Attempt", "CompressionResult", "MediaType"]


class MediaType(str, enum.Enum):
    """The broad kind of file that determines which backend is used."""

    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    PDF = "pdf"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class Attempt:
    """A single measured encode produced while searching for the best quality."""

    index: int
    size_bytes: int
    parameters: dict[str, Any] = field(default_factory=dict)
    accepted: bool = False
    valid: bool = True
    note: str = ""

    @property
    def size_mb(self) -> float:
        return bytes_to_mb(self.size_bytes)


@dataclass(frozen=True)
class CompressionResult:
    """Outcome of a successful (or intentionally skipped) compression run.

    ``output_size_bytes < target_size_bytes`` always holds for a returned
    result: when the target cannot be met a
    :class:`compress.errors.TargetNotAchievableError` is raised instead.
    """

    input_path: Path
    output_path: Path
    input_size_bytes: int
    output_size_bytes: int
    target_size_bytes: int
    media_type: MediaType
    min_size_bytes: int | None = None
    """Quality floor requested via a size range, if any."""
    attempts: list[Attempt] = field(default_factory=list)
    target_achieved: bool = True
    skipped: bool = False
    format_changed: bool = False
    backend: str = ""
    notes: list[str] = field(default_factory=list)

    # -- derived values ---------------------------------------------------

    @property
    def saved_bytes(self) -> int:
        """Bytes saved. Zero when the file was left untouched."""
        return max(self.input_size_bytes - self.output_size_bytes, 0)

    @property
    def reduction_percent(self) -> float:
        """Percentage of the original size that was removed."""
        if self.input_size_bytes <= 0:
            return 0.0
        return self.saved_bytes / self.input_size_bytes * 100.0

    @property
    def input_size_mb(self) -> float:
        return bytes_to_mb(self.input_size_bytes)

    @property
    def output_size_mb(self) -> float:
        return bytes_to_mb(self.output_size_bytes)

    @property
    def target_size_mb(self) -> float:
        return bytes_to_mb(self.target_size_bytes)

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def min_size_mb(self) -> float | None:
        """The requested floor in MB, or ``None`` when no range was given."""
        return None if self.min_size_bytes is None else bytes_to_mb(self.min_size_bytes)

    @property
    def within_requested_range(self) -> bool:
        """Whether the result satisfies both ends of the requested window.

        The ceiling always holds. This is ``False`` only when a floor was asked
        for and the output legitimately could not reach it.
        """
        if self.output_size_bytes >= self.target_size_bytes and not self.skipped:
            return False
        return self.min_size_bytes is None or self.output_size_bytes >= self.min_size_bytes

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serialisable view, used by ``ecompress --json``."""
        return {
            "input_path": str(self.input_path),
            "output_path": str(self.output_path),
            "input_size_bytes": self.input_size_bytes,
            "output_size_bytes": self.output_size_bytes,
            "target_size_bytes": self.target_size_bytes,
            "min_size_bytes": self.min_size_bytes,
            "within_requested_range": self.within_requested_range,
            "input_size_mb": round(self.input_size_mb, 6),
            "output_size_mb": round(self.output_size_mb, 6),
            "target_size_mb": round(self.target_size_mb, 6),
            "saved_bytes": self.saved_bytes,
            "reduction_percent": round(self.reduction_percent, 4),
            "media_type": self.media_type.value,
            "backend": self.backend,
            "attempts": self.attempt_count,
            "target_achieved": self.target_achieved,
            "skipped": self.skipped,
            "format_changed": self.format_changed,
            "notes": list(self.notes),
        }

    def __post_init__(self) -> None:
        if self.output_size_bytes < 0 or self.input_size_bytes < 0:
            raise ValueError("sizes cannot be negative")
