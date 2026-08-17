"""Safe external-tool invocation.

Every command is built as an argument **list** and executed with
``shell=False``. No user-controlled string is ever concatenated into a shell
command line, so filenames containing spaces, quotes, ``&``, ``|``, brackets,
parentheses or non-ASCII characters are passed through untouched.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from compress.errors import MissingDependencyError, ToolExecutionError

__all__ = ["CommandResult", "run_command"]

Arg = str | Path | int | float

#: Windows-only flag that stops a console window from flashing up per encode.
#: Zero on every other platform, where ``creationflags`` must stay unset.
_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


@dataclass(frozen=True)
class CommandResult:
    """Captured result of an external command."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _stringify(args: Sequence[Arg]) -> list[str]:
    out: list[str] = []
    for arg in args:
        if isinstance(arg, Path):
            out.append(os.fspath(arg))
        elif isinstance(arg, str):
            out.append(arg)
        else:
            out.append(str(arg))
    return out


def run_command(
    args: Sequence[Arg],
    *,
    timeout: float | None = None,
    check: bool = True,
    tool: str | None = None,
) -> CommandResult:
    """Run an external tool safely and capture its output.

    Args:
        args: the executable followed by its arguments, as a list.
        timeout: seconds before the child process is killed.
        check: raise :class:`ToolExecutionError` on a non-zero exit status.
        tool: friendly name used in error messages.

    Raises:
        MissingDependencyError: the executable could not be found.
        ToolExecutionError: non-zero exit (when ``check`` is true) or timeout.
    """
    if not args:
        raise ValueError("no command given")

    argv = _stringify(args)
    name = tool or Path(argv[0]).stem

    try:
        completed = subprocess.run(  # noqa: S603 - argument list, shell=False
            argv,
            capture_output=True,
            shell=False,
            timeout=timeout,
            check=False,
            # 0 everywhere except Windows, where it stops a console window
            # flashing up for every encode.
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise MissingDependencyError(
            name, f"{name} could not be found on this system ({argv[0]})."
        ) from exc
    except PermissionError as exc:
        raise MissingDependencyError(name, f"{name} is not executable: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolExecutionError(name, -1, f"{name} timed out after {timeout} seconds.") from exc

    result = CommandResult(
        args=tuple(argv),
        returncode=completed.returncode,
        stdout=_decode(completed.stdout),
        stderr=_decode(completed.stderr),
    )
    if check and not result.ok:
        raise ToolExecutionError(name, result.returncode, result.stderr or result.stdout)
    return result


def _decode(raw: bytes | None) -> str:
    if not raw:
        return ""
    return raw.decode("utf-8", errors="replace")
