"""The add_pdf / cut_pdf / add_video / cut_video console commands."""

from __future__ import annotations

import importlib
import json
import sys
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


def test_powershell_mangled_arguments_still_work(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "My Docs"
    folder.mkdir()
    book = make_pdf(folder / "book.pdf", 10)
    out = tmp_path / "out"
    # What Windows PowerShell 5.1 hands over for:
    #   cut_pdf 'pdf("...My Docs/book.pdf")[2-3] -> output("...")'
    argv = f"pdf({book})[2-3] -> output({out})".split(" ")
    assert len(argv) > 2

    assert main_cut_pdf(argv) == 0
    output = out / "book_pages_2-3.pdf"
    assert page_ids(output) == [102, 103]
    stdout = capsys.readouterr().out
    assert "book.pdf: pages [2-3] (2 of 10)" in stdout
    assert "Done." in stdout and "Pages:     2" in stdout
    assert str(output.resolve()) in stdout


def test_operation_word_may_be_repeated(tmp_path: Path) -> None:
    first = make_pdf(tmp_path / "a.pdf", 3)
    second = make_pdf(tmp_path / "b.pdf", 3, base=500)
    assert main_add_pdf([f'add_pdf pdf1("{first}")[1] pdf2("{second}")[3]', "-q"]) == 0
    assert page_ids(tmp_path / "a_merged.pdf") == [101, 503]


def test_json_and_quiet_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    book = make_pdf(tmp_path / "book.pdf", 4)
    assert main_cut_pdf(["--json", f'pdf("{book}")[1-2]']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "cut_pdf"
    assert payload["pages"] == 2
    assert Path(payload["output_path"]).is_file()

    assert main_cut_pdf(["-q", f'pdf("{book}")[3]']) == 0
    assert capsys.readouterr().out.strip() == str((tmp_path / "book_pages_3.pdf").resolve())


@pytest.mark.parametrize("operation", sorted(MAINS))
def test_help_and_version(operation: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert MAINS[operation](["--help"]) == 0
    text = capsys.readouterr().out
    assert text.startswith(f"usage: {operation} ")
    assert "examples:" in text and "Quote the whole command" in text
    assert ("--copy" in text) == (operation == "cut_video")

    assert MAINS[operation](["--version"]) == 0
    assert capsys.readouterr().out == f"{operation} (ecompress {__version__})\n"


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        ([], "no command given"),
        (["--bogus", 'pdf("a.pdf")[1]'], "unknown option --bogus"),
        (["--copy", 'pdf("a.pdf")[1]'], "--copy only applies to cut_video"),
        (['pdf("a.pdf")[1-'], "closing ']'"),
        (['cut_video video("a.mp4")[1-2]'], "This is the cut_pdf command"),
        (['pdf("missing-file.pdf")[1]'], "File not found"),
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
    assert main_cut_video(['video("a.mp4")[0-1]']) == 3
    assert "FFmpeg is not installed" in capsys.readouterr().err


def test_failed_operation_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from ecompress.errors import OutputValidationError

    def broken(*args: object, **kwargs: object) -> None:
        raise OutputValidationError("The new video failed validation (boom); nothing was written.")

    monkeypatch.setattr("ecompress.edit.run_video", broken)
    assert main_add_video(['video1("a.mp4") video2("b.mp4")']) == 1
    assert "boom" in capsys.readouterr().err


@requires_x264
def test_cut_video_command(
    tmp_path: Path, source_mp4: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main_cut_video(["--json", f'video("{source_mp4}")[0-1] -> output("{tmp_path}")']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["duration_seconds"] == pytest.approx(1.0, abs=0.15)
    assert Path(payload["output_path"]) == (tmp_path / "clip_cut.mp4").resolve()


def test_console_scripts_are_declared() -> None:
    tomllib = pytest.importorskip("tomllib") if sys.version_info >= (3, 11) else None
    if tomllib is None:
        pytest.skip("tomllib needs Python 3.11")
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    scripts = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["scripts"]
    for operation, main in MAINS.items():
        module, _, name = scripts[operation].partition(":")
        assert getattr(importlib.import_module(module), name) is main
