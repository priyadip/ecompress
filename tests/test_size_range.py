"""Size ranges: a ceiling that must not be crossed and a floor worth filling."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from ecompress import compress
from ecompress.cli import EXIT_OK, EXIT_USAGE, main
from ecompress.errors import InvalidTargetError
from ecompress.units import SizeRange, parse_size_range

Copier = Callable[..., Path]
Capsys = pytest.CaptureFixture[str]


# -- parsing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["40-50", "40,50", "[40,50]", "[40, 50]", "40..50", "40:50", "  40 - 50  ", "(40,50)"],
)
def test_every_range_spelling_means_the_same(text: str) -> None:
    assert parse_size_range(text) == SizeRange(40_000_000, 50_000_000)


def test_a_plain_number_has_no_floor() -> None:
    assert parse_size_range(50) == SizeRange(None, 50_000_000)
    assert parse_size_range("50") == SizeRange(None, 50_000_000)


def test_sequences_are_accepted() -> None:
    assert parse_size_range((40, 50)) == SizeRange(40_000_000, 50_000_000)
    assert parse_size_range([40, 50]) == SizeRange(40_000_000, 50_000_000)


def test_a_separate_minimum_is_accepted() -> None:
    assert parse_size_range(50, minimum=40) == SizeRange(40_000_000, 50_000_000)


def test_fractional_bounds_stay_exact() -> None:
    assert parse_size_range("0.5-1.5") == SizeRange(500_000, 1_500_000)
    assert parse_size_range("49.9-50") == SizeRange(49_900_000, 50_000_000)


def test_a_floor_at_or_above_the_ceiling_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be below the maximum"):
        parse_size_range("50-40")
    with pytest.raises(ValueError, match="must be below the maximum"):
        parse_size_range("50-50")


def test_range_and_separate_minimum_together_is_rejected() -> None:
    with pytest.raises(ValueError, match="not both"):
        parse_size_range("40-50", minimum=30)
    with pytest.raises(ValueError, match="not both"):
        parse_size_range((40, 50), minimum=30)


def test_a_malformed_range_is_rejected() -> None:
    for bad in ("40-", "-50", "40--50", "a-b", "40-50-60"):
        with pytest.raises(ValueError):
            parse_size_range(bad)


def test_wrong_length_sequence_is_rejected() -> None:
    with pytest.raises(ValueError, match="exactly two values"):
        parse_size_range((10, 20, 30))


def test_contains_is_strict_at_the_top_inclusive_at_the_bottom() -> None:
    window = SizeRange(40, 50)
    assert not window.contains(50), "the ceiling is exclusive"
    assert window.contains(49)
    assert window.contains(40), "the floor is inclusive"
    assert not window.contains(39)


def test_contains_without_a_floor() -> None:
    window = SizeRange(None, 50)
    assert window.contains(1)
    assert not window.contains(50)


# -- the API ---------------------------------------------------------------


def test_a_range_lands_inside_the_window(copy_media: Copier, source_jpg: Path) -> None:
    result = compress(copy_media(source_jpg), "0.30-0.40")

    assert result.output_size_bytes < 400_000
    assert result.output_size_bytes >= 300_000
    assert result.within_requested_range
    assert result.min_size_bytes == 300_000
    assert result.min_size_mb == 0.3


def test_min_mb_keyword_matches_the_range_form(copy_media: Copier, source_jpg: Path) -> None:
    ranged = compress(copy_media(source_jpg, "a.jpg"), "0.30-0.40")
    keyword = compress(copy_media(source_jpg, "b.jpg"), 0.40, min_mb=0.30)

    assert keyword.min_size_bytes == ranged.min_size_bytes
    assert keyword.target_size_bytes == ranged.target_size_bytes


def test_a_floor_raises_quality_versus_a_bare_ceiling(copy_media: Copier, source_jpg: Path) -> None:
    """The whole point: fill the budget instead of undershooting it."""
    bare = compress(copy_media(source_jpg, "bare.jpg"), 0.40)
    floored = compress(copy_media(source_jpg, "floored.jpg"), "0.36-0.40")

    assert floored.output_size_bytes >= 360_000
    assert floored.output_size_bytes >= bare.output_size_bytes


def test_no_floor_keeps_the_old_behaviour(copy_media: Copier, source_jpg: Path) -> None:
    result = compress(copy_media(source_jpg), 0.4)
    assert result.min_size_bytes is None
    assert result.within_requested_range
    assert result.output_size_bytes < 400_000


def test_an_unreachable_floor_still_succeeds_with_a_note(
    copy_media: Copier, source_jpg: Path
) -> None:
    """A floor is a goal, not a licence to pad the file with junk."""
    source = copy_media(source_jpg)
    original = source.stat().st_size

    # Ask for almost the original size as a floor; the encoder cannot inflate.
    result = compress(source, f"{(original - 2000) / 1e6}-{(original - 1000) / 1e6}")

    assert result.output_size_bytes < original - 1000, "the ceiling always holds"
    if not result.within_requested_range:
        assert any("minimum" in note for note in result.notes)


def test_a_file_already_under_the_floor_is_left_alone(copy_media: Copier, source_jpg: Path) -> None:
    source = copy_media(source_jpg)
    before = source.read_bytes()

    result = compress(source, "80-100")

    assert result.skipped
    assert source.read_bytes() == before, "must never pad a file to reach a floor"
    assert any("minimum" in note for note in result.notes)


@pytest.mark.parametrize("bad", ["50-40", "50-50", "0-10", "abc-50"])
def test_invalid_ranges_raise(copy_media: Copier, source_jpg: Path, bad: str) -> None:
    with pytest.raises(InvalidTargetError):
        compress(copy_media(source_jpg), bad)


# -- the CLI ---------------------------------------------------------------


def test_cli_accepts_the_range_form(copy_media: Copier, source_jpg: Path, capsys: Capsys) -> None:
    code = main([str(copy_media(source_jpg)), "0.30-0.40"])
    out = capsys.readouterr().out

    assert code == EXIT_OK
    assert "300.0 KB to 400.0 KB" in out
    assert "Compression successful." in out


def test_cli_min_flag(copy_media: Copier, source_jpg: Path, capsys: Capsys) -> None:
    code = main([str(copy_media(source_jpg)), "0.40", "--min", "0.30"])
    assert code == EXIT_OK
    assert "300.0 KB to 400.0 KB" in capsys.readouterr().out


def test_cli_json_reports_the_window(copy_media: Copier, source_jpg: Path, capsys: Capsys) -> None:
    main([str(copy_media(source_jpg)), "0.30-0.40", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["min_size_bytes"] == 300_000
    assert payload["target_size_bytes"] == 400_000
    assert payload["within_requested_range"] is True
    assert 300_000 <= payload["output_size_bytes"] < 400_000


def test_cli_rejects_a_backwards_range(
    copy_media: Copier, source_jpg: Path, capsys: Capsys
) -> None:
    with pytest.raises(SystemExit) as info:
        main([str(copy_media(source_jpg)), "50-40"])
    assert info.value.code == EXIT_USAGE
    assert "must be below the maximum" in capsys.readouterr().err


def test_cli_help_documents_the_range(capsys: Capsys) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "40-50" in out
