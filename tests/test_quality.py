"""Choosing resolution and frame rate together.

The rule these tests pin down: when the budget is thin, give up frames before
giving up pixels - but never give up either one gratuitously.
"""

from __future__ import annotations

import pytest

from ecompress.quality import (
    ACCEPTABLE_BITS_PER_PIXEL,
    FRAME_RATE_WEIGHT,
    MIN_BITS_PER_PIXEL,
    MIN_FRAME_RATE,
    RESOLUTION_WEIGHT,
    VideoPlan,
    build_plan_ladder,
    calibrate_bits_per_pixel,
    frame_rate_options,
    index_for_pixel_rate,
    pixel_rate,
    recommended_index,
    required_bitrate,
    resolution_options,
)

# -- the cost model --------------------------------------------------------


def test_required_bitrate_scales_with_pixel_count() -> None:
    """Twice the pixels, twice the bits."""
    small = required_bitrate(640, 360, 30)
    large = required_bitrate(1280, 720, 30)
    assert large == pytest.approx(small * 4, rel=0.01)


def test_doubling_frame_rate_costs_about_half_again() -> None:
    """Not 2x: consecutive frames are more alike the faster you sample."""
    at_30 = required_bitrate(1280, 720, 30)
    at_60 = required_bitrate(1280, 720, 60)
    assert at_60 == pytest.approx(at_30 * 1.5, rel=0.02)


def test_required_bitrate_is_in_a_sane_ballpark() -> None:
    """Sanity-check against published streaming ladders (~1-3 Mbps for 720p30)."""
    assert 1_000_000 < required_bitrate(1280, 720, 30) < 3_000_000
    assert 2_000_000 < required_bitrate(1920, 1080, 30) < 6_000_000


@pytest.mark.parametrize(("w", "h", "fps"), [(0, 100, 30), (100, 0, 30), (100, 100, 0)])
def test_degenerate_inputs_cost_nothing(w: int, h: int, fps: float) -> None:
    assert required_bitrate(w, h, fps) == 0.0


# -- frame-rate options ----------------------------------------------------


def test_frame_rate_options_prefer_exact_halving() -> None:
    """Dropping every other frame is exact; resampling to 30 would judder."""
    options = frame_rate_options(62)
    assert options[0] == 62
    assert 31 in options


def test_frame_rate_options_never_exceed_the_source() -> None:
    for source in (24, 25, 30, 50, 60, 62, 120):
        assert max(frame_rate_options(source)) <= source


def test_frame_rate_options_never_go_below_the_floor() -> None:
    for source in (24, 30, 60, 120):
        assert min(frame_rate_options(source)) >= MIN_FRAME_RATE - 0.01


def test_a_low_frame_rate_source_is_left_alone() -> None:
    """24 fps has nothing to give; halving it would break motion."""
    assert frame_rate_options(24) == [24]


def test_near_duplicate_rates_are_collapsed() -> None:
    """25 and 24 are close enough that offering both wastes an encode."""
    assert frame_rate_options(25) == [25]


def test_unknown_frame_rate_is_handled() -> None:
    assert frame_rate_options(0) == [0.0]


# -- resolution options ----------------------------------------------------


def test_resolution_options_start_at_the_source() -> None:
    options = resolution_options(1920, 1080)
    assert options[0] == (1920, 1080)


def test_resolution_options_descend_and_keep_aspect() -> None:
    options = resolution_options(1920, 1080)
    heights = [h for _, h in options]
    assert heights == sorted(heights, reverse=True)
    for width, height in options:
        assert abs(width / height - 16 / 9) < 0.05


def test_resolution_options_are_even() -> None:
    """4:2:0 chroma requires even dimensions."""
    for width, height in resolution_options(1919, 1079):
        assert width % 2 == 0
        assert height % 2 == 0


# -- the joint ladder ------------------------------------------------------


def test_the_ladder_always_starts_at_the_source() -> None:
    """Index 0 is the untouched source; a bitrate only chooses where to enter."""
    ladder = build_plan_ladder(width=1280, height=720, source_fps=60)
    assert (ladder[0].width, ladder[0].height) == (1280, 720)
    assert not ladder[0].changes_frame_rate


