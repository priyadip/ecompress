"""The ``compress`` command-line interface."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from compress import __version__
from compress.cli import (
    EXIT_MISSING_DEPENDENCY,
    EXIT_OK,
    EXIT_TARGET_NOT_ACHIEVED,
    EXIT_USAGE,
    main,
)

Copier = Callable[..., Path]
Capsys = pytest.CaptureFixture[str]


def test_two_arguments_are_the_whole_interface(
    copy_media: Copier, source_jpg: Path, capsys: Capsys
) -> None:
    source = copy_media(source_jpg)
    code = main([str(source), "0.3"])
    out = capsys.readouterr().out

    assert code == EXIT_OK
    assert "Compression successful." in out
    assert "Original:" in out
    assert "Compressed:" in out
    assert "Saved:" in out
    assert "Reduction:" in out
    assert str((source.parent / "photo_compressed.jpg").resolve()) in out


def test_output_file_is_really_below_the_target(copy_media: Copier, source_jpg: Path) -> None:
    source = copy_media(source_jpg)
    assert main([str(source), "0.3"]) == EXIT_OK
    assert (source.parent / "photo_compressed.jpg").stat().st_size < 300_000


def test_progress_shows_each_attempt(copy_media: Copier, source_jpg: Path, capsys: Capsys) -> None:
    main([str(copy_media(source_jpg)), "0.3"])
    out = capsys.readouterr().out

    assert "Detecting media type..." in out
    assert "Image detected" in out
    assert "Optimizing..." in out
    assert "Attempt 1:" in out


def test_already_small_enough(copy_media: Copier, source_jpg: Path, capsys: Capsys) -> None:
    code = main([str(copy_media(source_jpg)), "100"])
    out = capsys.readouterr().out

    assert code == EXIT_OK
    assert "already below the requested target" in out
    assert "No compression necessary" in out


def test_impossible_target_exits_one_and_never_claims_success(
    copy_media: Copier, source_jpg: Path, capsys: Capsys
) -> None:
    code = main([str(copy_media(source_jpg)), "0.00002"])
    captured = capsys.readouterr()

    assert code == EXIT_TARGET_NOT_ACHIEVED
    assert "Target size could not be achieved" in captured.err
    assert "successful" not in captured.out.lower()


def test_missing_file_exits_two(tmp_path: Path, capsys: Capsys) -> None:
    code = main([str(tmp_path / "nope.jpg"), "1"])
    assert code == EXIT_USAGE
    assert "File not found" in capsys.readouterr().err


def test_unsupported_type_exits_two(tmp_path: Path, capsys: Capsys) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("hello", encoding="utf-8")
    code = main([str(path), "1"])
    assert code == EXIT_USAGE
    assert "Unsupported file type" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["abc", "0", "-5", "nan", "inf"])
def test_bad_target_is_a_usage_error(
    copy_media: Copier, source_jpg: Path, bad: str, capsys: Capsys
) -> None:
    with pytest.raises(SystemExit) as info:
        main([str(copy_media(source_jpg)), bad])
    assert info.value.code == EXIT_USAGE
    err = capsys.readouterr().err
    assert "TARGET_MB" in err or "greater than 0" in err


def test_missing_arguments(capsys: Capsys) -> None:
    with pytest.raises(SystemExit) as info:
        main([])
    assert info.value.code == EXIT_USAGE


def test_quiet_prints_only_the_path(copy_media: Copier, source_jpg: Path, capsys: Capsys) -> None:
    source = copy_media(source_jpg)
    code = main([str(source), "0.3", "--quiet"])
    out = capsys.readouterr().out.strip()

    assert code == EXIT_OK
    assert out == str((source.parent / "photo_compressed.jpg").resolve())


def test_json_output(copy_media: Copier, source_jpg: Path, capsys: Capsys) -> None:
    code = main([str(copy_media(source_jpg)), "0.3", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == EXIT_OK
    assert payload["target_achieved"] is True
    assert payload["output_size_bytes"] < payload["target_size_bytes"] == 300_000
    assert payload["media_type"] == "image"
    assert Path(payload["output_path"]).exists()


def test_explicit_output(copy_media: Copier, tmp_path: Path, source_jpg: Path) -> None:
    destination = tmp_path / "chosen.jpg"
    code = main([str(copy_media(source_jpg)), "0.3", "-o", str(destination)])
    assert code == EXIT_OK
    assert destination.exists()


def test_output_collision_needs_overwrite(
    copy_media: Copier, tmp_path: Path, source_jpg: Path, capsys: Capsys
) -> None:
    destination = tmp_path / "taken.jpg"
    destination.write_bytes(b"existing")

    code = main([str(copy_media(source_jpg)), "0.3", "-o", str(destination)])
    assert code == EXIT_USAGE
    assert "--overwrite" in capsys.readouterr().err
    assert destination.read_bytes() == b"existing"


def test_overwrite_flag(copy_media: Copier, tmp_path: Path, source_jpg: Path) -> None:
    destination = tmp_path / "taken.jpg"
    destination.write_bytes(b"existing")

    code = main([str(copy_media(source_jpg)), "0.3", "-o", str(destination), "--overwrite"])
    assert code == EXIT_OK
    assert destination.read_bytes() != b"existing"


def test_version(capsys: Capsys) -> None:
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_help_shows_the_simple_usage(capsys: Capsys) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out

    assert "compress" in out
    assert "TARGET_MB" in out or "target_mb" in out
    assert "1,000,000 bytes" in out
    # The primary interface must not advertise codec knobs.
    for knob in ("--codec", "--crf", "--bitrate", "--quality", "--preset", "--max-width"):
        assert knob not in out


def test_missing_ffmpeg_exits_three(
    copy_media: Copier, source_mp4: Path, monkeypatch: pytest.MonkeyPatch, capsys: Capsys
) -> None:
    """Simulate a machine without FFmpeg and check the message is actionable."""
    from compress import ffmpeg as ffmpeg_module
    from compress.ffmpeg import FFmpegTools

    monkeypatch.setattr(ffmpeg_module.find_ffmpeg_tools, "__wrapped__", lambda: None, raising=False)
    monkeypatch.setattr(
        ffmpeg_module, "find_ffmpeg_tools", lambda: FFmpegTools(ffmpeg=None, ffprobe=None)
    )
    monkeypatch.setattr(
        "compress.backends.video.require_ffmpeg",
        lambda kind="media": FFmpegTools(None, None).require(kind),
    )
    monkeypatch.setattr(
        "compress.detect.find_ffmpeg_tools", lambda: FFmpegTools(ffmpeg=None, ffprobe=None)
    )

    code = main([str(copy_media(source_mp4)), "0.25"])
    err = capsys.readouterr().err

    assert code == EXIT_MISSING_DEPENDENCY
    assert "FFmpeg is not installed" in err
    assert "requires FFmpeg" in err


def test_module_entry_point_exists() -> None:
    import compress.__main__ as entry

    assert entry.main is main
