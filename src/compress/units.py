"""Size units and human-readable formatting.

The package uses **decimal** megabytes throughout::

    1 MB == 1_000_000 bytes

so a target of ``50`` means the output must be strictly smaller than
``50_000_000`` bytes.
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation

BYTES_PER_KB = 1_000
BYTES_PER_MB = 1_000_000
BYTES_PER_GB = 1_000_000_000

Number = int | float | str | Decimal

__all__ = [
    "BYTES_PER_GB",
    "BYTES_PER_KB",
    "BYTES_PER_MB",
    "bytes_to_mb",
    "format_mb",
    "format_size",
    "mb_to_bytes",
]


def mb_to_bytes(mb: Number) -> int:
    """Convert decimal megabytes to a whole number of bytes.

    ``Decimal`` is used so that values such as ``49.9`` map to exactly
    ``49_900_000`` bytes rather than to a float-rounding artefact.

    Raises:
        ValueError: if the value is not a finite number greater than zero.
    """
    try:
        value = Decimal(str(mb))
    except (InvalidOperation, ValueError) as exc:  # pragma: no cover - defensive
        raise ValueError(f"{mb!r} is not a valid size in MB") from exc

    if not value.is_finite():
        raise ValueError(f"{mb!r} is not a finite size in MB")
    if value <= 0:
        raise ValueError(f"target size must be greater than 0 MB, got {mb!r}")

    as_bytes = int((value * BYTES_PER_MB).to_integral_value(rounding="ROUND_HALF_UP"))
    return max(as_bytes, 1)


def bytes_to_mb(size_bytes: int) -> float:
    """Convert bytes to decimal megabytes."""
    return size_bytes / BYTES_PER_MB


def format_size(size_bytes: int) -> str:
    """Format a byte count the way the CLI reports it (decimal units)."""
    if size_bytes < 0:
        raise ValueError("size cannot be negative")
    if size_bytes < BYTES_PER_KB:
        return f"{size_bytes} B"
    if size_bytes < BYTES_PER_MB:
        return f"{size_bytes / BYTES_PER_KB:.1f} KB"
    if size_bytes < BYTES_PER_GB:
        return f"{size_bytes / BYTES_PER_MB:.1f} MB"
    return f"{size_bytes / BYTES_PER_GB:.2f} GB"


def format_mb(mb: float) -> str:
    """Format a megabyte value without noisy trailing zeros."""
    if not math.isfinite(mb):  # pragma: no cover - defensive
        return str(mb)
    if abs(mb - round(mb)) < 1e-9:
        return f"{round(mb)} MB"
    return f"{mb:.2f}".rstrip("0").rstrip(".") + " MB"
