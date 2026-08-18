"""Progress reporting hooks.

The core never prints anything itself; it pushes events to a
:class:`Reporter`. The CLI supplies :class:`ConsoleReporter`, the Python API
defaults to :class:`NullReporter`.
"""

from __future__ import annotations

import shutil
import sys
import time
from typing import IO

from ecompress.result import Attempt
from ecompress.units import format_size

__all__ = ["ConsoleReporter", "NullReporter", "Reporter"]


class Reporter:
    """Base reporter. Every method is a no-op; override what you need."""

    def step(self, message: str) -> None:
        """A short status line, e.g. ``"Video detected."``."""

    def attempt(self, attempt: Attempt) -> None:
        """One measured encode finished."""

    def note(self, message: str) -> None:
        """Something the user should know about, e.g. a format change."""

    def progress(self, fraction: float, *, label: str = "") -> None:
        """An encode is ``fraction`` of the way through the clip (0.0 to 1.0)."""

    def progress_done(self) -> None:
        """The encode finished; tear down any progress display."""


class NullReporter(Reporter):
    """Silent reporter used by the Python API."""


class ConsoleReporter(Reporter):
    """Writes the human-readable progress shown by the ``compress`` command."""

    #: Width of the drawn bar, in characters.
    BAR_WIDTH = 24

    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream: IO[str] = stream if stream is not None else sys.stdout
        # A progress bar redraws one line with carriage returns. Piped into a
        # file or another program that is just noise, so it is only drawn for
        # a real terminal.
        self._live = bool(getattr(self._stream, "isatty", lambda: False)())
        self._started: float | None = None
        self._drawn = False

    def _write(self, text: str) -> None:
        self._stream.write(text + "\n")
        self._stream.flush()

    def step(self, message: str) -> None:
        self._clear()
        self._write(message)

    def attempt(self, attempt: Attempt) -> None:
        self._clear()
        size = format_size(attempt.size_bytes)
        if not attempt.valid:
            self._write(f"  Attempt {attempt.index}: rejected ({attempt.note or 'invalid output'})")
            return
        marker = "  <- best so far" if attempt.accepted else ""
        detail = f" [{attempt.note}]" if attempt.note else ""
        self._write(f"  Attempt {attempt.index}: {size}{detail}{marker}")

    def note(self, message: str) -> None:
        self._clear()
        self._write(f"  Note: {message}")

    def progress(self, fraction: float, *, label: str = "") -> None:
        if not self._live:
            return
        now = time.monotonic()
        if self._started is None:
            self._started = now

        fraction = max(0.0, min(fraction, 1.0))
        filled = round(fraction * self.BAR_WIDTH)
        bar = "#" * filled + "." * (self.BAR_WIDTH - filled)

        elapsed = now - self._started
        remaining = ""
        # Below a few percent the rate estimate is dominated by start-up cost
        # and would show a wildly wrong number, so wait until it settles.
        if fraction > 0.03 and elapsed > 1.0:
            remaining = f"  {_clock((elapsed / fraction) * (1.0 - fraction))} left"

        text = f"  [{bar}] {fraction * 100:3.0f}%{remaining}"
        if label:
            text += f"  {label}"
        self._stream.write("\r" + text.ljust(self._width)[: self._width])
        self._stream.flush()
        self._drawn = True

    def progress_done(self) -> None:
        self._clear()
        self._started = None

    # -- internals --------------------------------------------------------

    @property
    def _width(self) -> int:
        return max(shutil.get_terminal_size((80, 24)).columns - 1, 40)

    def _clear(self) -> None:
        """Wipe the progress line so ordinary output is not written over it."""
        if self._drawn:
            self._stream.write("\r" + " " * self._width + "\r")
            self._stream.flush()
            self._drawn = False


def _clock(seconds: float) -> str:
    """Seconds as ``m:ss`` or ``h:mm:ss``."""
    total = int(max(seconds, 0))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