def test_a_generous_budget_starts_at_the_source() -> None:
    ladder = build_plan_ladder(width=1280, height=720, source_fps=60)
    start = ladder[recommended_index(ladder, video_bitrate=50_000_000, source_fps=60)]
    assert (start.width, start.height) == (1280, 720)
    assert not start.changes_frame_rate, "no reason to touch frame rate here"


def test_a_thin_budget_drops_frame_rate_before_resolution() -> None:
    """The core of the feature."""
    budget = 900_000
    ladder = build_plan_ladder(width=1920, height=1080, source_fps=60)
    start = ladder[recommended_index(ladder, video_bitrate=budget, source_fps=60)]

    slower = build_plan_ladder(width=1920, height=1080, source_fps=30)
    slower_start = slower[recommended_index(slower, video_bitrate=budget, source_fps=30)]

    # At 60fps the same budget must buy no more pixels than it would at 30fps.
    assert start.width * start.height <= slower_start.width * slower_start.height
    assert start.changes_frame_rate, "frame rate should have been traded first"


def test_the_reported_4k_case_keeps_far_more_detail() -> None:
    """3840x2160 @ 62fps into a 50 MB budget: the case that motivated this."""
    ladder = build_plan_ladder(width=3840, height=2160, source_fps=62)
    start = ladder[recommended_index(ladder, video_bitrate=965_000, source_fps=62)]

    # The old resolution-only rule landed on 640x360 at the full 62 fps.
    assert start.width * start.height > 640 * 360, "should beat the old 360p result"
    assert start.changes_frame_rate
    assert start.fps < 62


def _effective_fps(plan: VideoPlan, source_fps: float) -> float:
    return plan.fps if plan.fps > 0 else source_fps


def _score(plan: VideoPlan, width: int, height: int, source_fps: float) -> float:
    """Recompute the ranking score independently of the implementation."""
    pixels = plan.width * plan.height / (width * height)
    rate = _effective_fps(plan, source_fps) / source_fps
    return float(pixels**RESOLUTION_WEIGHT * rate**FRAME_RATE_WEIGHT)


def test_recommended_index_picks_the_first_affordable_plan() -> None:
    budget = 900_000
    fps = 60.0
    ladder = build_plan_ladder(width=1920, height=1080, source_fps=fps)

    start = recommended_index(ladder, video_bitrate=budget, source_fps=fps)

    chosen = ladder[start]
    assert required_bitrate(chosen.width, chosen.height, _effective_fps(chosen, fps)) <= budget
    # Everything ranked above it must genuinely be out of reach.
    for plan in ladder[:start]:
        assert required_bitrate(plan.width, plan.height, _effective_fps(plan, fps)) > budget


def test_the_whole_ladder_is_ordered_by_descending_quality() -> None:
    width, height, fps = 1920, 1080, 60.0
    ladder = build_plan_ladder(width=width, height=height, source_fps=fps)

    scores = [_score(p, width, height, fps) for p in ladder]
    assert scores == sorted(scores, reverse=True), "quality ranking is out of order"


def test_index_for_pixel_rate_never_exceeds_what_was_asked_for() -> None:
    fps = 60.0
    ladder = build_plan_ladder(width=1920, height=1080, source_fps=fps)

    for wanted in (1e5, 1e6, 1e7, 5e7, 1e9):
        index = index_for_pixel_rate(ladder, required=wanted, source_fps=fps)
        chosen = pixel_rate(ladder[index], fps)
        # Either it fits the budget, or we are already at the smallest option.
        assert chosen <= wanted or index == len(ladder) - 1
        if index > 0:
            assert pixel_rate(ladder[index - 1], fps) > wanted


# -- calibrating from the source -------------------------------------------


def test_calibration_needs_real_numbers() -> None:
    for kwargs in (
        {"video_bitrate": None, "width": 1920, "height": 1080, "fps": 30},
        {"video_bitrate": 0, "width": 1920, "height": 1080, "fps": 30},
        {"video_bitrate": 5_000_000, "width": 0, "height": 1080, "fps": 30},
        {"video_bitrate": 5_000_000, "width": 1920, "height": 1080, "fps": 0},
    ):
        assert calibrate_bits_per_pixel(**kwargs) is None


