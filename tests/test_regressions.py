"""Regression tests.

Every test here pins a bug that was found and fixed during development. They
exist so the same mistake cannot come back quietly.
"""

from __future__ import annotations

import gc
import warnings
from collections.abc import Callable
from pathlib import Path

import pytest
from PIL import Image, ImageFile

from ecompress import compress
from ecompress.errors import UnsupportedFormatError
from ecompress.result import MediaType
from ecompress.search import SearchOutcome, search_discrete_ladder, search_proportional
from ecompress.validation import validate_output

from .conftest import requires_x264

Copier = Callable[..., Path]


def test_shared_outcome_does_not_starve_later_ladders() -> None:
    """Regression: the evaluation budget was counted on the shared outcome.

    The image and video backends run one ladder per resolution and share a
    single ``SearchOutcome`` to keep the global best. The budget check used
    ``outcome.evaluations``, which already included the previous ladder's
    encodes, so every ladder after the first was skipped entirely and small
    targets failed that were perfectly achievable after a downscale.
    """
    shared: SearchOutcome[int] = SearchOutcome()

    first_calls: list[int] = []

    def too_big(setting: int) -> int:
        first_calls.append(setting)
        return setting * 1000

    search_discrete_ladder([10, 20, 30], too_big, limit=100, max_evaluations=4, outcome=shared)
    assert first_calls, "the first ladder should run"

    second_calls: list[int] = []

    def fits(setting: int) -> int:
        second_calls.append(setting)
        return setting

    search_discrete_ladder([1, 2, 3], fits, limit=100, max_evaluations=4, outcome=shared)

    assert second_calls, "the second ladder was starved by the shared counter"
    assert shared.best is not None
    assert shared.best.size_bytes == 3


def test_shared_outcome_does_not_starve_proportional_search() -> None:
    """Regression: same budget bug in the bitrate search."""
    shared: SearchOutcome[int] = SearchOutcome()

    search_proportional(
        lambda _bitrate: 9_000_000,
        limit=1_000,
        initial=500_000,
        minimum=1_000,
        maximum=800_000,
        max_evaluations=3,
        outcome=shared,
    )

    second_calls: list[int] = []

    def encode(bitrate: int) -> int:
        second_calls.append(bitrate)
        return 500

    search_proportional(
        encode,
        limit=1_000,
        initial=500,
        minimum=100,
        maximum=800,
        max_evaluations=3,
        outcome=shared,
    )

    assert second_calls, "the second search was starved by the shared counter"


