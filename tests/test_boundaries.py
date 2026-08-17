"""Boundary behaviour around the target size.

The rule under test everywhere in this file is ``<``, never ``<=``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from ecompress import compress
from ecompress.errors import TargetNotAchievableError
from ecompress.units import mb_to_bytes

Copier = Callable[..., Path]


def test_target_equal_to_the_original_size_still_compresses(
    copy_media: Copier, source_jpg: Path
) -> None:
    """``target == original`` is not "already smaller"; it must be reduced."""
    source = copy_media(source_jpg)
    original = source.stat().st_size

    result = compress(source, original / 1_000_000)

    assert not result.skipped
    assert result.output_size_bytes < original
    assert result.output_size_bytes < result.target_size_bytes


def test_target_one_byte_below_the_original(copy_media: Copier, source_jpg: Path) -> None:
    source = copy_media(source_jpg)
    original = source.stat().st_size

    result = compress(source, (original - 1) / 1_000_000)

    assert not result.skipped
    assert result.output_size_bytes < original - 1


def test_target_one_byte_above_the_original_is_skipped(
    copy_media: Copier, source_jpg: Path
) -> None:
    source = copy_media(source_jpg)
    original = source.stat().st_size

    result = compress(source, (original + 1) / 1_000_000)

    assert result.skipped
    assert result.output_path == source


@pytest.mark.parametrize("target_mb", [0.05, 0.1, 0.25, 0.5, 1, 1.5, 2])
def test_a_range_of_targets_all_hold_the_guarantee(
    copy_media: Copier, source_jpg: Path, target_mb: float
) -> None:
    source = copy_media(source_jpg)
    if source.stat().st_size < mb_to_bytes(target_mb):
        pytest.skip("input is already below this target")

    result = compress(source, target_mb)

    limit = mb_to_bytes(target_mb)
    assert result.output_size_bytes < limit
    assert result.output_path.stat().st_size < limit


@pytest.mark.parametrize("target", [1, 1.0, "1", "1.0"])
def test_equivalent_target_spellings_agree(
    copy_media: Copier, source_jpg: Path, target: object
) -> None:
    result = compress(copy_media(source_jpg), target)  # type: ignore[arg-type]
    assert result.target_size_bytes == 1_000_000
    assert result.output_size_bytes < 1_000_000


def test_fractional_targets_are_exact(copy_media: Copier, source_jpg: Path) -> None:
    result = compress(copy_media(source_jpg), 0.499)
    assert result.target_size_bytes == 499_000
    assert result.output_size_bytes < 499_000


def test_a_very_large_target_is_a_skip(copy_media: Copier, source_jpg: Path) -> None:
    result = compress(copy_media(source_jpg), 100_000)
    assert result.skipped


def test_a_very_small_target_fails_cleanly(copy_media: Copier, source_jpg: Path) -> None:
    with pytest.raises(TargetNotAchievableError) as info:
        compress(copy_media(source_jpg), 0.00001)  # 10 bytes

    assert info.value.target_bytes == 10


def test_safety_margin_leaves_headroom_without_wasting_quality(
    copy_media: Copier, source_jpg: Path
) -> None:
    """The result should sit under the ceiling but not far under it."""
    result = compress(copy_media(source_jpg), 0.5)

    assert result.output_size_bytes < 500_000
    assert result.output_size_bytes > 350_000, "too much quality was given away"


def test_returned_result_always_satisfies_the_contract(
    copy_media: Copier, source_jpg: Path
) -> None:
    for target_mb in (0.05, 0.2, 0.8):
        result = compress(copy_media(source_jpg, f"copy-{target_mb}.jpg"), target_mb)
        assert result.target_achieved
        assert result.output_size_bytes < result.target_size_bytes
        assert result.output_size_mb < target_mb
