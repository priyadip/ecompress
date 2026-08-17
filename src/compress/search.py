"""Search strategies for the constrained optimisation problem.

Every backend solves the same problem::

    maximise  quality
    subject to  output_size < target_size

The size of an encode is monotonically non-decreasing in the "quality knob"
(JPEG quality, video bitrate, audio bitrate, PDF image resolution), so the
largest knob value that still fits under the limit is the best answer. Two
strategies are provided:

``search_discrete_ladder``
    Binary search over an ordered list of candidate settings. Used when the
    knob is a small enumerated set (JPEG quality 1-95, PDF presets).

``search_proportional``
    Secant-style search for bitrate, where output size is close to linear in
    the knob. Converges in two or three encodes instead of ``log2(n)``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

__all__ = [
    "Candidate",
    "SearchOutcome",
    "search_discrete_ladder",
    "search_proportional",
]

T = TypeVar("T")

#: An encode either produced a measured size, or failed/was invalid (``None``).
Encoder = Callable[[T], int | None]


@dataclass(frozen=True)
class Candidate(Generic[T]):
    """One measured encode."""

    setting: T
    size_bytes: int


@dataclass
class SearchOutcome(Generic[T]):
    """What a search converged on."""

    best: Candidate[T] | None = None
    """Largest measured result that is strictly under the limit."""

    smallest: Candidate[T] | None = None
    """Smallest valid result seen at all - used for "not achievable" errors."""

    evaluations: int = 0

    def record(self, setting: T, size_bytes: int, limit: int) -> bool:
        """Register a measurement. Returns True if it fits under ``limit``."""
        self.evaluations += 1
        candidate = Candidate(setting, size_bytes)
        if self.smallest is None or size_bytes < self.smallest.size_bytes:
            self.smallest = candidate
        fits = size_bytes < limit
        if fits and (self.best is None or size_bytes > self.best.size_bytes):
            self.best = candidate
        return fits


def search_discrete_ladder(
    settings: Sequence[T],
    encode: Encoder[T],
    *,
    limit: int,
    max_evaluations: int = 10,
    good_enough_ratio: float = 0.90,
    outcome: SearchOutcome[T] | None = None,
) -> SearchOutcome[T]:
    """Binary-search an ascending-quality ladder for the best setting that fits.

    Args:
        settings: candidate settings ordered from *lowest* quality (smallest
            output) to *highest* quality (largest output).
        encode: produces a file for a setting and returns its size in bytes, or
            ``None`` when that setting could not produce a valid file.
        limit: the hard ceiling; results must be strictly below it.
        max_evaluations: never call ``encode`` more than this many times.
        good_enough_ratio: stop early once a result lands in
            ``[ratio * limit, limit)`` - it is close enough to the ceiling that
            further searching would not meaningfully improve quality.
        outcome: reuse an existing outcome (lets a caller run several ladders,
            e.g. one per resolution, and keep the global best).

    Returns:
        The :class:`SearchOutcome`; ``outcome.best`` is ``None`` when nothing fit.
    """
    result = outcome if outcome is not None else SearchOutcome()
    if not settings or max_evaluations <= 0:
        return result

    # Start at the top: the best possible quality is the answer whenever it fits.
    low, high = 0, len(settings) - 1
    probe = high
    target_floor = int(limit * good_enough_ratio)
    # Budget is per call: a shared ``outcome`` accumulates across several
    # ladders (one per resolution), and its running total must not starve them.
    spent = 0

    while low <= high and spent < max_evaluations:
        spent += 1
        size = encode(settings[probe])
        if size is None:
            # Treat an unusable encode as "too big" so we back off on quality.
            result.evaluations += 1
            high = probe - 1
        elif result.record(settings[probe], size, limit):
            if size >= target_floor:
                return result  # close enough to the ceiling; stop burning encodes
            low = probe + 1
        else:
            high = probe - 1

        if low > high:
            break
        probe = (low + high) // 2

    return result


def search_proportional(
    encode: Callable[[int], int | None],
    *,
    limit: int,
    initial: int,
    minimum: int,
    maximum: int,
    fixed_overhead_bytes: int = 0,
    max_evaluations: int = 6,
    good_enough_ratio: float = 0.88,
    aim_ratio: float = 0.97,
    outcome: SearchOutcome[int] | None = None,
) -> SearchOutcome[int]:
    """Search a bitrate-like knob where size is roughly linear in the setting.

    After each encode the next bitrate is estimated from the measured result::

        next = current * (desired_payload_bytes / measured_payload_bytes)

    where the payload excludes ``fixed_overhead_bytes`` (container headers and,
    for video, the audio track) which do not scale with the knob.

    Args:
        encode: encodes at a bitrate in bits/second, returning the file size or
            ``None`` if the encode failed.
        limit: hard ceiling in bytes; results must be strictly below it.
        initial: first bitrate to try, in bits/second.
        minimum: lowest bitrate worth attempting; below this the output is not
            usable and the search stops.
        maximum: never exceed this (e.g. the source bitrate).
        fixed_overhead_bytes: part of the output that does not scale.
        max_evaluations: encode budget.
        good_enough_ratio: stop once a result lands in ``[ratio * limit, limit)``.
        aim_ratio: aim for this fraction of the limit, leaving a safety margin.
        outcome: reuse an existing outcome to keep a global best.
    """
    result = outcome if outcome is not None else SearchOutcome()
    if max_evaluations <= 0 or minimum > maximum:
        return result

    aim_bytes = max(int(limit * aim_ratio), 1)
    good_enough = int(limit * good_enough_ratio)
    current = max(minimum, min(initial, maximum))
    tried: list[int] = []
    # Upper bound known to be too large; keeps the search from oscillating.
    ceiling = maximum
    # Per-call budget - see the note in ``search_discrete_ladder``.
    spent = 0

    while spent < max_evaluations:
        if any(abs(current - t) <= max(1, int(t * 0.01)) for t in tried):
            break  # converged: the next guess repeats one we already measured
        tried.append(current)
        spent += 1

        size = encode(current)
        if size is None:
            result.evaluations += 1
            ceiling = current
            nxt = max(minimum, current // 2)
            if nxt >= current:
                break
            current = nxt
            continue

        fits = result.record(current, size, limit)
        if fits and size >= good_enough:
            return result

        payload = max(size - fixed_overhead_bytes, 1)
        desired_payload = aim_bytes - fixed_overhead_bytes
        if desired_payload <= 0:
            # The fixed part alone already blows the budget.
            break

        scaled = int(current * (desired_payload / payload))
        if not fits:
            ceiling = min(ceiling, current)
            scaled = min(scaled, int(current * 0.9))
        nxt = max(minimum, min(scaled, ceiling, maximum))

        if nxt == current or (not fits and nxt >= current):
            break
        if fits and nxt <= current:
            break  # cannot improve quality any further within the budget
        current = nxt

    return result
