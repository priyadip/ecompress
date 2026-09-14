"""The editing command language: parsing, page ranges and time ranges."""

from __future__ import annotations

from pathlib import Path

import pytest

from ecompress.edit.grammar import (
    Command,
    Segment,
    Source,
    format_timestamp,
    parse_command,
    parse_page_ranges,
    parse_time_ranges,
    parse_timestamp,
)
from ecompress.errors import CommandSyntaxError

# -- commands -----------------------------------------------------------------


def test_add_pdf_with_quoted_paths_and_output() -> None:
    command = parse_command(
        'add_pdf pdf1("C:/My Documents/a.pdf")[2-9] pdf2("D:/b.pdf")[7-16] -> output("D:/out dir")'
    )
    assert command.operation == "add_pdf"
    assert command.sources == (
        Source("pdf1", Path("C:/My Documents/a.pdf"), "2-9"),
        Source("pdf2", Path("D:/b.pdf"), "7-16"),
    )
    assert command.output == Path("D:/out dir")


def test_output_is_optional() -> None:
    command = parse_command('cut_pdf pdf("book.pdf")[7-16]')
    assert command.output is None
    assert command.sources[0].spec == "7-16"


def test_single_quotes_and_backslash_paths() -> None:
    command = parse_command(r"cut_video video('C:\Videos\my movie.mp4')[01:20-05:40]")
    assert command.sources[0].path == Path(r"C:\Videos\my movie.mp4")
    assert command.sources[0].spec == "01:20-05:40"


def test_unquoted_paths_as_windows_powershell_delivers_them() -> None:
    # PowerShell 5.1 strips the inner quotes and splits on spaces; the CLI
    # rejoins with single spaces, which must parse to the same command.
    mangled = " ".join(
        ["pdf1(C:/My", "Docs/a.pdf)[2-9]", "pdf2(D:/b.pdf)[1]", "->", "output(D:/o)"]
    )
    command = parse_command(mangled, operation="add_pdf")
    assert [source.path for source in command.sources] == [
        Path("C:/My Docs/a.pdf"),
        Path("D:/b.pdf"),
    ]
    assert command.output == Path("D:/o")


def test_unquoted_path_may_contain_parentheses() -> None:
    command = parse_command("cut_pdf pdf(C:/Report (final).pdf)[1-2]")
    assert command.sources[0].path == Path("C:/Report (final).pdf")


def test_output_without_arrow_and_whitespace_everywhere() -> None:
    command = parse_command('  cut_pdf   pdf ( "a.pdf" )  [ 1 - 3 ]   output( "out" ) ')
    assert command.sources[0].spec == " 1 - 3 "
    assert command.output == Path("out")


def test_operation_may_come_from_the_caller() -> None:
    assert parse_command('pdf("a.pdf")[1]', operation="cut_pdf").operation == "cut_pdf"
    assert parse_command('cut_pdf pdf("a.pdf")[1]', operation="cut_pdf").operation == "cut_pdf"


@pytest.mark.parametrize(
    ("text", "operation", "message"),
    [
        ("", None, "empty"),
        ('pdf("a.pdf")[1]', None, "must start with an operation"),
        ('merge_pdf pdf("a.pdf")[1]', None, "Unknown operation 'merge_pdf'"),
        ('cut_video video("a.mp4")[1-2]', "cut_pdf", "This is the cut_pdf command"),
        ('cut_pdf video("a.mp4")[1-2]', None, "takes pdf(...) inputs"),
        ('add_video pdf1("a.pdf") pdf2("b.pdf")', None, "takes video(...) inputs"),
        ('cut_pdf pdf("a.pdf")', None, "needs a range"),
        ('cut_pdf pdf1("a.pdf")[1] pdf2("b.pdf")[1]', None, "exactly one pdf"),
        ('add_pdf pdf("a.pdf")[1]', None, "two or more"),
        ("cut_pdf", None, "No input"),
        ('cut_pdf pdf("a.pdf")[1] ->', None, "must be followed by output"),
        ('cut_pdf pdf("a.pdf")[1] -> -> output("o")', None, "more than once"),
        ('cut_pdf -> output("o") pdf("a.pdf")[1]', None, "Nothing may follow"),
        ('add_pdf pdf1("a.pdf") -> pdf2("b.pdf")', None, "before '->'"),
        ('cut_pdf pdf("a.pdf")[1] output("o")[2]', None, "not a [range]"),
        ('cut_pdf pdf("a.pdf")[1-2', None, "closing ']'"),
        ('cut_pdf pdf("a.pdf)[1]', None, 'closing "'),
        ('cut_pdf pdf("a.pdf" x)[1]', None, "expected ')'"),
        ("cut_pdf pdf(a.pdf[1]", None, "missing ')'"),
        ('cut_pdf pdf("")[1]', None, "path is empty"),
        ("cut_pdf 7-16", None, "Expected something like pdf"),
    ],
)
def test_malformed_commands_explain_themselves(
    text: str, operation: str | None, message: str
) -> None:
    with pytest.raises(
        CommandSyntaxError,
        match=message.replace("(", r"\(").replace(")", r"\)").replace("[", r"\["),
    ):
        parse_command(text, operation=operation)


