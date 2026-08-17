"""Output naming: never overwrite, always unique, always predictable."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from compress.errors import InputFileError
from compress.naming import reserve_output_path


def _make(path: Path, content: bytes = b"data") -> Path:
    path.write_bytes(content)
    return path


def test_default_name_inserts_compressed_before_extension(tmp_path: Path) -> None:
    source = _make(tmp_path / "movie.mp4")
    reserved = reserve_output_path(source)
    assert reserved.path.name == "movie_compressed.mp4"
    assert reserved.path.parent == tmp_path


def test_dotted_stem_keeps_only_the_real_extension(tmp_path: Path) -> None:
    """The user's real example: a name containing dots before the extension."""
    source = _make(tmp_path / "CasualIQBusinessIntelligence.ai.mp4")
    reserved = reserve_output_path(source)
    assert reserved.path.name == "CasualIQBusinessIntelligence.ai_compressed.mp4"


def test_collisions_get_numeric_suffixes(tmp_path: Path) -> None:
    source = _make(tmp_path / "movie.mp4")
    _make(tmp_path / "movie_compressed.mp4")
    _make(tmp_path / "movie_compressed_1.mp4")

    reserved = reserve_output_path(source)
    assert reserved.path.name == "movie_compressed_2.mp4"


def test_existing_files_are_never_touched(tmp_path: Path) -> None:
    source = _make(tmp_path / "movie.mp4")
    taken = _make(tmp_path / "movie_compressed.mp4", b"precious")

    reserve_output_path(source)
    assert taken.read_bytes() == b"precious"


def test_reservation_is_atomic(tmp_path: Path) -> None:
    """Two reservations in a row must never return the same path."""
    source = _make(tmp_path / "movie.mp4")
    first = reserve_output_path(source)
    second = reserve_output_path(source)
    assert first.path != second.path
    assert first.path.exists() and second.path.exists()


def test_release_removes_only_the_empty_placeholder(tmp_path: Path) -> None:
    source = _make(tmp_path / "movie.mp4")
    reserved = reserve_output_path(source)
    assert reserved.path.exists()
    reserved.release()
    assert not reserved.path.exists()

    again = reserve_output_path(source)
    again.path.write_bytes(b"real output")
    again.release()
    assert again.path.exists(), "a written file must survive release()"


def test_release_is_idempotent(tmp_path: Path) -> None:
    reserved = reserve_output_path(_make(tmp_path / "movie.mp4"))
    reserved.release()
    reserved.release()


def test_extension_override_changes_the_suffix(tmp_path: Path) -> None:
    source = _make(tmp_path / "song.wav")
    reserved = reserve_output_path(source, extension=".mp3")
    assert reserved.path.name == "song_compressed.mp3"


def test_extension_override_accepts_a_missing_dot(tmp_path: Path) -> None:
    source = _make(tmp_path / "song.wav")
    reserved = reserve_output_path(source, extension="mp3")
    assert reserved.path.name == "song_compressed.mp3"


def test_explicit_output_is_used_verbatim(tmp_path: Path) -> None:
    source = _make(tmp_path / "movie.mp4")
    wanted = tmp_path / "elsewhere.mp4"
    reserved = reserve_output_path(source, explicit=wanted)
    assert reserved.path == wanted


def test_explicit_output_refuses_to_clobber_the_input(tmp_path: Path) -> None:
    source = _make(tmp_path / "movie.mp4")
    with pytest.raises(InputFileError, match="Refusing to overwrite the original"):
        reserve_output_path(source, explicit=source)


def test_explicit_output_refuses_an_existing_file_without_overwrite(tmp_path: Path) -> None:
    source = _make(tmp_path / "movie.mp4")
    taken = _make(tmp_path / "out.mp4")
    with pytest.raises(InputFileError, match="already exists"):
        reserve_output_path(source, explicit=taken)


def test_explicit_output_allows_overwrite_when_asked(tmp_path: Path) -> None:
    source = _make(tmp_path / "movie.mp4")
    taken = _make(tmp_path / "out.mp4")
    reserved = reserve_output_path(source, explicit=taken, overwrite=True)
    assert reserved.path == taken


def test_explicit_output_rejects_a_directory(tmp_path: Path) -> None:
    source = _make(tmp_path / "movie.mp4")
    folder = tmp_path / "folder"
    folder.mkdir()
    with pytest.raises(InputFileError, match="is a directory"):
        reserve_output_path(source, explicit=folder)


def test_missing_output_directory_is_reported(tmp_path: Path) -> None:
    source = _make(tmp_path / "movie.mp4")
    with pytest.raises(InputFileError, match="does not exist"):
        reserve_output_path(source, explicit=tmp_path / "nope" / "out.mp4")


@pytest.mark.parametrize(
    "name",
    [
        "file with spaces.mp4",
        "file (1) [copy].mp4",
        "file&and;semi.mp4",
        "видео.mp4",
        "動画テスト.mp4",
        "emoji-🎬.mp4",
        "file'quote.mp4",
        "file#hash.mp4",
        "-leading-dash.mp4",
    ],
)
def test_awkward_filenames_survive(tmp_path: Path, name: str) -> None:
    source = _make(tmp_path / name)
    reserved = reserve_output_path(source)
    assert reserved.path.exists()
    assert reserved.path.stem.endswith("_compressed")
    assert reserved.path.suffix == ".mp4"


def test_no_extension_input(tmp_path: Path) -> None:
    source = _make(tmp_path / "noext")
    reserved = reserve_output_path(source)
    assert reserved.path.name == "noext_compressed"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
def test_unwritable_directory_is_reported(tmp_path: Path) -> None:
    folder = tmp_path / "locked"
    folder.mkdir()
    source = _make(folder / "movie.mp4")
    folder.chmod(0o500)
    try:
        with pytest.raises(InputFileError, match="No permission"):
            reserve_output_path(source)
    finally:
        folder.chmod(0o700)
