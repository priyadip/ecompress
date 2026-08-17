"""Decimal-megabyte arithmetic and formatting."""

from __future__ import annotations

import pytest

from compress.units import BYTES_PER_MB, bytes_to_mb, format_mb, format_size, mb_to_bytes


def test_one_mb_is_one_million_bytes() -> None:
    assert BYTES_PER_MB == 1_000_000
    assert mb_to_bytes(1) == 1_000_000


@pytest.mark.parametrize(
    ("mb", "expected"),
    [
        (50, 50_000_000),
        (50.0, 50_000_000),
        (49.9, 49_900_000),
        (1, 1_000_000),
        (0.5, 500_000),
        (0.001, 1_000),
        (2.5, 2_500_000),
        ("50", 50_000_000),
        ("0.25", 250_000),
    ],
)
def test_mb_to_bytes_is_exact(mb: object, expected: int) -> None:
    """Float rounding must not leak in: 49.9 MB is exactly 49,900,000 bytes."""
    assert mb_to_bytes(mb) == expected  # type: ignore[arg-type]


def test_float_artefacts_do_not_leak() -> None:
    # 49.9 * 1_000_000 == 49900000.000000004 in binary floating point.
    assert mb_to_bytes(49.9) == 49_900_000
    assert mb_to_bytes(0.1) == 100_000
    assert mb_to_bytes(0.7) == 700_000


@pytest.mark.parametrize("bad", [0, -1, -0.5, "0", float("nan"), float("inf")])
def test_rejects_non_positive_and_non_finite(bad: object) -> None:
    with pytest.raises(ValueError):
        mb_to_bytes(bad)  # type: ignore[arg-type]


def test_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        mb_to_bytes("fifty")


def test_tiny_targets_floor_at_one_byte() -> None:
    assert mb_to_bytes(1e-9) == 1


def test_bytes_to_mb_round_trips() -> None:
    assert bytes_to_mb(50_000_000) == 50.0
    assert bytes_to_mb(mb_to_bytes(49.9)) == 49.9


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (999, "999 B"),
        (1_000, "1.0 KB"),
        (500_000, "500.0 KB"),
        (1_000_000, "1.0 MB"),
        (82_400_000, "82.4 MB"),
        (1_000_000_000, "1.00 GB"),
    ],
)
def test_format_size(size: int, expected: str) -> None:
    assert format_size(size) == expected


def test_format_size_rejects_negative() -> None:
    with pytest.raises(ValueError):
        format_size(-1)


@pytest.mark.parametrize(
    ("mb", "expected"),
    [(50, "50 MB"), (50.0, "50 MB"), (49.9, "49.9 MB"), (0.5, "0.5 MB"), (1.25, "1.25 MB")],
)
def test_format_mb_drops_trailing_zeros(mb: float, expected: str) -> None:
    assert format_mb(mb) == expected