def test_easy_content_measures_below_the_generic_constant() -> None:
    """A near-static 4K recording is far cheaper than typical footage."""
    measured = calibrate_bits_per_pixel(video_bitrate=3_330_000, width=3840, height=2160, fps=62.5)
    assert measured is not None
    assert measured < ACCEPTABLE_BITS_PER_PIXEL
    assert measured == pytest.approx(0.26, abs=0.05)


def test_calibration_can_only_lower_the_requirement() -> None:
    """A lavish source shows the content *can* take bits, not that it needs them.

    Without this cap, a visually-lossless source would be judged unable to hold
    its own resolution at 90% of its own bitrate - plainly wrong, and it made
    high-quality clips lose frame rate for no reason.
    """
    lavish = calibrate_bits_per_pixel(video_bitrate=200_000_000, width=1920, height=1080, fps=30)
    assert lavish == ACCEPTABLE_BITS_PER_PIXEL


def test_a_crushed_source_hits_the_floor() -> None:
    crushed = calibrate_bits_per_pixel(video_bitrate=1, width=3840, height=2160, fps=60)
    assert crushed == MIN_BITS_PER_PIXEL


def test_calibration_moves_the_starting_plan_up_for_easy_content() -> None:
    """The reported case: the generic constant started far too low."""
    width, height, fps = 3840, 2160, 62.5
    budget = 965_000
    ladder = build_plan_ladder(width=width, height=height, source_fps=fps)

    generic = ladder[recommended_index(ladder, video_bitrate=budget, source_fps=fps)]
    measured = calibrate_bits_per_pixel(
        video_bitrate=3_330_000, width=width, height=height, fps=fps
    )
    assert measured is not None
    calibrated = ladder[
        recommended_index(ladder, video_bitrate=budget, source_fps=fps, bits_per_pixel=measured)
    ]

    assert calibrated.width * calibrated.height > generic.width * generic.height * 3, (
        f"calibration should have started far higher: {generic} -> {calibrated}"
    )


def test_calibration_does_not_disturb_ordinary_footage() -> None:
    """Content matching the generic assumption should land in the same place."""
    width, height, fps = 1920, 1080, 30.0
    # A bitrate that measures out at roughly the default constant.
    typical = int(ACCEPTABLE_BITS_PER_PIXEL * width * height)
    ladder = build_plan_ladder(width=width, height=height, source_fps=fps)

    measured = calibrate_bits_per_pixel(video_bitrate=typical, width=width, height=height, fps=fps)
    assert measured == pytest.approx(ACCEPTABLE_BITS_PER_PIXEL, rel=0.01)

    budget = 2_000_000
    generic = recommended_index(ladder, video_bitrate=budget, source_fps=fps)
    calibrated = recommended_index(
        ladder, video_bitrate=budget, source_fps=fps, bits_per_pixel=measured
    )
    assert generic == calibrated


def test_index_for_pixel_rate_returns_the_source_when_budget_is_huge() -> None:
    fps = 60.0
    ladder = build_plan_ladder(width=1920, height=1080, source_fps=fps)
    assert index_for_pixel_rate(ladder, required=1e12, source_fps=fps) == 0


def test_ladder_has_no_duplicates() -> None:
    ladder = build_plan_ladder(width=1920, height=1080, source_fps=60)
    assert len(ladder) == len(set(ladder))


def test_ladder_never_upscales() -> None:
    ladder = build_plan_ladder(width=640, height=360, source_fps=30)
    for plan in ladder:
        assert plan.width <= 640
        assert plan.height <= 360
        assert plan.fps == 0 or plan.fps <= 30


def test_an_impossible_budget_still_offers_something() -> None:
    """Nothing is affordable, but the search must have somewhere to go."""
    ladder = build_plan_ladder(width=1920, height=1080, source_fps=60)
    assert ladder, "must never return an empty ladder"
    smallest = ladder[-1]
    assert smallest.width > 0


def test_unknown_dimensions_are_survivable() -> None:
    ladder = build_plan_ladder(width=0, height=0, source_fps=30)
    assert ladder == [VideoPlan(0, 0, 0.0)]


def test_plan_description_mentions_frame_rate_only_when_changed() -> None:
    assert VideoPlan(1280, 720, 0.0).describe() == "1280x720"
    assert VideoPlan(1280, 720, 30).describe() == "1280x720 @ 30 fps"
