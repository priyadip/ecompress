"""The search strategies: maximise the setting subject to a hard ceiling."""

from __future__ import annotations

import pytest

from compress.search import SearchOutcome, search_discrete_ladder, search_proportional


class _Recorder:
    """Fake encoder whose size is a known function of the setting."""

    def __init__(self, sizes: dict[int, int]) -> None:
        self.sizes = sizes
        self.calls: list[int] = []

    def __call__(self, setting: int) -> int | None:
        self.calls.append(setting)
        return self.sizes.get(setting)


# -- discrete ladder -------------------------------------------------------


def test_ladder_picks_the_largest_setting_that_fits() -> None:
    ladder = [10, 20, 30, 40, 50]
    sizes = {10: 100, 20: 200, 30: 300, 40: 400, 50: 500}
    encoder = _Recorder(sizes)

    outcome = search_discrete_ladder(ladder, encoder, limit=350, good_enough_ratio=1.0)

    assert outcome.best is not None
    assert outcome.best.setting == 30
    assert outcome.best.size_bytes == 300


def test_ladder_is_strictly_below_the_limit() -> None:
    """A result exactly equal to the limit must be rejected."""
    ladder = [1, 2, 3]
    encoder = _Recorder({1: 50, 2: 100, 3: 150})

    outcome = search_discrete_ladder(ladder, encoder, limit=100, good_enough_ratio=1.0)

    assert outcome.best is not None
    assert outcome.best.setting == 1
    assert outcome.best.size_bytes == 50


def test_ladder_reports_nothing_when_even_the_smallest_is_too_big() -> None:
    encoder = _Recorder({1: 900, 2: 950, 3: 999})
    outcome = search_discrete_ladder([1, 2, 3], encoder, limit=100)

    assert outcome.best is None
    assert outcome.smallest is not None
    assert outcome.smallest.size_bytes == 900


def test_ladder_uses_binary_search_not_a_linear_scan() -> None:
    ladder = list(range(1, 33))
    encoder = _Recorder({n: n * 10 for n in ladder})

    search_discrete_ladder(ladder, encoder, limit=155, good_enough_ratio=1.0)

    assert len(encoder.calls) <= 7, f"expected ~log2(32) probes, got {encoder.calls}"


def test_ladder_stops_early_once_close_to_the_ceiling() -> None:
    ladder = list(range(1, 21))
    encoder = _Recorder({n: n * 50 for n in ladder})

    outcome = search_discrete_ladder(ladder, encoder, limit=1000, good_enough_ratio=0.90)

    assert outcome.best is not None
    assert outcome.best.size_bytes >= 900
    assert len(encoder.calls) < 20


def test_ladder_respects_the_evaluation_budget() -> None:
    ladder = list(range(1, 100))
    encoder = _Recorder({n: n for n in ladder})
    search_discrete_ladder(ladder, encoder, limit=1, max_evaluations=3)
    assert len(encoder.calls) <= 3


def test_ladder_treats_a_failed_encode_as_too_big() -> None:
    ladder = [1, 2, 3, 4]
    # Setting 4 cannot be encoded at all.
    encoder = _Recorder({1: 10, 2: 20, 3: 30})

    outcome = search_discrete_ladder(ladder, encoder, limit=1000, good_enough_ratio=1.0)

    assert outcome.best is not None
    assert outcome.best.setting == 3


def test_ladder_handles_an_empty_ladder() -> None:
    outcome: SearchOutcome[int] = search_discrete_ladder([], _Recorder({}), limit=100)
    assert outcome.best is None
    assert outcome.evaluations == 0


def test_ladder_can_accumulate_into_a_shared_outcome() -> None:
    shared: SearchOutcome[int] = SearchOutcome()
    search_discrete_ladder([1, 2], _Recorder({1: 10, 2: 20}), limit=100, outcome=shared)
    search_discrete_ladder([3, 4], _Recorder({3: 30, 4: 40}), limit=100, outcome=shared)

    assert shared.best is not None
    assert shared.best.size_bytes == 40


# -- proportional search ---------------------------------------------------


def _linear(bits_per_second: int, *, overhead: int = 0, seconds: float = 10.0) -> int:
    """Size of a perfectly linear encoder."""
    return int(bits_per_second * seconds / 8) + overhead


