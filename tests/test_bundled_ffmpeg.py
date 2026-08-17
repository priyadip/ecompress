"""FFmpeg that ships with the package.

``ffmpeg-binaries`` is a normal dependency, so on the common platforms
``pip install compress-cli`` already puts ffmpeg and ffprobe on disk. These
tests cover finding them, preferring the right copy, and the ``--check``
report that tells a user what was found.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator
from importlib import metadata
from pathlib import Path

import pytest

from compress import ffmpeg as ffmpeg_module
from compress.cli import EXIT_MISSING_DEPENDENCY, EXIT_OK, main
from compress.diagnostics import collect, render
from compress.ffmpeg import (
    FFmpegTools,
    bundled_binary,
    clear_ffmpeg_cache,
    find_ffmpeg_tools,
)

Capsys = pytest.CaptureFixture[str]


def _bundled_installed() -> bool:
    try:
        metadata.distribution("ffmpeg-binaries")
    except metadata.PackageNotFoundError:
        return False
    return True


requires_bundled = pytest.mark.skipif(
    not _bundled_installed(),
    reason="ffmpeg-binaries is not installed (no prebuilt wheel for this platform)",
)


@pytest.fixture(autouse=True)
def _reset_discovery_cache() -> Iterator[None]:
    """Discovery is cached; every test here must start from a clean slate."""
    clear_ffmpeg_cache()
    yield
    clear_ffmpeg_cache()


# -- locating the bundled binaries ----------------------------------------


@requires_bundled
@pytest.mark.parametrize("binary", ["ffmpeg", "ffprobe"])
def test_bundled_binary_is_found(binary: str) -> None:
    found = bundled_binary(binary)
    assert found is not None, f"{binary} should ship with ffmpeg-binaries"
    assert found.is_file()
    assert os.access(found, os.X_OK), "the binary must be executable"


@requires_bundled
def test_bundled_ffprobe_is_present() -> None:
    """The whole reason for choosing ffmpeg-binaries over imageio-ffmpeg."""
    assert bundled_binary("ffprobe") is not None


@requires_bundled
def test_bundled_binary_prefers_the_real_executable_over_the_shim() -> None:
    """pip also drops a console-script shim; the real binary is the one we want.

    The shim lands in the environment's ``Scripts``/``bin`` directory, the real
    executable in the package's own ``binaries`` directory.
    """
    found = bundled_binary("ffmpeg")
    assert found is not None

    parts = [part.lower() for part in found.parts]
    assert "binaries" in parts, f"picked the shim instead of the real binary: {found}"
    assert "scripts" not in parts, f"picked the Scripts shim: {found}"


@requires_bundled
def test_bundled_binaries_actually_run() -> None:
    """Locating a file is not enough - it has to be a working FFmpeg."""
    from compress.process import run_command

    for binary in ("ffmpeg", "ffprobe"):
        path = bundled_binary(binary)
        assert path is not None
        result = run_command([path, "-version"], timeout=60, check=False)
        assert binary in (result.stdout + result.stderr).lower()


def test_bundled_binary_returns_none_when_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ffmpeg_module, "_BUNDLED_DISTRIBUTION", "definitely-not-installed-xyz")
    assert bundled_binary("ffmpeg") is None


def test_unknown_binary_name_is_not_found() -> None:
    assert bundled_binary("not-a-real-tool") is None


# -- discovery order -------------------------------------------------------


def test_explicit_override_beats_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    fake.write_text("", encoding="utf-8")

    monkeypatch.setenv("COMPRESS_FFMPEG", str(fake))
    clear_ffmpeg_cache()
    assert find_ffmpeg_tools().ffmpeg == fake


@requires_bundled
def test_bundled_is_preferred_over_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Behaviour must be identical on every machine, whatever is on PATH."""
    monkeypatch.delenv("COMPRESS_FFMPEG", raising=False)
    monkeypatch.delenv("COMPRESS_FFPROBE", raising=False)
    clear_ffmpeg_cache()

    tools = find_ffmpeg_tools()
    assert tools.ffmpeg == bundled_binary("ffmpeg")
    assert tools.ffprobe == bundled_binary("ffprobe")


