"""The command language shared by the PDF and video editing commands.

One grammar for every operation::

    <operation> "<path>"[<range>] ... -> "<folder or file>"

    add_pdf   "D:/a.pdf"[2-9] "D:/b.pdf"[7-16] -> "D:/out"
    cut_pdf   "C:/My Documents/book.pdf"[2-5,8-12,20] -> "D:/out"
    add_video "a.mp4"[00:00-00:30] "b.mp4"[01:10-02:00] -> "D:/out"
    cut_video "movie.mp4"[00:02:10-00:05:30]

``-> "folder"`` is optional, and so are the quotes around paths - which
matters, because shells remove them before a program sees its arguments:
bash hands over ``D:/My Docs/a.pdf[2-9]``, and Windows PowerShell 5.1 can even
split one argument at its spaces. The console scripts therefore join their
arguments back into one line, and an unquoted path is read up to its
``[range]`` - or, for an input without a range, up to where it names a file
that exists.
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

EXAMPLES = {
    "add_pdf": 'add_pdf "D:/a.pdf"[2-9] "D:/b.pdf"[7-16] -> "D:/out"',
    "cut_pdf": 'cut_pdf "C:/My Documents/book.pdf"[2-5,8-12,20] -> "D:/out"',
    "add_video": 'add_video "a.mp4"[00:00-00:30] "b.mp4"[01:10-02:00] -> "D:/out"',
    "cut_video": 'cut_video "movie.mp4"[00:02:10-00:05:30] -> "D:/out"',
}

_OPERATION_WORD = re.compile(r"\s*([A-Za-z_]\w*)(?=\s|$)")
# A [range] closing an unquoted input: followed by whitespace or the end.
_RANGE_AT_WORD_END = re.compile(r"\[([^\[\]]*)\](?=\s|$)")
_NON_SPACE = re.compile(r"\S+")


@dataclass(frozen=True)
class Source:
    """One input. ``spec`` is the text inside ``[...]``, or ``None`` without one."""

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
            raise CommandSyntaxError(_unknown(self.operation))
        verb, kind = self.operation.split("_", 1)
        example = EXAMPLES[self.operation]
        if not self.sources:
            raise CommandSyntaxError(f"No input file given. Example:\n  {example}")
        if verb == "cut":
            if len(self.sources) != 1:
                raise CommandSyntaxError(
                    f"{self.operation} takes exactly one file; "
                    f"use add_{kind} to combine several. Example:\n  {example}"
                )
            if self.sources[0].spec is None:
                raise CommandSyntaxError(
                    f"{self.operation} needs a range in [...]. Example:\n  {example}"
                )
        elif len(self.sources) < 2:
            raise CommandSyntaxError(
                f"add_{kind} combines two or more files; to take part of one "
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
        raise CommandSyntaxError(_unknown(operation))
    text = text.strip()
    if not text:
        raise CommandSyntaxError(
            f"The command is empty. Example:\n  {EXAMPLES[operation or 'cut_pdf']}"
        )

    pos = 0
    word = _OPERATION_WORD.match(text)
    if word is not None and word.group(1) in OPERATIONS:
        if operation is not None and word.group(1) != operation:
            raise CommandSyntaxError(
                f"This is the {operation} command, but the text says {word.group(1)}."
            )
        operation = word.group(1)
        pos = word.end()
    if operation is None:
        if word is not None:
            raise CommandSyntaxError(_unknown(word.group(1)))
        raise CommandSyntaxError(
            "The command must start with an operation: " + ", ".join(OPERATIONS) + "."
        )

    inputs, target = _split_arrow(text, pos)
    sources = _read_inputs(inputs)
    output = None if target is None else _read_output(target)
    return Command(operation, tuple(sources), output)


def _unknown(name: str) -> str:
    return f"Unknown operation '{name}'. Expected one of: {', '.join(OPERATIONS)}."


def _skip_space(text: str, pos: int) -> int:
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return pos


def _excerpt(text: str) -> str:
    return repr(text if len(text) <= 40 else text[:40] + "...")


def _is_file(text: str) -> bool:
    try:
        return Path(text).expanduser().is_file()
    except (OSError, ValueError):
        return False


def _split_arrow(text: str, pos: int) -> tuple[str, str | None]:
    """Split at the ``->`` that is not inside quotes or a range.

    A quote only opens at the start of a word, so the apostrophe in an
    unquoted ``Tom's video.mp4`` is just a character. ``->`` must start a
    word or follow ``]`` or a closing quote, so ``a->b.pdf`` is a file name.
    """
    arrow: int | None = None
    quote: str | None = None
    in_range = False
    index = pos
    while index < len(text):
        char = text[index]
        starts_word = index == pos or text[index - 1].isspace()
        if quote is not None:
            if char == quote:
                quote = None
        elif char in "\"'" and starts_word:
            quote = char
        elif char == "[":
            in_range = True
        elif char == "]":
            in_range = False
        elif (
            not in_range
            and text.startswith("->", index)
            and (starts_word or text[index - 1] in "]\"'")
        ):
            if arrow is not None:
                raise CommandSyntaxError("'->' appears more than once.")
            arrow = index
            index += 2
            continue
        index += 1
    if arrow is None:
        return text[pos:], None
    return text[pos:arrow], text[arrow + 2 :]


def _read_output(target: str) -> Path:
    value = target.strip()
    if value[:1] in {'"', "'"}:
        close = value.find(value[0], 1)
        if close < 0:
            raise CommandSyntaxError(f"The output folder is missing its closing {value[0]}.")
        rest = value[close + 1 :].strip()
        if rest:
            raise CommandSyntaxError(
                f"Nothing may follow the output folder; found {_excerpt(rest)}."
            )
        value = value[1:close]
    if not value.strip():
        raise CommandSyntaxError("'->' must be followed by a folder, e.g. -> \"D:/out\".")
    return Path(value).expanduser()


def _read_inputs(text: str) -> list[Source]:
    sources: list[Source] = []
    pos = 0
    while True:
        pos = _skip_space(text, pos)
        if pos >= len(text):
            return sources
        char = text[pos]
        if char in "\"'":
            close = text.find(char, pos + 1)
            if close < 0:
                raise CommandSyntaxError(
                    f"The path {_excerpt(text[pos:])} is missing its closing {char}."
                )
            raw = text[pos + 1 : close]
            pos = close + 1
            spec: str | None = None
            after = _skip_space(text, pos)
            if after < len(text) and text[after] == "[":
                end = text.find("]", after)
                if end < 0:
                    raise CommandSyntaxError(f"\"{raw}\": the range is missing its closing ']'.")
                spec = text[after + 1 : end]
                pos = end + 1
            if pos < len(text) and not text[pos].isspace():
                raise CommandSyntaxError(
                    f'Unexpected text right after "{raw}": {_excerpt(text[pos:])}. '
                    "Separate inputs with a space."
                )
        elif char == "[":
            raise CommandSyntaxError(
                f"A range needs a file in front of it: {_excerpt(text[pos:])}."
            )
        else:
            raw, spec, pos = _read_bare(text, pos)
        if not raw.strip():
            raise CommandSyntaxError("A path is empty.")
        sources.append(Source(Path(raw).expanduser(), spec))


def _read_bare(text: str, pos: int) -> tuple[str, str | None, int]:
    """Read one unquoted input starting at ``pos``: ``(path, spec, new_pos)``.

    Spaces are ambiguous here - ``C:/My Docs/a.pdf`` is one path, ``a.pdf
    b.pdf`` is two - so where they appear the file system decides: an input
    ends at the first space where the text so far is an existing file. Paths
    that do not exist cannot be told apart that way, and fall back to the
    first ``[range]`` (or, with no range, the first word) so the error that
    follows names what was typed.
    """
    rest = text[pos:]
    ranges = [match for match in _RANGE_AT_WORD_END.finditer(rest) if match.start() > 0]
    chosen = next((m for m in ranges if _is_file(rest[: m.start()])), None)
    if chosen is None and ranges:
        chosen = ranges[0]

    path_end = chosen.start() if chosen is not None else len(rest)
    for index in range(path_end):
        if rest[index].isspace() and _is_file(rest[:index]):
            return rest[:index], None, pos + index

    if chosen is not None:
        return rest[: chosen.start()], chosen.group(1), pos + chosen.end()
    whole = rest.rstrip()
    if _is_file(whole):
        return whole, None, pos + len(rest)
    match = _NON_SPACE.match(rest)
    token = match.group() if match else rest
    if "[" in token and "]" not in token[token.index("[") :]:
        raise CommandSyntaxError(f"'{token}': the range is missing its closing ']'.")
    return token, None, pos + len(token)


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
