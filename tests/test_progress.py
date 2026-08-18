"""Progress reporting: the bar, the percentage and the estimate."""

from __future__ import annotations

import io
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from ecompress.process import _progress_seconds, run_with_progress
from ecompress.reporting import ConsoleReporter, NullReporter

from .conftest import requires_x264

Copier = Callable[..., Path]


class _Tty(io.StringIO):
    """A stream that claims to be a terminal, so the bar is drawn."""

    def isatty(self) -> bool:
        return True


def frames(stream: io.StringIO) -> list[str]:
    return [part.rstrip() for part in stream.getvalue().split("\r") if part.strip()]


# -- parsing FFmpeg's progress stream --------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("out_time_us=4000000", 4.0),
        ("out_time_ms=1500000", 1.5),
        ("out_time_us=0", 0.0),
        ("out_time_us=90500000\n", 90.5),
    ],
)
def test_progress_lines_are_read_as_microseconds(line: str, expected: float) -> None:
    """`out_time_ms` is an FFmpeg misnomer - it carries microseconds."""
    assert _progress_seconds(line) == pytest.approx(expected)


@pytest.mark.parametrize(
    "line",
    ["frame=100", "progress=continue", "speed=1.02x", "out_time=00:00:04.00", "", "garbage"],
)
def test_other_progress_lines_are_ignored(line: str) -> None:
    assert _progress_seconds(line) is None


def test_unparsable_timestamp_is_ignored() -> None:
    assert _progress_seconds("out_time_us=N/A") is None


# -- drawing ---------------------------------------------------------------


def test_the_bar_fills_as_the_encode_advances() -> None:
    stream = _Tty()
    reporter = ConsoleReporter(stream)

    for fraction in (0.0, 0.5, 1.0):
        reporter.progress(fraction)

    drawn = frames(stream)
    assert "  0%" in drawn[0]
    assert " 50%" in drawn[1]
    assert "100%" in drawn[2]
    assert drawn[0].count("#") == 0
    assert drawn[2].count(".") == 0
    assert drawn[1].count("#") == drawn[1].count(".")


def test_an_estimate_appears_once_there_is_enough_to_go_on() -> None:
    stream = _Tty()
    reporter = ConsoleReporter(stream)
    reporter._started = time.monotonic() - 10.0

    reporter.progress(0.5)

    # Half done after 10s means about 10s left.
    assert "left" in frames(stream)[-1]


def test_no_estimate_is_shown_before_it_would_be_meaningful() -> None:
    """Early on the rate is dominated by start-up and would read wildly wrong."""
    stream = _Tty()
    reporter = ConsoleReporter(stream)
    reporter.progress(0.001)
    assert "left" not in frames(stream)[-1]


def test_nothing_is_drawn_when_output_is_not_a_terminal() -> None:
    """Piped into a file or another program, a redrawn bar is just noise."""
    stream = io.StringIO()
    reporter = ConsoleReporter(stream)

    reporter.progress(0.5)
    reporter.progress_done()

    assert stream.getvalue() == ""


def test_progress_is_cleared_before_ordinary_output() -> None:
    """A note must not be written on top of a half-drawn bar."""
    stream = _Tty()
    reporter = ConsoleReporter(stream)

    reporter.progress(0.5)
    reporter.note("switching approach")

    assert "switching approach" in stream.getvalue().splitlines()[-1]


def test_out_of_range_fractions_are_clamped() -> None:
    stream = _Tty()
    reporter = ConsoleReporter(stream)
    reporter.progress(-1.0)
    reporter.progress(5.0)
    drawn = frames(stream)
    assert "  0%" in drawn[0]
    assert "100%" in drawn[1]


def test_the_silent_reporter_accepts_progress() -> None:
    reporter = NullReporter()
    reporter.progress(0.5)
    reporter.progress_done()


# -- end to end ------------------------------------------------------------


@requires_x264
@pytest.mark.slow
def test_ffmpeg_progress_reaches_the_callback(tmp_path: Path) -> None:
    """The real thing: FFmpeg's pipe must drive the fraction."""
    from ecompress.ffmpeg import find_ffmpeg_tools

    ffmpeg = find_ffmpeg_tools().ffmpeg
    assert ffmpeg is not None
    out = tmp_path / "progress.mp4"

    seen: list[float] = []
    run_with_progress(
        [
            str(ffmpeg),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            "-nostats",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=30:duration=4",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ],
        total_seconds=4.0,
        on_progress=seen.append,
        timeout=120,
        tool="ffmpeg",
    )

    assert out.exists() and out.stat().st_size > 0
    assert seen, "no progress was reported"
    assert all(0.0 <= f <= 1.0 for f in seen)
    assert seen == sorted(seen), "progress went backwards"
    assert seen[-1] > 0.5, f"never got close to the end: {seen[-1]}"


@requires_x264
@pytest.mark.slow
def test_a_failing_encode_still_reports_its_error(tmp_path: Path) -> None:
    from ecompress.errors import ToolExecutionError
    from ecompress.ffmpeg import find_ffmpeg_tools

    ffmpeg = find_ffmpeg_tools().ffmpeg
    assert ffmpeg is not None

    with pytest.raises(ToolExecutionError):
        run_with_progress(
            [
                str(ffmpeg),
                "-y",
                "-progress",
                "pipe:1",
                "-i",
                str(tmp_path / "nope.mp4"),
                str(tmp_path / "out.mp4"),
            ],
            total_seconds=1.0,
            on_progress=lambda _f: None,
            timeout=60,
            tool="ffmpeg",
        )