@requires_bundled
def test_discovery_works_with_an_empty_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of bundling: FFmpeg is found even with nothing on PATH."""
    monkeypatch.delenv("COMPRESS_FFMPEG", raising=False)
    monkeypatch.delenv("COMPRESS_FFPROBE", raising=False)
    monkeypatch.setenv("PATH", "")
    clear_ffmpeg_cache()

    tools = find_ffmpeg_tools()
    assert tools.available, "FFmpeg should still be found with an empty PATH"
    assert shutil.which("ffmpeg") is None, "sanity check: PATH really is empty"


@requires_bundled
def test_real_compression_works_with_an_empty_path(
    tmp_path: Path, source_wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end with no system FFmpeg reachable at all."""
    from compress import compress

    monkeypatch.delenv("COMPRESS_FFMPEG", raising=False)
    monkeypatch.delenv("COMPRESS_FFPROBE", raising=False)
    monkeypatch.setenv("PATH", "")
    clear_ffmpeg_cache()

    source = tmp_path / "sound.wav"
    source.write_bytes(source_wav.read_bytes())

    result = compress(source, 0.2)
    assert result.output_size_bytes < 200_000
    assert result.output_path.exists()


# -- the --check report ----------------------------------------------------


def test_check_reports_a_healthy_environment(capsys: Capsys) -> None:
    code = main(["--check"])
    out = capsys.readouterr().out

    assert "compress " in out
    assert "Images (Pillow)" in out
    assert "PDF (pikepdf)" in out
    assert "Video and audio (FFmpeg)" in out
    if code == EXIT_OK:
        assert "Everything is installed" in out
    else:
        assert "Problems found:" in out


def test_check_needs_no_other_arguments(capsys: Capsys) -> None:
    main(["--check"])
    assert "compress " in capsys.readouterr().out


def test_check_json_is_machine_readable(capsys: Capsys) -> None:
    code = main(["--check", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is (code == EXIT_OK)
    assert payload["pillow_version"]
    assert payload["pikepdf_version"]
    assert "encoders" in payload
    assert isinstance(payload["problems"], list)


@requires_bundled
def test_check_says_where_ffmpeg_came_from(capsys: Capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COMPRESS_FFMPEG", raising=False)
    monkeypatch.delenv("COMPRESS_FFPROBE", raising=False)
    clear_ffmpeg_cache()

    main(["--check"])
    out = capsys.readouterr().out
    assert "bundled with this package" in out


def test_check_reports_missing_ffmpeg_without_crashing(
    monkeypatch: pytest.MonkeyPatch, capsys: Capsys
) -> None:
    monkeypatch.setattr(
        "compress.diagnostics.find_ffmpeg_tools",
        lambda: FFmpegTools(ffmpeg=None, ffprobe=None),
    )

    code = main(["--check"])
    out = capsys.readouterr().out

    assert code == EXIT_MISSING_DEPENDENCY
    assert "MISSING" in out
    assert "Images and PDFs still work." in out
    assert "Problems found:" in out


def test_diagnostics_render_is_stable_without_ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "compress.diagnostics.find_ffmpeg_tools",
        lambda: FFmpegTools(ffmpeg=None, ffprobe=None),
    )
    report = collect()
    assert not report.ok
    text = render(report)
    assert text.endswith("\n")
    assert "ffmpeg / ffprobe were not found" in text


# -- the install hint ------------------------------------------------------


def test_missing_dependency_message_offers_the_pip_route() -> None:
    from compress.errors import MissingDependencyError

    with pytest.raises(MissingDependencyError) as info:
        FFmpegTools(ffmpeg=None, ffprobe=None).require("a video")

    message = str(info.value)
    assert 'pip install "compress-cli[ffmpeg]"' in message
    assert "installed automatically" in message
    assert "COMPRESS_FFMPEG" in message
