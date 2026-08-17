"""Choosing resolution and frame rate together.

The rule these tests pin down: when the budget is thin, give up frames before
giving up pixels - but never give up either one gratuitously.
"""

from __future__ import annotations

import pytest

from ecompress.quality import (
    FRAME_RATE_WEIGHT,
    MIN_FRAME_RATE,
    RESOLUTION_WEIGHT,
    VideoPlan,
    build_plan_ladder,
    frame_rate_options,
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


def test_a_generous_budget_keeps_the_source_untouched() -> None:
    ladder = build_plan_ladder(width=1280, height=720, source_fps=60, video_bitrate=50_000_000)
    best = ladder[0]
    assert (best.width, best.height) == (1280, 720)
    assert not best.changes_frame_rate, "no reason to touch frame rate here"


def test_a_thin_budget_drops_frame_rate_before_resolution() -> None:
    """The core of the feature."""
    ladder = build_plan_ladder(width=1920, height=1080, source_fps=60, video_bitrate=900_000)
    best = ladder[0]

    reduced_fps_only = build_plan_ladder(
        width=1920, height=1080, source_fps=30, video_bitrate=900_000
    )[0]

    # At 60fps the same budget must buy no more pixels than it would at 30fps.
    assert best.width * best.height <= reduced_fps_only.width * reduced_fps_only.height
    assert best.changes_frame_rate, "frame rate should have been traded first"


def test_the_reported_4k_case_keeps_far_more_detail() -> None:
    """3840x2160 @ 62fps into a 50 MB budget: the case that motivated this."""
    ladder = build_plan_ladder(width=3840, height=2160, source_fps=62, video_bitrate=965_000)
    best = ladder[0]

    # The old resolution-only rule landed on 640x360 at the full 62 fps.
    assert best.width * best.height > 640 * 360, "should beat the old 360p result"
    assert best.changes_frame_rate
    assert best.fps < 62


def _effective_fps(plan: VideoPlan, source_fps: float) -> float:
    return plan.fps if plan.fps > 0 else source_fps


def _score(plan: VideoPlan, width: int, height: int, source_fps: float) -> float:
    """Recompute the ranking score independently of the implementation."""
    pixels = plan.width * plan.height / (width * height)
    rate = _effective_fps(plan, source_fps) / source_fps
    return float(pixels**RESOLUTION_WEIGHT * rate**FRAME_RATE_WEIGHT)


def test_affordable_plans_come_before_unaffordable_ones() -> None:
    budget = 900_000
    ladder = build_plan_ladder(width=1920, height=1080, source_fps=60, video_bitrate=budget)

    affordable_flags = [
        required_bitrate(p.width, p.height, _effective_fps(p, 60)) <= budget for p in ladder
    ]
    assert any(affordable_flags), "at least one combination must be affordable"
    # Once the list turns unaffordable it must never turn back.
    first_unaffordable = affordable_flags.index(False) if False in affordable_flags else len(ladder)
    assert all(affordable_flags[:first_unaffordable])
    assert not any(affordable_flags[first_unaffordable:])


def test_affordable_plans_are_ordered_by_descending_quality() -> None:
    budget = 900_000
    width, height, fps = 1920, 1080, 60.0
    ladder = build_plan_ladder(width=width, height=height, source_fps=fps, video_bitrate=budget)
    affordable = [
        p for p in ladder if required_bitrate(p.width, p.height, _effective_fps(p, fps)) <= budget
    ]

    scores = [_score(p, width, height, fps) for p in affordable]
    assert scores == sorted(scores, reverse=True), "quality ranking is out of order"


def test_ladder_has_no_duplicates() -> None:
    ladder = build_plan_ladder(width=1920, height=1080, source_fps=60, video_bitrate=1_000_000)
    assert len(ladder) == len(set(ladder))


def test_ladder_never_upscales() -> None:
    ladder = build_plan_ladder(width=640, height=360, source_fps=30, video_bitrate=100_000_000)
    for plan in ladder:
        assert plan.width <= 640
        assert plan.height <= 360
        assert plan.fps == 0 or plan.fps <= 30


def test_an_impossible_budget_still_offers_something() -> None:
    """Nothing is affordable, but the search must have somewhere to go."""
    ladder = build_plan_ladder(width=1920, height=1080, source_fps=60, video_bitrate=1)
    assert ladder, "must never return an empty ladder"
    smallest = ladder[-1]
    assert smallest.width > 0


def test_unknown_dimensions_are_survivable() -> None:
    ladder = build_plan_ladder(width=0, height=0, source_fps=30, video_bitrate=500_000)
    assert ladder == [VideoPlan(0, 0, 0.0)]


def test_plan_description_mentions_frame_rate_only_when_changed() -> None:
    assert VideoPlan(1280, 720, 0.0).describe() == "1280x720"
    assert VideoPlan(1280, 720, 30).describe() == "1280x720 @ 30 fps"
