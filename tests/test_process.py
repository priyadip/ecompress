"""Subprocess safety: argument lists only, never a shell."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from compress.errors import MissingDependencyError, ToolExecutionError
from compress.process import run_command


def test_runs_a_command_and_captures_output() -> None:
    result = run_command([sys.executable, "-c", "print('hello')"])
    assert result.ok
    assert "hello" in result.stdout


def test_accepts_path_objects(tmp_path: Path) -> None:
    script = tmp_path / "script.py"
    script.write_text("print('from a Path')", encoding="utf-8")
    result = run_command([sys.executable, script])
    assert "from a Path" in result.stdout


def test_shell_metacharacters_are_not_interpreted(tmp_path: Path) -> None:
    """The classic injection: a filename that would be a command if shelled out."""
    canary = tmp_path / "canary.txt"
    hostile = f"; echo pwned > {canary} ; #"

    result = run_command([sys.executable, "-c", "import sys; print(repr(sys.argv[1]))", hostile])

    assert not canary.exists(), "the shell must never see the argument"
    assert "pwned" in result.stdout, "the string was passed through verbatim"


@pytest.mark.parametrize(
    "hostile",
    [
        "a && rm -rf /",
        "$(whoami)",
        "`id`",
        "| cat /etc/passwd",
        '" ; del C:\\Windows ; "',
        "%PATH%",
        "file with spaces and 'quotes'",
        "видео файл.mp4",
        "emoji 🎬 name.mp4",
    ],
)
def test_hostile_arguments_arrive_unchanged(hostile: str) -> None:
    result = run_command(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1])", hostile]
    )
    assert result.stdout == hostile


def test_never_uses_shell_true() -> None:
    """Guard against a future edit turning shell=True back on."""
    import inspect

    from compress import process

    source = inspect.getsource(process)
    assert "shell=True" not in source
    assert "shell=False" in source


def test_nonzero_exit_raises_with_the_tail_of_stderr() -> None:
    with pytest.raises(ToolExecutionError) as info:
        run_command(
            [sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"],
            tool="fake",
        )
    assert info.value.returncode == 3
    assert "boom" in str(info.value)


def test_check_false_returns_the_failure() -> None:
    result = run_command([sys.executable, "-c", "raise SystemExit(4)"], check=False)
    assert not result.ok
    assert result.returncode == 4


def test_missing_executable_is_a_dependency_error() -> None:
    with pytest.raises(MissingDependencyError):
        run_command(["this-command-does-not-exist-anywhere-12345"])


def test_timeout_is_reported() -> None:
    with pytest.raises(ToolExecutionError, match="timed out"):
        run_command([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1.0)


def test_empty_command_is_rejected() -> None:
    with pytest.raises(ValueError, match="no command"):
        run_command([])


def test_undecodable_output_does_not_crash() -> None:
    result = run_command(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff\\xfe\\x00')"]
    )
    assert result.ok
    assert isinstance(result.stdout, str)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only flag")
def test_windows_hides_the_console_window() -> None:
    assert hasattr(subprocess, "CREATE_NO_WINDOW")
