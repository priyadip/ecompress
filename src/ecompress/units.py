"""Size units and human-readable formatting.

The package uses **decimal** megabytes throughout::

    1 MB == 1_000_000 bytes

so a target of ``50`` means the output must be strictly smaller than
``50_000_000`` bytes.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

BYTES_PER_KB = 1_000
BYTES_PER_MB = 1_000_000
BYTES_PER_GB = 1_000_000_000

Number = int | float | str | Decimal

__all__ = [
    "BYTES_PER_GB",
    "BYTES_PER_KB",
    "BYTES_PER_MB",
    "SizeRange",
    "bytes_to_mb",
    "format_mb",
    "format_size",
    "mb_to_bytes",
    "parse_size_range",
]


class SizeRange(NamedTuple):
    """The size window an output must land in.

    ``maximum`` is a hard ceiling: the result must be **strictly** below it.
    ``minimum`` is a quality floor - "do not undershoot this far" - and is
    ``None`` when the caller only gave a ceiling.
    """

    minimum: int | None
    maximum: int

    def contains(self, size_bytes: int) -> bool:
        """Whether ``size_bytes`` satisfies both ends of the window."""
        if size_bytes >= self.maximum:
            return False
        return self.minimum is None or size_bytes >= self.minimum

    def describe(self) -> str:
        if self.minimum is None:
            return f"below {format_size(self.maximum)}"
        return f"between {format_size(self.minimum)} and {format_size(self.maximum)}"


#: ``40-50``, ``40,50``, ``[40, 50]``, ``40..50`` and ``40:50`` all mean the same.
_RANGE_PATTERN = re.compile(
    r"""^\s*[\[(]?\s*
        (?P<low>\d*\.?\d+)
        \s*(?:\.\.|[-,:])\s*
        (?P<high>\d*\.?\d+)
        \s*[\])]?\s*$""",
    re.VERBOSE,
)


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


def parse_size_range(value: object, *, minimum: Number | None = None) -> SizeRange:
    """Read a target size, which may be a single ceiling or a window.

    Accepts ``50``, ``"50"``, ``"40-50"``, ``"[40,50]"``, ``"40..50"``,
    ``(40, 50)`` and ``[40, 50]``. A separate ``minimum`` may be supplied
    instead, which is how ``--min`` reaches this function.

    Raises:
        ValueError: the value is not a usable size, or the floor is not below
            the ceiling.
    """
    low: Number | None
    high: Number

    if isinstance(value, (tuple, list)):
        if minimum is not None:
            raise ValueError(
                "give either a range like '40-50' or a single size with a separate "
                "minimum, not both"
            )
        pair: tuple[object, ...] = tuple(value)
        if len(pair) != 2:
            raise ValueError(f"a size range needs exactly two values, got {len(pair)}")
        low, high = _as_number(pair[0]), _as_number(pair[1])
    elif isinstance(value, str) and (match := _RANGE_PATTERN.match(value)):
        if minimum is not None:
            raise ValueError(
                "give either a range like '40-50' or a single size with a separate "
                "minimum, not both"
            )
        low, high = match.group("low"), match.group("high")
    else:
        low, high = minimum, _as_number(value)

    ceiling = mb_to_bytes(high)
    if low is None:
        return SizeRange(None, ceiling)

    floor = mb_to_bytes(low)
    if floor >= ceiling:
        raise ValueError(
            f"the minimum size ({format_size(floor)}) must be below the maximum "
            f"({format_size(ceiling)})"
        )
    return SizeRange(floor, ceiling)


def _as_number(value: object) -> Number:
    """Narrow an arbitrary object to something ``mb_to_bytes`` can read."""
    if isinstance(value, (int, float, str, Decimal)):
        return value
    raise ValueError(f"{value!r} is not a valid size in MB")


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
