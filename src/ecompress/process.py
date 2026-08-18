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
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ecompress.errors import MissingDependencyError, ToolExecutionError

__all__ = ["CommandResult", "run_command", "run_with_progress"]

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


def run_with_progress(
    args: Sequence[Arg],
    *,
    total_seconds: float,
    on_progress: Callable[[float], None],
    timeout: float | None = None,
    tool: str | None = None,
) -> CommandResult:
    """Run FFmpeg, reporting how far through the timeline it has reached.

    ``-progress pipe:1`` makes FFmpeg emit ``key=value`` lines as it works. The
    only one that matters here is the output timestamp, which divided by the
    clip's duration gives a genuine fraction complete - far better than
    guessing from bytes written, which jumps around with scene complexity.

    stderr is drained on a second thread: FFmpeg writes enough of it to fill
    the pipe buffer and deadlock if nobody is reading.
    """
    if not args:
        raise ValueError("no command given")

    argv = _stringify(args)
    name = tool or Path(argv[0]).stem
    errors: list[str] = []

    try:
        process = subprocess.Popen(  # noqa: S603 - argument list, shell=False
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise MissingDependencyError(
            name, f"{name} could not be found on this system ({argv[0]})."
        ) from exc
    except PermissionError as exc:
        raise MissingDependencyError(name, f"{name} is not executable: {argv[0]}") from exc

    def drain_stderr() -> None:
        if process.stderr is not None:
            errors.extend(process.stderr)

    reader = threading.Thread(target=drain_stderr, daemon=True)
    reader.start()

    # Reading FFmpeg's progress stream blocks until it exits, so a timeout
    # cannot be enforced by the read loop - it needs something that can act
    # while we are blocked.
    expired = threading.Event()

    def give_up() -> None:
        expired.set()
        process.kill()

    watchdog = threading.Timer(timeout, give_up) if timeout else None
    if watchdog is not None:
        watchdog.start()

    try:
        if process.stdout is not None:
            for line in process.stdout:
                seconds = _progress_seconds(line)
                if seconds is not None and total_seconds > 0:
                    on_progress(max(0.0, min(seconds / total_seconds, 1.0)))
        process.wait()
    finally:
        if watchdog is not None:
            watchdog.cancel()
        reader.join(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    if expired.is_set():
        raise ToolExecutionError(name, -1, f"{name} timed out after {timeout} seconds.")

    result = CommandResult(
        args=tuple(argv), returncode=process.returncode, stdout="", stderr="".join(errors)
    )
    if not result.ok:
        raise ToolExecutionError(name, result.returncode, result.stderr)
    return result


def _progress_seconds(line: str) -> float | None:
    """Seconds of output written, from one ``-progress`` line.

    ``out_time_ms`` is a long-standing FFmpeg misnomer - it carries
    microseconds - so the explicit ``out_time_us`` is preferred where present
    and both are divided by a million.
    """
    key, _, value = line.strip().partition("=")
    if key not in {"out_time_us", "out_time_ms"}:
        return None
    try:
        return int(value) / 1_000_000
    except ValueError:
        return None
