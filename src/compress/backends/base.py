"""Backend contract shared by the image, video, audio and PDF backends."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from compress.detect import Detection
from compress.reporting import Reporter
from compress.result import Attempt, MediaType
from compress.validation import validate_output

__all__ = ["Backend", "BackendOutcome", "Job"]


@dataclass
class Job:
    """Everything a backend needs for one compression run."""

    input_path: Path
    input_size_bytes: int
    target_bytes: int
    """Hard ceiling. The final file must be strictly below this."""
    aim_bytes: int
    """Soft goal, a little under ``target_bytes``, used to seed estimates."""
    detection: Detection
    workdir: Path
    """Private scratch directory; the backend writes all candidates here."""
    reporter: Reporter
    timeout: float | None = None
    min_bytes: int | None = None
    """Quality floor from a user-supplied size range; ``None`` means no floor."""

    @property
    def media_type(self) -> MediaType:
        return self.detection.media_type

    def scratch(self, name: str) -> Path:
        return self.workdir / name


@dataclass
class BackendOutcome:
    """What a backend achieved."""

    best_path: Path | None = None
    """Best (largest under the ceiling) valid candidate, or ``None``."""

    best_size_bytes: int | None = None
    smallest_valid_bytes: int | None = None
    """Smallest valid output produced, used to explain unachievable targets."""

    attempts: list[Attempt] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    format_changed: bool = False
    output_extension: str | None = None
    """Extension of ``best_path``; defaults to the input's extension."""

    detail: str | None = None
    """Backend-specific explanation shown when the target is unachievable."""

    @property
    def achieved(self) -> bool:
        return self.best_path is not None and self.best_size_bytes is not None


class Backend:
    """Base class providing the measure-validate-record loop.

    Subclasses implement :meth:`run`, calling :meth:`measure` for each candidate
    file they produce. ``measure`` is what enforces the guarantee: a candidate
    only becomes "best" after it has been independently re-parsed *and* found to
    be strictly under the target.
    """

    name = "base"
    media_type: MediaType

    def __init__(self) -> None:
        self._outcome = BackendOutcome()
        self._job: Job | None = None
        self._expected_duration: float | None = None
        self._expected_pages: int | None = None

    # -- public API -------------------------------------------------------

    def compress(self, job: Job) -> BackendOutcome:
        self._job = job
        self._outcome = BackendOutcome()
        if self.prepare(job):
            job.reporter.step("")
            job.reporter.step("Optimizing...")
            self.run(job)
        return self._outcome

    def prepare(self, job: Job) -> bool:  # noqa: ARG002 - hook for subclasses
        """Inspect the source and report what it is.

        Runs before the ``Optimizing...`` banner so the user sees what was
        detected first. Return ``False`` to abort before any encoding, after
        setting ``self._outcome.detail``.
        """
        return True

    def run(self, job: Job) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- helpers for subclasses -------------------------------------------

    def expect_duration(self, duration: float | None) -> None:
        """Record the source duration so outputs can be checked against it."""
        self._expected_duration = duration

    def expect_pages(self, pages: int | None) -> None:
        self._expected_pages = pages

    def note(self, message: str) -> None:
        job = self._require_job()
        if message not in self._outcome.notes:
            self._outcome.notes.append(message)
            job.reporter.note(message)

    def measure(
        self,
        path: Path,
        *,
        parameters: dict[str, Any] | None = None,
        label: str = "",
        keep_as: str | None = None,
    ) -> int | None:
        """Validate a candidate, record it, and return its size.

        Returns the measured size in bytes, or ``None`` when the candidate is
        invalid (callers treat that the same as "too big" and compress harder).
        A candidate that is valid *and* strictly under the target replaces the
        current best when it is larger than the current best - that is the
        "maximise quality under the ceiling" rule.
        """
        job = self._require_job()
        outcome = self._outcome
        index = len(outcome.attempts) + 1
        params = dict(parameters or {})

        if not path.exists():
            attempt = Attempt(index, 0, params, accepted=False, valid=False, note="not created")
            outcome.attempts.append(attempt)
            job.reporter.attempt(attempt)
            return None

        size = path.stat().st_size
        report = validate_output(
            path,
            self.media_type,
            expected_duration=self._expected_duration,
            expected_pages=self._expected_pages,
        )
        if not report.valid:
            attempt = Attempt(index, size, params, accepted=False, valid=False, note=report.reason)
            outcome.attempts.append(attempt)
            job.reporter.attempt(attempt)
            return None

        if outcome.smallest_valid_bytes is None or size < outcome.smallest_valid_bytes:
            outcome.smallest_valid_bytes = size

        fits = size < job.target_bytes
        improves = outcome.best_size_bytes is None or size > outcome.best_size_bytes
        accepted = fits and improves
        if accepted:
            kept = job.scratch(keep_as or f"best{path.suffix}")
            if kept != path:
                if kept.exists():
                    kept.unlink()
                path.replace(kept)
                path = kept
            outcome.best_path = path
            outcome.best_size_bytes = size
            outcome.output_extension = path.suffix

        attempt = Attempt(index, size, params, accepted=accepted, valid=True, note=label)
        outcome.attempts.append(attempt)
        job.reporter.attempt(attempt)
        return size

    def _require_job(self) -> Job:
        if self._job is None:  # pragma: no cover - defensive
            raise RuntimeError("backend used outside of compress()")
        return self._job
