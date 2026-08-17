"""Progress reporting hooks.

The core never prints anything itself; it pushes events to a
:class:`Reporter`. The CLI supplies :class:`ConsoleReporter`, the Python API
defaults to :class:`NullReporter`.
"""

from __future__ import annotations

import sys
from typing import IO

from compress.result import Attempt
from compress.units import format_size

__all__ = ["ConsoleReporter", "NullReporter", "Reporter"]


class Reporter:
    """Base reporter. Every method is a no-op; override what you need."""

    def step(self, message: str) -> None:
        """A short status line, e.g. ``"Video detected."``."""

    def attempt(self, attempt: Attempt) -> None:
        """One measured encode finished."""

    def note(self, message: str) -> None:
        """Something the user should know about, e.g. a format change."""


class NullReporter(Reporter):
    """Silent reporter used by the Python API."""


class ConsoleReporter(Reporter):
    """Writes the human-readable progress shown by the ``compress`` command."""

    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream: IO[str] = stream if stream is not None else sys.stdout

    def _write(self, text: str) -> None:
        self._stream.write(text + "\n")
        self._stream.flush()

    def step(self, message: str) -> None:
        self._write(message)

    def attempt(self, attempt: Attempt) -> None:
        size = format_size(attempt.size_bytes)
        if not attempt.valid:
            self._write(f"  Attempt {attempt.index}: rejected ({attempt.note or 'invalid output'})")
            return
        marker = "  <- best so far" if attempt.accepted else ""
        detail = f" [{attempt.note}]" if attempt.note else ""
        self._write(f"  Attempt {attempt.index}: {size}{detail}{marker}")

    def note(self, message: str) -> None:
        self._write(f"  Note: {message}")