def test_truncated_images_never_validate(tmp_path: Path, source_jpg: Path) -> None:
    """Regression: a global Pillow flag made truncated files look valid.

    ``ImageFile.LOAD_TRUNCATED_IMAGES`` was set to ``True`` at import time so
    damaged *inputs* could be read. Because the flag is process-global it also
    applied to output validation, which would have let a file "hit" its target
    by simply having bytes chopped off the end.
    """
    truncated = tmp_path / "truncated.jpg"
    data = source_jpg.read_bytes()
    truncated.write_bytes(data[: len(data) // 3])

    assert not validate_output(truncated, MediaType.IMAGE).valid


def test_compressing_leaves_the_truncation_flag_untouched(
    copy_media: Copier, source_jpg: Path
) -> None:
    """Regression: the same flag must not leak out of a compression run."""
    before = ImageFile.LOAD_TRUNCATED_IMAGES
    compress(copy_media(source_jpg), 0.3)
    assert ImageFile.LOAD_TRUNCATED_IMAGES is before


def test_unsupported_file_below_target_is_still_rejected(tmp_path: Path) -> None:
    """Regression: the "already small enough" shortcut ran before detection.

    A 5-byte ``.txt`` was reported as a successful no-op for any target, which
    implied the tool supports text files.
    """
    path = tmp_path / "notes.txt"
    path.write_text("hello", encoding="utf-8")

    with pytest.raises(UnsupportedFormatError):
        compress(path, 1)


def test_skip_path_does_not_require_ffmpeg(
    copy_media: Copier, source_mp4: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: detection before the skip must not need FFmpeg."""
    from ecompress import detect as detect_module
    from ecompress.ffmpeg import FFmpegTools

    monkeypatch.setattr(
        detect_module, "find_ffmpeg_tools", lambda: FFmpegTools(ffmpeg=None, ffprobe=None)
    )

    result = compress(copy_media(source_mp4), 500)
    assert result.skipped
    assert result.media_type is MediaType.VIDEO


def test_image_downscales_when_quality_alone_cannot_reach_the_target(
    copy_media: Copier, source_jpg: Path
) -> None:
    """Regression: this failed while later resolution ladders were starved."""
    result = compress(copy_media(source_jpg), 0.012)

    assert result.output_size_bytes < 12_000
    with Image.open(result.output_path) as image:
        image.load()
        assert image.width < 1200


def test_damaged_image_does_not_leak_a_file_handle(tmp_path: Path, source_jpg: Path) -> None:
    """Regression: the failed first open was never closed.

    Reading a truncated source raises from ``Image.load()``, and the original
    ``Image.open()`` handle was dropped without being closed - leaving the file
    open until the garbage collector happened to run.
    """
    damaged = tmp_path / "damaged.jpg"
    data = source_jpg.read_bytes()
    damaged.write_bytes(data[: int(len(data) * 0.85)])

    with warnings.catch_warnings():
        warnings.simplefilter("error", ResourceWarning)
        result = compress(damaged, 0.2)
        gc.collect()

    assert result.output_size_bytes < 200_000


@requires_x264
@pytest.mark.slow
def test_easy_video_climbs_back_up_instead_of_undershooting(tmp_path: Path) -> None:
    """Regression: a saturated encoder made the search give up far too small.

    Highly compressible video (a near-static screen recording) cannot absorb
    the bitrate it is given: past a point, tripling the bitrate produces the
    same file. The search only knew how to step *down* the quality ladder, so
    it settled at a fraction of the requested size - a 151 MB 4K source asked
    to land in 40-50 MB came back as 12.5 MB of 576p. The fix spends the spare
    budget on pixels instead, climbing back towards the source.
    """
    from .conftest import run_ffmpeg

    source = tmp_path / "static.mp4"
    run_ffmpeg(
        [
            "-f", "lavfi", "-i", "color=c=0x1e2430:size=1920x1080:rate=60:duration=8",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
            "-vf", "drawbox=x=60:y=60:w=1800:h=960:color=white@0.25:t=8",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            str(source),
        ]
    )  # fmt: skip

    original = source.stat().st_size
    low = original * 0.35 / 1_000_000
    high = original * 0.50 / 1_000_000

    result = compress(source, f"{low}-{high}")

    assert result.output_size_bytes < int(high * 1_000_000), "the ceiling always holds"
    assert result.within_requested_range, (
        f"settled at {result.output_size_bytes} bytes, under the "
        f"{result.min_size_bytes} floor: {result.notes}"
    )
    # It should have climbed rather than stayed at the starving frame size.
    accepted = [a for a in result.attempts if a.accepted]
    assert accepted
    assert any("raising quality" in note for note in result.notes)


def test_dotted_filename_keeps_only_the_real_extension(tmp_path: Path, source_jpg: Path) -> None:
    """Regression guard for the reported filename shape ``name.ai.mp4``."""
    path = tmp_path / "CasualIQBusinessIntelligence.ai.jpg"
    path.write_bytes(source_jpg.read_bytes())

    result = compress(path, 0.3)
    assert result.output_path.name == "CasualIQBusinessIntelligence.ai_compressed.jpg"


def test_ladder_failure_at_the_top_still_finds_a_lower_setting() -> None:
    """Regression guard: a failed encode must not abort the whole search."""

    def encode(setting: int) -> int | None:
        if setting > 2:
            return None  # encoder refuses these settings
        return setting * 10

    outcome = search_discrete_ladder([1, 2, 3, 4, 5], encode, limit=1_000, good_enough_ratio=1.0)
    assert outcome.best is not None
    assert outcome.best.setting == 2