def test_proportional_converges_within_a_few_encodes() -> None:
    calls: list[int] = []

    def encode(bitrate: int) -> int:
        calls.append(bitrate)
        return _linear(bitrate)

    outcome = search_proportional(
        encode, limit=1_000_000, initial=2_000_000, minimum=24_000, maximum=8_000_000
    )

    assert outcome.best is not None
    assert outcome.best.size_bytes < 1_000_000
    assert len(calls) <= 4, calls


def test_proportional_result_is_close_to_the_ceiling() -> None:
    """Quality is maximised, so the result should sit just under the limit."""

    def encode(bitrate: int) -> int:
        return _linear(bitrate)

    outcome = search_proportional(
        encode, limit=1_000_000, initial=4_000_000, minimum=24_000, maximum=8_000_000
    )

    assert outcome.best is not None
    assert outcome.best.size_bytes >= 850_000


def test_proportional_accounts_for_fixed_overhead() -> None:
    """A large fixed part (audio track) must not confuse the correction."""
    overhead = 400_000

    def encode(bitrate: int) -> int:
        return _linear(bitrate, overhead=overhead)

    outcome = search_proportional(
        encode,
        limit=1_000_000,
        initial=3_000_000,
        minimum=24_000,
        maximum=8_000_000,
        fixed_overhead_bytes=overhead,
    )

    assert outcome.best is not None
    assert outcome.best.size_bytes < 1_000_000
    assert outcome.best.size_bytes >= 850_000


def test_proportional_gives_up_when_the_floor_is_still_too_big() -> None:
    def encode(bitrate: int) -> int:
        return 5_000_000  # nothing helps

    outcome = search_proportional(
        encode, limit=1_000_000, initial=2_000_000, minimum=24_000, maximum=8_000_000
    )

    assert outcome.best is None
    assert outcome.smallest is not None
    assert outcome.evaluations <= 6


def test_proportional_never_exceeds_the_maximum() -> None:
    seen: list[int] = []

    def encode(bitrate: int) -> int:
        seen.append(bitrate)
        return _linear(bitrate)

    search_proportional(encode, limit=100_000_000, initial=500_000, minimum=24_000, maximum=600_000)
    assert max(seen) <= 600_000


def test_proportional_never_goes_below_the_minimum() -> None:
    seen: list[int] = []

    def encode(bitrate: int) -> int:
        seen.append(bitrate)
        return 9_000_000

    search_proportional(encode, limit=1_000, initial=500_000, minimum=32_000, maximum=800_000)
    assert min(seen) >= 32_000


def test_proportional_handles_a_failing_encoder() -> None:
    def encode(bitrate: int) -> int | None:
        return None

    outcome = search_proportional(
        encode, limit=1_000_000, initial=2_000_000, minimum=24_000, maximum=8_000_000
    )
    assert outcome.best is None
    assert outcome.evaluations <= 6


def test_proportional_respects_the_evaluation_budget() -> None:
    calls: list[int] = []

    def encode(bitrate: int) -> int:
        calls.append(bitrate)
        return 9_000_000

    search_proportional(
        encode,
        limit=1_000,
        initial=900_000,
        minimum=1_000,
        maximum=1_000_000,
        max_evaluations=2,
    )
    assert len(calls) <= 2


def test_outcome_record_tracks_best_and_smallest() -> None:
    outcome: SearchOutcome[int] = SearchOutcome()
    assert outcome.record(1, 500, limit=1_000) is True
    assert outcome.record(2, 900, limit=1_000) is True
    assert outcome.record(3, 1_500, limit=1_000) is False

    assert outcome.best is not None and outcome.best.size_bytes == 900
    assert outcome.smallest is not None and outcome.smallest.size_bytes == 500
    assert outcome.evaluations == 3


@pytest.mark.parametrize("limit", [1, 999, 1_000_000, 50_000_000])
def test_record_rejects_sizes_equal_to_the_limit(limit: int) -> None:
    outcome: SearchOutcome[int] = SearchOutcome()
    assert outcome.record(1, limit, limit=limit) is False
    assert outcome.best is None
    assert outcome.record(2, limit - 1, limit=limit) is True
