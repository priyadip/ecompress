"""The editing command language: parsing, page ranges and time ranges."""

from __future__ import annotations

import re
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


def test_the_documented_examples() -> None:
    add = parse_command('add_pdf   "D:/a.pdf"[2-9] "D:/b.pdf"[7-16] -> "D:/out"')
    assert add == Command(
        "add_pdf",
        (Source(Path("D:/a.pdf"), "2-9"), Source(Path("D:/b.pdf"), "7-16")),
        Path("D:/out"),
    )

    cut = parse_command('cut_pdf   "C:/My Documents/book.pdf"[2-5,8-12,20] -> "D:/out"')
    assert cut.sources == (Source(Path("C:/My Documents/book.pdf"), "2-5,8-12,20"),)

    join = parse_command('add_video "a.mp4"[00:00-00:30] "b.mp4"[01:10-02:00] -> "D:/out"')
    assert [source.spec for source in join.sources] == ["00:00-00:30", "01:10-02:00"]

    clip = parse_command('cut_video "movie.mp4"[00:02:10-00:05:30]')
    assert clip.output is None
    assert clip.sources == (Source(Path("movie.mp4"), "00:02:10-00:05:30"),)


EXPECTED = Command(
    "add_pdf",
    (Source(Path("D:/My Docs/a.pdf"), "2-9"), Source(Path("D:/b.pdf"), "7-16")),
    Path("D:/out dir"),
)


@pytest.mark.parametrize(
    "argv",
    [
        # bash, cmd, and PowerShell after --%: the shell removes the quotes.
        ["D:/My Docs/a.pdf[2-9]", "D:/b.pdf[7-16]", "->", "D:/out dir"],
        # Windows PowerShell 5.1 given the whole line in single quotes.
        ["D:/My", "Docs/a.pdf[2-9] D:/b.pdf[7-16] -> D:/out", "dir"],
        # A shell that keeps the inner quotes, or an argument list with no shell.
        ['"D:/My Docs/a.pdf"[2-9] "D:/b.pdf"[7-16] -> "D:/out dir"'],
        ['"D:/My Docs/a.pdf"[2-9]', '"D:/b.pdf"[7-16]', "->", '"D:/out dir"'],
    ],
)
def test_every_way_a_shell_delivers_the_line(argv: list[str]) -> None:
    assert parse_command(" ".join(argv), operation="add_pdf") == EXPECTED


def test_single_quotes_backslashes_and_no_spaces_around_the_arrow() -> None:
    command = parse_command(r"cut_video 'C:\Videos\my movie.mp4' [01:20-05:40]->'D:\out'")
    assert command.sources == (Source(Path(r"C:\Videos\my movie.mp4"), "01:20-05:40"),)
    assert command.output == Path(r"D:\out")


def test_arrow_and_apostrophe_inside_unquoted_names() -> None:
    command = parse_command("cut_pdf D:/Tom's a->b.pdf[1-2] -> D:/out")
    assert command.sources == (Source(Path("D:/Tom's a->b.pdf"), "1-2"),)
    assert command.output == Path("D:/out")


def test_unquoted_inputs_without_ranges_split_where_files_exist(tmp_path: Path) -> None:
    folder = tmp_path / "My Docs"
    folder.mkdir()
    first, second = folder / "a b.pdf", folder / "c d.pdf"
    for path in (first, second):
        path.write_bytes(b"%PDF")

    command = parse_command(
        f"{first} {second} {second}[2] -> {tmp_path / 'out dir'}", operation="add_pdf"
    )
    assert command.sources == (Source(first), Source(second), Source(second, "2"))
    assert command.output == tmp_path / "out dir"


def test_brackets_in_a_folder_name_prefer_the_real_file(tmp_path: Path) -> None:
    folder = tmp_path / "photos [2024] trip"
    folder.mkdir()
    pdf = folder / "a.pdf"
    pdf.write_bytes(b"%PDF")
    assert parse_command(f"cut_pdf {pdf}[1-2]").sources == (Source(pdf, "1-2"),)


def test_operation_may_come_from_the_caller() -> None:
    assert parse_command('"a.pdf"[1]', operation="cut_pdf").operation == "cut_pdf"
    assert parse_command('cut_pdf "a.pdf"[1]', operation="cut_pdf").operation == "cut_pdf"
    assert parse_command("movie.mp4[1-2]", operation="cut_video").sources[0].path == Path(
        "movie.mp4"
    )
    my_file = parse_command("my file.pdf[1]", operation="cut_pdf")
    assert my_file.sources == (Source(Path("my file.pdf"), "1"),)


@pytest.mark.parametrize(
    ("text", "operation", "message"),
    [
        ("", None, "The command is empty"),
        ('"a.pdf"[1]', None, "must start with an operation"),
        ('merge_pdf "a.pdf"[1]', None, "Unknown operation 'merge_pdf'"),
        ('cut_video "a.mp4"[1-2]', "cut_pdf", "This is the cut_pdf command"),
        ('cut_pdf "a.pdf"', None, "needs a range in [...]"),
        ('cut_pdf "a.pdf"[1] "b.pdf"[1]', None, "takes exactly one file"),
        ('add_pdf "a.pdf"[1]', None, "two or more files"),
        ("cut_pdf", None, "No input file given"),
        ('cut_pdf -> "out"', None, "No input file given"),
        ('cut_pdf "a.pdf"[1] ->', None, "'->' must be followed by a folder"),
        ('cut_pdf "a.pdf"[1] -> ""', None, "'->' must be followed by a folder"),
        ('cut_pdf "a.pdf"[1] -> "o" -> "p"', None, "'->' appears more than once"),
        ('cut_pdf "a.pdf"[1] -> "o" [2]', None, "Nothing may follow the output folder"),
        ('cut_pdf "a.pdf"[1] -> "o', None, 'output folder is missing its closing "'),
        ('cut_pdf "a.pdf"[1-2', None, "the range is missing its closing ']'"),
        ("cut_pdf a.pdf[1-2", None, "the range is missing its closing ']'"),
        ('cut_pdf "a.pdf[1]', None, 'is missing its closing "'),
        ('cut_pdf "a.pdf"x[1]', None, 'Unexpected text right after "a.pdf"'),
        ('cut_pdf ""[1]', None, "A path is empty"),
        ("cut_pdf [7-16]", None, "A range needs a file in front of it"),
    ],
)
def test_malformed_commands_explain_themselves(
    text: str, operation: str | None, message: str
) -> None:
    with pytest.raises(CommandSyntaxError, match=re.escape(message)):
        parse_command(text, operation=operation)


def test_command_validates_on_construction() -> None:
    with pytest.raises(CommandSyntaxError, match="Unknown operation"):
        Command("split_pdf", (Source(Path("a.pdf"), "1"),))
    with pytest.raises(CommandSyntaxError, match="Unknown operation"):
        parse_command('"a.pdf"[1]', operation="split_pdf")
    assert Command("cut_video", (Source(Path("a.mp4"), "1-2"),)).kind == "video"


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
