"""The command language shared by the PDF and video editing commands.

One grammar for every operation::

    <operation> <label>(<path>)[<range>] ... -> output(<folder or file>)

    add_pdf   pdf1("a.pdf")[2-9] pdf2("b.pdf")[7-16] -> output("D:/out")
    cut_pdf   pdf("book.pdf")[2-5,8-12,20]
    add_video video1("a.mp4")[00:10-01:20] video2("b.mp4")[02:00-03:30]
    cut_video video("movie.mp4")[01:20-05:40]

Paths may be quoted with ``"`` or ``'``. Unquoted paths are accepted too,
because Windows PowerShell 5.1 strips embedded double quotes from arguments
before a program ever sees them; quoting is only *needed* when a path contains
``)`` followed by something that looks like the next part of the command.

Ranges are typed by people (or written by an LLM), so every error names the
offending text and says what was expected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from ecompress.errors import CommandSyntaxError

__all__ = [
    "OPERATIONS",
    "Command",
    "Segment",
    "Source",
    "format_timestamp",
    "parse_command",
    "parse_page_ranges",
    "parse_time_ranges",
    "parse_timestamp",
]

OPERATIONS = ("add_pdf", "cut_pdf", "add_video", "cut_video")

#: A range may end this far past the real duration and is quietly clamped:
#: containers round durations, and people round timestamps.
END_TOLERANCE_SECONDS = 0.5

_EXAMPLES = {
    "add_pdf": 'add_pdf pdf1("a.pdf")[2-9] pdf2("b.pdf")[7-16] -> output("folder")',
    "cut_pdf": 'cut_pdf pdf("book.pdf")[7-16] -> output("folder")',
    "add_video": 'add_video video1("a.mp4")[00:00-00:30] video2("b.mp4")[01:10-02:00]',
    "cut_video": 'cut_video video("movie.mp4")[00:02:10-00:05:30] -> output("folder")',
}

_WORD = re.compile(r"\s*([A-Za-z_]\w*)")
_LABEL = re.compile(r"\s*([A-Za-z_]\w*)\s*\(")
# Where an unquoted path ends: a ")" followed by a range, the arrow, the end of
# the command, or the next label.
_UNQUOTED_END = re.compile(r"\)(?=\s*(?:\[|->|$)|\s+[A-Za-z_]\w*\s*\()")
_LABEL_KIND = re.compile(r"(pdf|video)\d*")


@dataclass(frozen=True)
class Source:
    """One input: ``label(path)[spec]``. ``spec`` is ``None`` when no range was given."""

    label: str
    path: Path
    spec: str | None = None


@dataclass(frozen=True)
class Command:
    """A parsed command. Construction validates it, so every instance is usable."""

    operation: str
    sources: tuple[Source, ...]
    output: Path | None = None

    def __post_init__(self) -> None:
        if self.operation not in OPERATIONS:
            raise CommandSyntaxError(
                f"Unknown operation '{self.operation}'. Expected one of: {', '.join(OPERATIONS)}."
            )
        verb, kind = self.operation.split("_", 1)
        example = _EXAMPLES[self.operation]
        if not self.sources:
            raise CommandSyntaxError(f"No input given. Example:\n  {example}")
        for source in self.sources:
            match = _LABEL_KIND.fullmatch(source.label.lower())
            if match is None or match.group(1) != kind:
                raise CommandSyntaxError(
                    f"{self.operation} takes {kind}(...) inputs, not {source.label}(...). "
                    f"Example:\n  {example}"
                )
        if verb == "cut":
            if len(self.sources) != 1:
                raise CommandSyntaxError(
                    f"{self.operation} takes exactly one {kind}; "
                    f"use add_{kind} to combine several. Example:\n  {example}"
                )
            if self.sources[0].spec is None:
                raise CommandSyntaxError(
                    f"{self.operation} needs a range in [...]. Example:\n  {example}"
                )
        elif len(self.sources) < 2:
            raise CommandSyntaxError(
                f"add_{kind} combines two or more inputs; to take part of one "
                f"use cut_{kind}. Example:\n  {example}"
            )

    @property
    def kind(self) -> str:
        """``"pdf"`` or ``"video"``."""
        return self.operation.split("_", 1)[1]


def parse_command(text: str, *, operation: str | None = None) -> Command:
    """Parse ``text`` into a :class:`Command`.

    ``operation`` supplies the verb when the text leaves it out, which is how
    the ``cut_pdf`` etc. console scripts call this. If the text *does* start
    with an operation it must agree.
    """
    if operation is not None and operation not in OPERATIONS:
        raise CommandSyntaxError(
            f"Unknown operation '{operation}'. Expected one of: {', '.join(OPERATIONS)}."
        )
    text = text.strip()
    if not text:
        example = _EXAMPLES[operation] if operation else _EXAMPLES["cut_pdf"]
        raise CommandSyntaxError(f"The command is empty. Example:\n  {example}")

    pos = 0
    word = _WORD.match(text)
    if word is not None and not text[word.end() :].lstrip().startswith("("):
        name = word.group(1)
        if name not in OPERATIONS:
            raise CommandSyntaxError(
                f"Unknown operation '{name}'. Expected one of: {', '.join(OPERATIONS)}."
            )
        if operation is not None and name != operation:
            raise CommandSyntaxError(f"This is the {operation} command, but the text says {name}.")
        operation = name
        pos = word.end()
    if operation is None:
        raise CommandSyntaxError(
            "The command must start with an operation: " + ", ".join(OPERATIONS) + "."
        )

    sources: list[Source] = []
    output: Path | None = None
    arrow = False
    while True:
        pos = _skip_space(text, pos)
        if pos >= len(text):
            break
        if output is not None:
            raise CommandSyntaxError(
                f"Nothing may follow output(...), found: {_excerpt(text, pos)}"
            )
        if text.startswith("->", pos):
            if arrow:
                raise CommandSyntaxError("'->' appears more than once.")
            arrow = True
            pos += 2
            continue

        label_match = _LABEL.match(text, pos)
        if label_match is None:
            raise CommandSyntaxError(
                f'Expected something like {operation.split("_")[1]}("path")[range] '
                f"at: {_excerpt(text, pos)}\nExample:\n  {_EXAMPLES[operation]}"
            )
        label = label_match.group(1)
        path, pos = _read_path(text, label_match.end(), label)

        spec: str | None = None
        after = _skip_space(text, pos)
        if after < len(text) and text[after] == "[":
            close = text.find("]", after)
            if close < 0:
                raise CommandSyntaxError(f"{label}(...): the range is missing its closing ']'.")
            spec = text[after + 1 : close]
            pos = close + 1

        if label.lower() == "output":
            if spec is not None:
                raise CommandSyntaxError("output(...) takes a folder or file, not a [range].")
            output = path
        else:
            if arrow:
                raise CommandSyntaxError(
                    f"Inputs must come before '->'; found {label}(...) after it."
                )
            sources.append(Source(label, path, spec))

    if arrow and output is None:
        raise CommandSyntaxError("'->' must be followed by output(\"folder\").")
    return Command(operation, tuple(sources), output)


def _skip_space(text: str, pos: int) -> int:
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return pos


def _excerpt(text: str, pos: int) -> str:
    rest = text[pos:]
    return repr(rest if len(rest) <= 40 else rest[:40] + "...")


def _read_path(text: str, pos: int, label: str) -> tuple[Path, int]:
    """Read the path inside ``label(...)``; ``pos`` is just after the ``(``."""
    pos = _skip_space(text, pos)
    if pos < len(text) and text[pos] in "\"'":
        quote = text[pos]
        close = text.find(quote, pos + 1)
        if close < 0:
            raise CommandSyntaxError(f"{label}(...): the path is missing its closing {quote}.")
        raw = text[pos + 1 : close]
        end = _skip_space(text, close + 1)
        if end >= len(text) or text[end] != ")":
            raise CommandSyntaxError(f"{label}(...): expected ')' after the quoted path.")
        end += 1
    else:
        match = _UNQUOTED_END.search(text, pos)
        if match is None:
            raise CommandSyntaxError(f"{label}(...): missing ')' after the path.")
        raw = text[pos : match.start()].strip()
        end = match.end()
    if not raw.strip():
        raise CommandSyntaxError(f"{label}(): the path is empty.")
    return Path(raw).expanduser(), end


# -- PDF page ranges ----------------------------------------------------------


def parse_page_ranges(spec: str | None, page_count: int, *, name: str = "the PDF") -> list[int]:
    """Zero-based page indices for a spec like ``"2-5,8-12,20"``.

    Pages are 1-based and inclusive, as a reader shows them. ``7-`` and
    ``7-end`` run to the last page, ``16-7`` runs backwards, and a page may be
    repeated. ``None`` (no brackets) or ``all`` means every page.
    """
    if spec is None or spec.strip().lower() in {"all", "*"}:
        return list(range(page_count))
    if not spec.strip():
        raise CommandSyntaxError(f"{name}: the page range [] is empty; write e.g. [2-9] or [all].")

    pages: list[int] = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            raise CommandSyntaxError(f"{name}: empty entry in page range [{spec}].")
        if "-" in item:
            first_text, _, last_text = item.partition("-")
            first = _page_number(first_text, page_count, name, default=1)
            last = _page_number(last_text, page_count, name, default=page_count)
        else:
            first = last = _page_number(item, page_count, name, default=None)
        step = 1 if last >= first else -1
        pages.extend(range(first - 1, last - 1 + step, step))
    return pages


def _page_number(text: str, page_count: int, name: str, *, default: int | None) -> int:
    text = text.strip().lower()
    if not text and default is not None:
        return default
    if text in {"end", "last"}:
        return page_count
    if not text.isdigit():
        raise CommandSyntaxError(
            f"{name}: '{text}' is not a page number. Use forms like 7, 2-9, 7-end or 2-5,8-12,20."
        )
    number = int(text)
    if not 1 <= number <= page_count:
        plural = "page" if page_count == 1 else "pages"
        raise CommandSyntaxError(
            f"{name}: page {number} does not exist; it has {page_count} {plural}."
        )
    return number


# -- video time ranges --------------------------------------------------------


class Segment(NamedTuple):
    """A span of a video, in seconds."""

    start: float
    end: float

    @property
    def length(self) -> float:
        return self.end - self.start


_SECONDS = re.compile(r"\d+(?:\.\d+)?")
_MM_SS = re.compile(r"(\d+):(\d{1,2}(?:\.\d+)?)")
_HH_MM_SS = re.compile(r"(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)")


def parse_timestamp(text: str) -> float:
    """Seconds from ``130``, ``130.5``, ``02:10``, ``00:02:10`` or ``1:02:03.250``."""
    value = text.strip()
    if _SECONDS.fullmatch(value):
        return float(value)
    match = _MM_SS.fullmatch(value)
    if match:
        minutes, seconds = int(match.group(1)), float(match.group(2))
        if seconds >= 60:
            raise CommandSyntaxError(f"'{text}': seconds must be below 60.")
        return minutes * 60 + seconds
    match = _HH_MM_SS.fullmatch(value)
    if match:
        hours, minutes, seconds = int(match.group(1)), int(match.group(2)), float(match.group(3))
        if minutes >= 60 or seconds >= 60:
            raise CommandSyntaxError(f"'{text}': minutes and seconds must be below 60.")
        return hours * 3600 + minutes * 60 + seconds
    raise CommandSyntaxError(
        f"'{text}' is not a time. Use seconds (130), mm:ss (02:10) or hh:mm:ss (00:02:10)."
    )


def parse_time_ranges(
    spec: str | None, duration: float | None, *, name: str = "the video"
) -> list[Segment]:
    """Segments for a spec like ``"00:10-01:20,02:00-end"``.

    ``01:20-`` and ``01:20-end`` run to the end; ``-00:30`` starts at zero.
    ``None`` (no brackets) or ``all`` is the whole video.
    """
    if spec is None or spec.strip().lower() in {"all", "*"}:
        if not duration:
            raise CommandSyntaxError(f"{name}: its duration is unknown, so give an explicit range.")
        return [Segment(0.0, duration)]
    if not spec.strip():
        raise CommandSyntaxError(f"{name}: the time range [] is empty; write e.g. [00:10-01:20].")

    segments: list[Segment] = []
    for item in spec.split(","):
        item = item.strip()
        pieces = item.split("-")
        if len(pieces) != 2:
            raise CommandSyntaxError(
                f"{name}: '{item}' is not a range. Write start-end, e.g. 00:10-01:20 or 130-330."
            )
        start_text, end_text = (piece.strip() for piece in pieces)
        start = parse_timestamp(start_text) if start_text else 0.0
        if end_text.lower() in {"", "end"}:
            if not duration:
                raise CommandSyntaxError(
                    f"{name}: its duration is unknown, so '{item}' needs an explicit end."
                )
            end = duration
        else:
            end = parse_timestamp(end_text)

        if duration:
            if start >= duration:
                raise CommandSyntaxError(
                    f"{name}: {format_timestamp(start)} is past the end; "
                    f"it is only {format_timestamp(duration)} long."
                )
            if end > duration + END_TOLERANCE_SECONDS:
                raise CommandSyntaxError(
                    f"{name}: {format_timestamp(end)} is past the end; "
                    f"it is only {format_timestamp(duration)} long."
                )
            end = min(end, duration)
        if end <= start:
            raise CommandSyntaxError(f"{name}: in '{item}' the end must be after the start.")
        segments.append(Segment(start, end))
    return segments


def format_timestamp(seconds: float) -> str:
    """``2:10``, ``1:02:03`` or ``0:04.5``: compact, with fractions only when present."""
    millis = round(max(seconds, 0.0) * 1000)
    hours, rest = divmod(millis, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    text = f"{secs:02d}"
    if millis:
        text += f".{millis:03d}".rstrip("0")
    if hours:
        return f"{hours}:{minutes:02d}:{text}"
    return f"{minutes}:{text}"
