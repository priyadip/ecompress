"""cut_pdf / add_pdf on real PDFs.

Each generated page has a unique width, so the output's page order can be
checked exactly rather than just counted.
"""

from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest

from ecompress import CommandSyntaxError, InputFileError, add_pdf, cut_pdf, execute


def make_pdf(path: Path, pages: int, *, base: int = 100) -> Path:
    """Page ``n`` is ``base + n`` points wide."""
    with pikepdf.new() as pdf:
        for number in range(1, pages + 1):
            pdf.add_blank_page(page_size=(base + number, 200))
        pdf.save(path)
    return path


def page_ids(path: Path) -> list[int]:
    with pikepdf.open(path) as pdf:
        return [int(page.mediabox[2]) for page in pdf.pages]


def test_cut_range_is_written_next_to_the_source(tmp_path: Path) -> None:
    book = make_pdf(tmp_path / "book.pdf", 20)
    result = cut_pdf(book, "7-16")
    assert result.output_path == tmp_path / "book_pages_7-16.pdf"
    assert page_ids(result.output_path) == list(range(107, 117))
    assert result.pages == 10
    assert result.output_size_bytes == result.output_path.stat().st_size
    assert page_ids(book) == list(range(101, 121))  # original untouched


def test_cut_several_ranges(tmp_path: Path) -> None:
    book = make_pdf(tmp_path / "book.pdf", 20)
    result = cut_pdf(book, "2-5,8-12,20")
    assert result.output_path.name == "book_pages_2-5_8-12_20.pdf"
    assert page_ids(result.output_path) == [102, 103, 104, 105, 108, 109, 110, 111, 112, 120]


def test_reversed_and_repeated_pages(tmp_path: Path) -> None:
    book = make_pdf(tmp_path / "book.pdf", 5)
    result = cut_pdf(book, "3-1,1")
    assert page_ids(result.output_path) == [103, 102, 101, 101]


def test_single_page_as_int(tmp_path: Path) -> None:
    book = make_pdf(tmp_path / "book.pdf", 5)
    assert page_ids(cut_pdf(book, 4).output_path) == [104]


def test_add_from_command_text_with_spaces_and_new_folder(tmp_path: Path) -> None:
    folder = tmp_path / "My Documents"
    folder.mkdir()
    first = make_pdf(folder / "a.pdf", 10)
    second = make_pdf(folder / "b file.pdf", 5, base=500)
    out = tmp_path / "out dir" / "nested"

    result = execute(f'add_pdf "{first}"[2-4] "{second}"[1-2] -> "{out}"')

    assert result.output_path == out / "a_merged.pdf"
    assert page_ids(result.output_path) == [102, 103, 104, 501, 502]
    assert result.sources == (first, second)


def test_add_whole_files_and_one_file_twice(tmp_path: Path) -> None:
    first = make_pdf(tmp_path / "a.pdf", 2)
    second = make_pdf(tmp_path / "b.pdf", 2, base=500)
    whole = add_pdf([first, second])
    assert page_ids(whole.output_path) == [101, 102, 501, 502]

    twice = add_pdf([(first, "2"), (first, "1"), (second, None)])
    assert page_ids(twice.output_path) == [102, 101, 501, 502]
    assert twice.output_path.name == "a_merged_1.pdf"  # the first name was taken


def test_explicit_output_file_and_overwrite(tmp_path: Path) -> None:
    book = make_pdf(tmp_path / "book.pdf", 5)
    target = tmp_path / "picked.pdf"
    assert cut_pdf(book, "1-2", output=target).output_path == target

    with pytest.raises(InputFileError, match="already exists"):
        cut_pdf(book, "3", output=target)
    assert page_ids(target) == [101, 102]

    cut_pdf(book, "3", output=target, overwrite=True)
    assert page_ids(target) == [103]


def test_output_may_never_replace_an_input(tmp_path: Path) -> None:
    book = make_pdf(tmp_path / "book.pdf", 5)
    with pytest.raises(InputFileError, match="Refusing to overwrite an input"):
        cut_pdf(book, "1", output=book, overwrite=True)
    assert page_ids(book) == [101, 102, 103, 104, 105]


def test_wrong_output_extension(tmp_path: Path) -> None:
    book = make_pdf(tmp_path / "book.pdf", 5)
    with pytest.raises(InputFileError, match=r"cannot write '\.mp4'"):
        cut_pdf(book, "1", output=tmp_path / "pages.mp4")


def test_bad_range_writes_nothing(tmp_path: Path) -> None:
    book = make_pdf(tmp_path / "book.pdf", 5)
    other = make_pdf(tmp_path / "other.pdf", 5)
    before = sorted(tmp_path.iterdir())
    with pytest.raises(CommandSyntaxError, match="page 9 does not exist"):
        add_pdf([(book, "1-2"), (other, "9")])
    assert sorted(tmp_path.iterdir()) == before


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ("missing", "File not found"),
        ("empty", "File is empty"),
        ("folder", "Not a file"),
        ("garbage", "not a readable PDF"),
        ("password", "password-protected"),
    ],
)
def test_unusable_inputs(tmp_path: Path, setup: str, message: str) -> None:
    path = tmp_path / "input.pdf"
    if setup == "empty":
        path.write_bytes(b"")
    elif setup == "folder":
        path.mkdir()
    elif setup == "garbage":
        path.write_bytes(b"this is not a pdf at all")
    elif setup == "password":
        make_pdf(tmp_path / "plain.pdf", 3)
        with pikepdf.open(tmp_path / "plain.pdf") as pdf:
            pdf.save(path, encryption=pikepdf.Encryption(owner="owner", user="secret"))

    with pytest.raises(InputFileError, match=message):
        cut_pdf(path, "1")
    assert not list(tmp_path.glob("input_pages*"))


def test_copy_flag_is_rejected_for_pdf(tmp_path: Path) -> None:
    book = make_pdf(tmp_path / "book.pdf", 3)
    with pytest.raises(CommandSyntaxError, match="--copy only applies to cut_video"):
        execute(f'cut_pdf "{book}"[1]', copy=True)


def test_long_page_lists_get_a_short_name(tmp_path: Path) -> None:
    book = make_pdf(tmp_path / "book.pdf", 40)
    spec = ",".join(str(n) for n in range(1, 40, 2))
    assert cut_pdf(book, spec).output_path.name == "book_pages.pdf"
    assert cut_pdf(book, "end").output_path.name == "book_pages_end.pdf"
