"""The add_pdf / cut_pdf / add_video / cut_video console commands."""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest

from ecompress import __version__
from ecompress.edit.cli import main_add_pdf, main_add_video, main_cut_pdf, main_cut_video
from ecompress.errors import MissingDependencyError
from tests.conftest import requires_x264
from tests.test_edit_pdf import make_pdf, page_ids

MAINS = {
    "add_pdf": main_add_pdf,
    "cut_pdf": main_cut_pdf,
    "add_video": main_add_video,
    "cut_video": main_cut_video,
}


def _pdf_in_folder_with_spaces(tmp_path: Path) -> Path:
    folder = tmp_path / "My Docs"
    folder.mkdir()
    return make_pdf(folder / "book one.pdf", 10)


def test_arguments_as_bash_cmd_and_powershell_stop_parsing_deliver_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    book = _pdf_in_folder_with_spaces(tmp_path)
    out = tmp_path / "out dir"
    # cut_pdf "…/My Docs/book one.pdf"[2-3] '->' "…/out dir"   (quotes removed)
    assert main_cut_pdf(["--%", f"{book}[2-3]", "->", str(out)]) == 0

    output = out / "book one_pages_2-3.pdf"
    assert page_ids(output) == [102, 103]
    stdout = capsys.readouterr().out
    assert "book one.pdf: pages [2-3] (2 of 10)" in stdout
    assert "Done." in stdout and "Pages:     2" in stdout
    assert str(output.resolve()) in stdout


def test_arguments_as_windows_powershell_splits_a_quoted_line(tmp_path: Path) -> None:
    book = _pdf_in_folder_with_spaces(tmp_path)
    second = make_pdf(tmp_path / "My Docs" / "other.pdf", 3, base=500)
    # cut_pdf '"…/book one.pdf" "…/other.pdf"[3] -> "…/out"'  under PowerShell 5.1
    argv = f"{book} {second}[3] -> {tmp_path / 'out'}".split(" ")
    assert main_add_pdf(["-q", *argv]) == 0
    assert page_ids(tmp_path / "out" / "book one_merged.pdf") == [*range(101, 111), 503]


def test_quoted_text_and_the_operation_word(tmp_path: Path) -> None:
    first = make_pdf(tmp_path / "a.pdf", 3)
    second = make_pdf(tmp_path / "b.pdf", 3, base=500)
    assert main_add_pdf([f'add_pdf "{first}"[1] "{second}"[3]', "-q"]) == 0
    assert page_ids(tmp_path / "a_merged.pdf") == [101, 503]


def test_swallowed_arrow_is_caught_before_anything_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    book = make_pdf(tmp_path / "book.pdf", 4)
    before = sorted(tmp_path.iterdir())
    # bash / cmd / PowerShell for:  cut_pdf "book.pdf"[1-2] -> "D:/out"
    assert main_cut_pdf([f"{book}[1-2]", "-"]) == 2
    err = capsys.readouterr().err
    assert "Your shell read the '>'" in err
    assert 'cut_pdf --% "C:/My Documents/book.pdf"[2-5,8-12,20] -> "D:/out"' in err
    assert "'->'" in err and '"->"' in err and "noglob" in err
    assert sorted(tmp_path.iterdir()) == before


def test_json_and_quiet_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    book = make_pdf(tmp_path / "book.pdf", 4)
    assert main_cut_pdf(["--json", f'"{book}"[1-2]']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "cut_pdf"
    assert payload["pages"] == 2
    assert Path(payload["output_path"]).is_file()

    assert main_cut_pdf(["-q", f"{book}[3]"]) == 0
    assert capsys.readouterr().out.strip() == str((tmp_path / "book_pages_3.pdf").resolve())


@pytest.mark.parametrize("operation", sorted(MAINS))
def test_help_and_version(operation: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert MAINS[operation](["--help"]) == 0
    text = capsys.readouterr().out
    assert text.startswith(f'usage: {operation} "PATH"[RANGE] ... [-> "FOLDER"]')
    assert "examples:" in text
    assert f"PowerShell   {operation} --% " in text
    assert ("--copy" in text) == (operation == "cut_video")

    assert MAINS[operation](["--version"]) == 0
    assert capsys.readouterr().out == f"{operation} (ecompress {__version__})\n"


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        ([], "no command given"),
        (["--bogus", '"a.pdf"[1]'], "unknown option --bogus"),
        (["--copy", '"a.pdf"[1]'], "--copy only applies to cut_video"),
        (['"a.pdf"[1-'], "closing ']'"),
        (['cut_video "a.mp4"[1-2]'], "This is the cut_pdf command"),
        (['"missing-file.pdf"[1]'], "File not found"),
        (["--%", "missing-file.pdf[1]"], "File not found"),
    ],
)
def test_usage_errors_exit_2(
    argv: list[str],
    message: str,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main_cut_pdf(argv) == 2
    assert message in capsys.readouterr().err


def test_missing_ffmpeg_exits_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(media_kind: str = "media") -> tuple[Path, Path]:
        raise MissingDependencyError("ffmpeg", "FFmpeg is not installed.")

    monkeypatch.setattr("ecompress.edit.video.require_ffmpeg", missing)
    assert main_cut_video(['"a.mp4"[0-1]']) == 3
    assert "FFmpeg is not installed" in capsys.readouterr().err


def test_failed_operation_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ecompress.errors import OutputValidationError

    def broken(*args: object, **kwargs: object) -> None:
        raise OutputValidationError("The new video failed validation (boom); nothing was written.")

    monkeypatch.setattr("ecompress.edit.run_video", broken)
    assert main_add_video(['"a.mp4" "b.mp4"']) == 1
    assert "boom" in capsys.readouterr().err


@requires_x264
def test_cut_video_command(
    tmp_path: Path, source_mp4: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main_cut_video(["--json", f"{source_mp4}[0-1]", "->", str(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["duration_seconds"] == pytest.approx(1.0, abs=0.15)
    assert Path(payload["output_path"]) == (tmp_path / "clip_cut.mp4").resolve()


def test_console_scripts_are_declared() -> None:
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    for operation, main in MAINS.items():
        entry = re.search(rf'^{operation} = "([\w.]+):(\w+)"$', pyproject, re.MULTILINE)
        assert entry is not None, f"{operation} is not in [project.scripts]"
        assert getattr(importlib.import_module(entry.group(1)), entry.group(2)) is main
