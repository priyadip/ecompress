"""PDF compression: shrink without destroying the document."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pikepdf
import pytest

from ecompress import compress
from ecompress.backends.pdf import PdfBackend, find_ghostscript
from ecompress.errors import InputFileError, TargetNotAchievableError
from ecompress.result import MediaType

from .conftest import requires_reportlab

Copier = Callable[..., Path]

pytestmark = requires_reportlab


def open_pages(path: Path) -> int:
    """Independently re-open the PDF, exactly as a reader would."""
    with pikepdf.open(path) as pdf:
        count = len(pdf.pages)
        for page in pdf.pages:
            _ = page.obj.get("/Resources")
    return count


def test_image_heavy_pdf_lands_below_target(copy_media: Copier, source_pdf_images: Path) -> None:
    source = copy_media(source_pdf_images)
    original_pages = open_pages(source)

    result = compress(source, 0.2)

    assert result.output_size_bytes < 200_000
    assert result.media_type is MediaType.PDF
    assert result.output_path.suffix == ".pdf"
    assert open_pages(result.output_path) == original_pages


def test_page_count_is_always_preserved(copy_media: Copier, source_pdf_images: Path) -> None:
    source = copy_media(source_pdf_images)
    before = open_pages(source)
    result = compress(source, 0.05)
    assert open_pages(result.output_path) == before


def test_text_is_preserved_through_compression(copy_media: Copier, source_pdf_images: Path) -> None:
    """Image re-encoding must not touch the text layer."""
    result = compress(copy_media(source_pdf_images), 0.1)

    with pikepdf.open(result.output_path) as pdf:
        fonts_present = any("/Font" in (page.obj.get("/Resources") or {}) for page in pdf.pages)
    assert fonts_present, "the text layer disappeared"


def test_quality_is_maximised(copy_media: Copier, source_pdf_images: Path) -> None:
    result = compress(copy_media(source_pdf_images), 0.2)
    assert result.output_size_bytes > 100_000, "the budget was badly undershot"


def test_lossless_pass_is_tried_first(copy_media: Copier, source_pdf_images: Path) -> None:
    result = compress(copy_media(source_pdf_images), 0.5)
    first = result.attempts[0]
    assert first.parameters.get("image_quality") is None, "the first attempt should be lossless"


def test_text_only_pdf_that_cannot_shrink_reports_honestly(
    copy_media: Copier, source_pdf_text: Path
) -> None:
    source = copy_media(source_pdf_text)
    with pytest.raises(TargetNotAchievableError) as info:
        compress(source, 0.0005)  # 500 bytes

    error = info.value
    message = str(error)
    assert "could not be achieved" in message
    assert "no raster images" in message or "text" in message.lower()
    assert error.smallest_valid_bytes is not None
    assert error.smallest_valid_bytes > 500


def test_no_broken_pdf_is_left_behind_on_failure(
    copy_media: Copier, tmp_path: Path, source_pdf_text: Path
) -> None:
    source = copy_media(source_pdf_text)
    with pytest.raises(TargetNotAchievableError):
        compress(source, 0.0005)

    leftovers = [p for p in tmp_path.iterdir() if p != source]
    assert leftovers == [], f"unexpected files left behind: {leftovers}"


def test_text_only_pdf_still_shrinks_a_little(copy_media: Copier, source_pdf_text: Path) -> None:
    """Lossless structural optimisation alone should still gain something."""
    source = copy_media(source_pdf_text)
    original = source.stat().st_size

    result = compress(source, (original - 100) / 1_000_000)

    assert result.output_size_bytes < original
    assert open_pages(result.output_path) == 3


def test_original_is_untouched(copy_media: Copier, source_pdf_images: Path) -> None:
    source = copy_media(source_pdf_images)
    before = source.read_bytes()
    compress(source, 0.2)
    assert source.read_bytes() == before


def test_encrypted_pdf_is_rejected_clearly(tmp_path: Path, source_pdf_text: Path) -> None:
    encrypted = tmp_path / "locked.pdf"
    with pikepdf.open(source_pdf_text) as pdf:
        pdf.save(encrypted, encryption=pikepdf.Encryption(owner="secret", user="secret"))

    with pytest.raises(InputFileError, match="password protected"):
        compress(encrypted, 0.001)


def test_corrupt_pdf_is_rejected_clearly(tmp_path: Path) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.7\nthis is not really a pdf\n" + b"\x00" * 4000)

    with pytest.raises(InputFileError, match="not a PDF this tool can read"):
        compress(broken, 0.001)


@pytest.mark.parametrize("name", ["my report.pdf", "отчёт.pdf", "doc (final) [v2].pdf"])
def test_awkward_pdf_filenames(tmp_path: Path, source_pdf_images: Path, name: str) -> None:
    path = tmp_path / name
    path.write_bytes(source_pdf_images.read_bytes())

    result = compress(path, 0.2)
    assert result.output_size_bytes < 200_000
    assert open_pages(result.output_path) == 3


# -- Ghostscript, when available -------------------------------------------

ghostscript_available = find_ghostscript() is not None
requires_ghostscript = pytest.mark.skipif(
    not ghostscript_available, reason="Ghostscript is not installed"
)


def test_ghostscript_discovery_honours_the_env_var(tmp_path: Path) -> None:
    fake = tmp_path / ("gs.exe" if os.name == "nt" else "gs")
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)

    previous = os.environ.get("COMPRESS_GHOSTSCRIPT")
    os.environ["COMPRESS_GHOSTSCRIPT"] = str(fake)
    try:
        assert find_ghostscript() == fake
    finally:
        if previous is None:
            del os.environ["COMPRESS_GHOSTSCRIPT"]
        else:
            os.environ["COMPRESS_GHOSTSCRIPT"] = previous


@requires_ghostscript
@pytest.mark.slow
def test_ghostscript_path_produces_a_valid_pdf(
    copy_media: Copier, tmp_path: Path, source_pdf_images: Path
) -> None:
    """Exercise the Ghostscript engine directly, including acceptance."""
    from ecompress.backends.base import Job
    from ecompress.detect import detect_media_type
    from ecompress.reporting import NullReporter

    source = copy_media(source_pdf_images)
    workdir = tmp_path / "work"
    workdir.mkdir()

    backend = PdfBackend()
    backend.expect_pages(open_pages(source))
    job = Job(
        input_path=source,
        input_size_bytes=source.stat().st_size,
        target_bytes=source.stat().st_size,
        aim_bytes=source.stat().st_size,
        detection=detect_media_type(source),
        workdir=workdir,
        reporter=NullReporter(),
    )
    backend._job = job
    gs = find_ghostscript()
    assert gs is not None
    backend._ghostscript_search(job, gs)

    outcome = backend._outcome
    assert outcome.best_path is not None, "Ghostscript produced no accepted output"
    assert outcome.best_size_bytes is not None
    assert outcome.best_size_bytes < job.target_bytes
    assert open_pages(outcome.best_path) == 3