def test_command_validates_on_construction() -> None:
    with pytest.raises(CommandSyntaxError, match="Unknown operation"):
        Command("split_pdf", (Source("pdf", Path("a.pdf"), "1"),))
    with pytest.raises(CommandSyntaxError, match="Unknown operation"):
        parse_command('pdf("a.pdf")[1]', operation="split_pdf")
    assert Command("cut_video", (Source("video", Path("a.mp4"), "1-2"),)).kind == "video"


# -- page ranges --------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("7", [6]),
        ("2-4", [1, 2, 3]),
        ("2-3,5,9-10", [1, 2, 4, 8, 9]),
        ("8-", [7, 8, 9]),
        ("8-end", [7, 8, 9]),
        ("-2", [0, 1]),
        ("last", [9]),
        ("3-1", [2, 1, 0]),
        ("1,1", [0, 0]),
        (" 2 - 3 , 4 ", [1, 2, 3]),
        ("all", list(range(10))),
        (None, list(range(10))),
    ],
)
def test_page_ranges(spec: str | None, expected: list[int]) -> None:
    assert parse_page_ranges(spec, 10) == expected


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("", "empty"),
        ("0", "page 0 does not exist; it has 10 pages"),
        ("11", "page 11 does not exist"),
        ("2-11", "page 11 does not exist"),
        ("2,,3", "empty entry"),
        ("two", "'two' is not a page number"),
        ("1.5", "not a page number"),
    ],
)
def test_bad_page_ranges(spec: str, message: str) -> None:
    with pytest.raises(CommandSyntaxError, match=message):
        parse_page_ranges(spec, 10)


def test_single_page_document_wording() -> None:
    with pytest.raises(CommandSyntaxError, match="it has 1 page\\."):
        parse_page_ranges("2", 1, name="one.pdf")


# -- time ranges --------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("130", 130.0),
        ("130.5", 130.5),
        ("02:10", 130.0),
        ("90:00", 5400.0),
        ("00:02:10", 130.0),
        ("1:02:03.250", 3723.25),
    ],
)
def test_timestamps(text: str, seconds: float) -> None:
    assert parse_timestamp(text) == pytest.approx(seconds)


@pytest.mark.parametrize("text", ["01:75", "1:60:00", "1:00:60", "abc", "1:2:3:4", "-5", ""])
def test_bad_timestamps(text: str) -> None:
    with pytest.raises(CommandSyntaxError):
        parse_timestamp(text)


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("00:10-01:20", [Segment(10, 80)]),
        ("130-330", [Segment(130, 330)]),
        ("01:20-", [Segment(80, 600)]),
        ("01:20-end", [Segment(80, 600)]),
        ("-00:30", [Segment(0, 30)]),
        ("0-10, 20-30", [Segment(0, 10), Segment(20, 30)]),
        ("590-600.4", [Segment(590, 600)]),  # a hair past the end is clamped
        ("all", [Segment(0, 600)]),
        (None, [Segment(0, 600)]),
    ],
)
def test_time_ranges(spec: str | None, expected: list[Segment]) -> None:
    assert parse_time_ranges(spec, 600.0) == expected


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("", "empty"),
        ("10", "not a range"),
        ("1-2-3", "not a range"),
        ("20-10", "end must be after the start"),
        ("10-10", "end must be after the start"),
        ("600-700", "past the end; it is only 10:00 long"),
        ("0-11:00", "11:00 is past the end"),
    ],
)
def test_bad_time_ranges(spec: str, message: str) -> None:
    with pytest.raises(CommandSyntaxError, match=message):
        parse_time_ranges(spec, 600.0)


def test_unknown_duration_needs_explicit_ends() -> None:
    assert parse_time_ranges("5-10", None) == [Segment(5, 10)]
    with pytest.raises(CommandSyntaxError, match="duration is unknown"):
        parse_time_ranges("5-", None)
    with pytest.raises(CommandSyntaxError, match="duration is unknown"):
        parse_time_ranges(None, None)


def test_segment_length() -> None:
    assert Segment(1.5, 4.0).length == 2.5


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0:00"), (130, "2:10"), (4.5, "0:04.5"), (3723.25, "1:02:03.25"), (-1, "0:00")],
)
def test_format_timestamp(seconds: float, text: str) -> None:
    assert format_timestamp(seconds) == text
