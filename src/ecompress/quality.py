"""Choosing resolution and frame rate together for a video bitrate budget.

When the budget is too small for the source, something has to give. There are
two levers - how many pixels per frame, and how many frames per second - and
picking only one of them wastes quality. A 4K 60 fps screen recording squeezed
into a small budget does not need to fall all the way to 360p; halving the
frame rate buys back most of the resolution.

The model has two halves.

**What a combination costs.** Published streaming ladders (Apple's HLS
authoring spec, and the public YouTube/Netflix recommendations) all agree on
two things: required bitrate is close to proportional to pixel count, and
doubling frame rate costs roughly 1.5x - not 2x - because consecutive frames
are more similar the faster you sample. That gives

    required_bits_per_second = BITS_PER_PIXEL * width * height * (fps / 30) ** FPS_EXPONENT

with ``FPS_EXPONENT = log2(1.5) ~= 0.585``. Calibrating ``BITS_PER_PIXEL``
against those ladders puts "visually good" at about 3.0 and "acceptable, some
artefacts" at about 1.5, which is the floor used here.

**Which affordable combination is best.** Among the combinations the budget can
pay for, quality is scored relative to the source:

    score = (pixels / source_pixels) ** 1.0 * (fps / source_fps) ** 0.3

The exponents encode a deliberate preference: losing resolution hurts more than
losing frame rate. That matches how most real footage is watched - text,
faces and detail survive a 60 -> 30 fps drop far better than they survive being
halved in each dimension - so the search gives up frames before it gives up
pixels.

Both constants are heuristics, not measurements of a particular clip. They are
tuned to established encoding-ladder practice, and a pathological source
(very high motion, or a slideshow) will not match them exactly.
"""

from __future__ import annotations

from typing import NamedTuple

__all__ = [
    "ACCEPTABLE_BITS_PER_PIXEL",
    "VideoPlan",
    "build_plan_ladder",
    "required_bitrate",
]

#: Bits per pixel per second at 30 fps. ~3.0 is visually good, ~1.5 acceptable.
ACCEPTABLE_BITS_PER_PIXEL = 1.5

#: Doubling frame rate costs ~1.5x the bitrate, not 2x: log2(1.5).
FPS_EXPONENT = 0.585

#: Relative weights when ranking affordable combinations.
RESOLUTION_WEIGHT = 1.0
FRAME_RATE_WEIGHT = 0.3

#: Standard heights to step down through, descending. Gaps here cost quality:
#: if no tier is affordable between two steps, the search falls further than it
#: needs to, so the low end is kept as dense as the high end.
HEIGHT_LADDER: tuple[int, ...] = (
    2160, 1440, 1080, 900, 720, 576, 540, 480, 432, 360, 288, 270, 240, 216, 180, 144,
)  # fmt: skip

#: Below this, motion stops reading as motion.
MIN_FRAME_RATE = 24.0

#: Two rates closer than this are treated as the same option.
_FRAME_RATE_TOLERANCE = 2.0


class VideoPlan(NamedTuple):
    """One (resolution, frame rate) combination to encode at."""

    width: int
    height: int
    fps: float
    """``0`` means "leave the source frame rate alone"."""

    @property
    def changes_frame_rate(self) -> bool:
        return self.fps > 0

    def describe(self) -> str:
        label = f"{self.width}x{self.height}"
        if self.changes_frame_rate:
            label += f" @ {self.fps:g} fps"
        return label


def required_bitrate(width: int, height: int, fps: float) -> float:
    """Bits per second needed for this combination to look acceptable."""
    if width <= 0 or height <= 0 or fps <= 0:
        return 0.0
    return float(ACCEPTABLE_BITS_PER_PIXEL * width * height * (fps / 30.0) ** FPS_EXPONENT)


def _even(value: int) -> int:
    """H.264 and VP9 need even dimensions for 4:2:0 chroma."""
    return max(2, value - (value % 2))


def frame_rate_options(source_fps: float) -> list[float]:
    """Frame rates worth considering, highest first.

    Halving is preferred over an arbitrary target because dropping every other
    frame is exact - no judder from resampling to an unrelated rate.
    """
    if source_fps <= 0:
        return [0.0]

    candidates = [source_fps]
    for option in (source_fps / 2.0, 30.0, 24.0):
        if option < MIN_FRAME_RATE - 0.01 or option >= source_fps:
            continue
        if any(abs(option - kept) < _FRAME_RATE_TOLERANCE for kept in candidates):
            continue
        candidates.append(option)
    return sorted(candidates, reverse=True)


def resolution_options(width: int, height: int) -> list[tuple[int, int]]:
    """Source resolution first, then progressively smaller standard heights."""
    if width <= 0 or height <= 0:
        return []
    aspect = width / height
    options = [(_even(width), _even(height))]
    for target_height in HEIGHT_LADDER:
        if target_height >= height:
            continue
        options.append((_even(round(target_height * aspect)), _even(target_height)))
    return options


def build_plan_ladder(
    *,
    width: int,
    height: int,
    source_fps: float,
    video_bitrate: int,
) -> list[VideoPlan]:
    """Rank (resolution, frame rate) combinations for a bitrate, best first.

    Combinations the budget can actually pay for come first, ordered by the
    quality score. Unaffordable ones follow in the same order, so a search that
    exhausts the affordable options still has somewhere to go rather than
    failing outright.
    """
    resolutions = resolution_options(width, height)
    if not resolutions:
        return [VideoPlan(0, 0, 0.0)]

    rates = frame_rate_options(source_fps)
    source_pixels = float(width * height) or 1.0
    reference_fps = source_fps if source_fps > 0 else 30.0

    affordable: list[tuple[float, VideoPlan]] = []
    beyond: list[tuple[float, VideoPlan]] = []

    for target_width, target_height in resolutions:
        for fps in rates:
            effective_fps = fps if fps > 0 else reference_fps
            score = (target_width * target_height / source_pixels) ** RESOLUTION_WEIGHT * (
                effective_fps / reference_fps
            ) ** FRAME_RATE_WEIGHT
            # Leave the source rate untouched when it is already the choice.
            keep_rate = abs(effective_fps - source_fps) < _FRAME_RATE_TOLERANCE
            plan = VideoPlan(target_width, target_height, 0.0 if keep_rate else effective_fps)

            needed = required_bitrate(target_width, target_height, effective_fps)
            (affordable if video_bitrate >= needed else beyond).append((score, plan))

    affordable.sort(key=lambda item: item[0], reverse=True)
    beyond.sort(key=lambda item: item[0], reverse=True)

    ordered = [plan for _, plan in affordable] + [plan for _, plan in beyond]
    return _deduplicate(ordered)


def _deduplicate(plans: list[VideoPlan]) -> list[VideoPlan]:
    seen: set[tuple[int, int, int]] = set()
    unique: list[VideoPlan] = []
    for plan in plans:
        key = (plan.width, plan.height, round(plan.fps * 100))
        if key in seen:
            continue
        seen.add(key)
        unique.append(plan)
    return unique
