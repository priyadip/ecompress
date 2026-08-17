"""Output file naming.

Default behaviour: the compressed file is written next to the input with
``_compressed`` inserted before the extension::

    D:/Videos/movie.mp4  ->  D:/Videos/movie_compressed.mp4

If that name is taken, a numeric suffix is appended (``movie_compressed_1.mp4``,
``movie_compressed_2.mp4``, ...). The original file is never overwritten.
"""

from __future__ import annotations

import os
from pathlib import Path

from compress.errors import InputFileError

__all__ = ["COMPRESSED_SUFFIX", "ReservedPath", "reserve_output_path"]

COMPRESSED_SUFFIX = "_compressed"

#: Upper bound on the ``_1``, ``_2``, ... search before giving up.
_MAX_CANDIDATES = 10_000


class ReservedPath:
    """A uniquely reserved output path.

    The path is created immediately as an empty file using ``O_EXCL`` so two
    concurrent runs can never pick the same name. Call :meth:`release` to remove
    the placeholder if the compression ultimately fails.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._released = False

    def release(self) -> None:
        """Delete the placeholder file (safe to call more than once)."""
        if self._released:
            return
        self._released = True
        try:
            if self.path.is_file() and self.path.stat().st_size == 0:
                self.path.unlink()
        except OSError:  # pragma: no cover - best effort cleanup
            pass

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ReservedPath({str(self.path)!r})"


def _candidate_names(stem: str, extension: str, suffix: str) -> list[str]:
    base = f"{stem}{suffix}{extension}"
    names = [base]
    names.extend(f"{stem}{suffix}_{n}{extension}" for n in range(1, _MAX_CANDIDATES))
    return names


def reserve_output_path(
    input_path: Path,
    *,
    extension: str | None = None,
    suffix: str = COMPRESSED_SUFFIX,
    explicit: Path | None = None,
    overwrite: bool = False,
) -> ReservedPath:
    """Pick and atomically reserve the path the compressed file will be written to.

    Args:
        input_path: the original file.
        extension: final extension including the dot (e.g. ``".mp3"``). Defaults
            to the input's extension.
        suffix: inserted before the extension.
        explicit: an exact path requested by the caller; bypasses the automatic
            naming but is still refused if it would clobber the input.
        overwrite: allow ``explicit`` to replace an existing file.

    Raises:
        InputFileError: if the target directory is not writable, if the chosen
            path would overwrite the input, or if no free name exists.
    """
    ext = extension if extension is not None else input_path.suffix
    if ext and not ext.startswith("."):
        ext = "." + ext

    if explicit is not None:
        return _reserve_explicit(input_path, explicit, overwrite=overwrite)

    directory = input_path.parent
    _require_writable_dir(directory)

    resolved_input = _safe_resolve(input_path)
    for name in _candidate_names(input_path.stem, ext, suffix):
        candidate = directory / name
        if _safe_resolve(candidate) == resolved_input:
            continue
        reserved = _try_create(candidate)
        if reserved is not None:
            return reserved

    raise InputFileError(
        f"Could not find a free output name next to {input_path}. "
        f"Too many files already match '{input_path.stem}{suffix}*{ext}'."
    )


def _reserve_explicit(input_path: Path, explicit: Path, *, overwrite: bool) -> ReservedPath:
    target = explicit.expanduser()
    if target.is_dir():
        raise InputFileError(f"Output path {target} is a directory.")

    if _safe_resolve(target) == _safe_resolve(input_path):
        raise InputFileError(
            "Refusing to overwrite the original file. "
            "Choose a different output path (the original is never modified)."
        )

    _require_writable_dir(target.parent if str(target.parent) else Path())

    if target.exists():
        if not overwrite:
            raise InputFileError(
                f"Output file already exists: {target}\nPass --overwrite to replace it."
            )
        return ReservedPath(target)

    reserved = _try_create(target)
    if reserved is None:
        raise InputFileError(f"Could not create the output file: {target}")
    return reserved


def _require_writable_dir(directory: Path) -> None:
    directory = directory if str(directory) else Path()
    if not directory.exists():
        raise InputFileError(f"Output directory does not exist: {directory}")
    if not directory.is_dir():
        raise InputFileError(f"Output location is not a directory: {directory}")
    if not os.access(directory, os.W_OK):
        raise InputFileError(
            f"No permission to write into {directory}.\n"
            "Choose a different output location with --output."
        )


def _try_create(candidate: Path) -> ReservedPath | None:
    """Create ``candidate`` exclusively, returning ``None`` if it already exists."""
    try:
        fd = os.open(str(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return None
    except OSError as exc:
        raise InputFileError(f"Could not create the output file {candidate}: {exc}") from exc
    os.close(fd)
    return ReservedPath(candidate)


def _safe_resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:  # pragma: no cover - unreachable on supported platforms
        return path.absolute()
