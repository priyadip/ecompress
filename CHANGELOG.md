# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - 2026-08-17

Renamed. **No functional changes** - every behaviour in 1.3.0 is identical.

### Changed

- **The project is now ecompress.** The PyPI name, the console command and
  the import name all changed together:

  | | Before (1.3.0) | Now (2.0.0) |
  | --- | --- | --- |
  | Install | pip install compress-cli | pip install ecompress |
  | Command | compress file.mp4 50 | ecompress file.mp4 50 |
  | Import | 
rom compress import compress | 
rom ecompress import compress |

  The function is still called compress().

  Two reasons. The import name compress shadowed an unrelated package of that
  name on PyPI, so installing both broke one of them. More seriously, the
  command compress collided with /usr/bin/compress - the classic Unix .Z
  tool - on Linux and macOS, where either could shadow the other depending on
  PATH order.

### Migration

Replace compress with ecompress in install commands, shell invocations and
imports. Calls to compress(...) in Python are unchanged.

compress-cli 1.1.0-1.3.0 remain on PyPI and keep working; they will not be
updated further. Entries below describe those releases as they shipped, under
their original names.

## [1.3.0] - 2026-08-17

### Added

- **Size ranges.** `compress "movie.mp4" 40-50` sets a floor as well as a
  ceiling, so the budget gets used instead of undershot. `[40,50]`, `40..50`,
  `40,50` and `--min 40` are all accepted, as are `(40, 50)` and `min_mb=` from
  Python. The maximum stays a hard limit; the minimum is a quality floor that
  the search climbs towards. When a file genuinely cannot reach the floor — a
  small source, or a format with nothing left to give — the result is still
  returned with an explanatory note. Files are never padded to hit a number.
- `CompressionResult.min_size_bytes`, `.min_size_mb` and
  `.within_requested_range`, plus the same fields in `--json` output.

### Changed

- **Video now trades frame rate and resolution together.** Previously only
  resolution was reduced, which starved high-frame-rate sources: a 5-minute 4K
  62 fps clip targeting 50 MB fell all the way to 640x360 while keeping all 62
  frames per second. The cost of each (resolution, frame rate) combination is
  now modelled on published streaming ladders — doubling frame rate costs about
  1.5x the bitrate, not 2x — every affordable combination is ranked with
  resolution weighted above frame rate, and the best is encoded. The same clip
  now produces 1024x576 at 31 fps: 2.6x the pixels per frame.

  Frame rate is never raised, never reduced below 24 fps, and left untouched
  when the budget comfortably covers the source. Duration is unchanged, and the
  existing validation still verifies it on every candidate.
- The resolution ladder gained intermediate tiers (432, 288, 240, 216). Gaps in
  it made the search fall further than necessary when no tier in between was
  affordable.

### Notes

- `compress.quality` is a new module holding the bitrate cost model and the
  ranking, so the heuristics are testable in isolation and documented in one
  place. Both constants are tuned to established encoding practice rather than
  measured per clip; unusual footage will not match them exactly.

## [1.2.0] - 2026-08-17

**No functional changes.** Everything under `src/compress/` is byte-identical
to 1.1.0, so upgrading from 1.1.0 changes nothing about how the tool behaves.
This release exists to exercise the tag-driven release path end to end; the
work below only affects the repository, never the installed package.

### Changed

- Pinned `ruff` to the 0.16 series in the `dev` extra. Ruff 0.16 began
  formatting Python code blocks inside Markdown, which 0.15 did not, so an
  unpinned range let CI and a developer's machine disagree about
  `ruff format --check`.
- Release workflow now fails immediately with an actionable message when a
  PyPI API token secret is missing, instead of falling through to Trusted
  Publishing and reporting an opaque OIDC error.
- Release workflow skips a version already present on the index, so re-running
  a release (or pushing a tag after a manual run) is idempotent rather than a
  hard failure.
- CI installs FFmpeg only as a package dependency rather than from the system,
  so a broken bundle cannot be masked; a separate job uninstalls it to cover
  the `PATH` fallback used on platforms without a prebuilt wheel.
- README documents the automatic FFmpeg install, `compress --check`, and where
  each component is installed.

## [1.1.0] - 2026-08-17

### Added

- **FFmpeg is now installed automatically.** `ffmpeg-binaries` (which ships
  both `ffmpeg` and `ffprobe`) is a regular dependency on every platform with a
  prebuilt wheel — Windows x64, macOS, and Linux x86_64 — so a plain
  `pip install compress-cli` is all a user needs for video and audio. The
  binaries land inside the environment's `site-packages`; nothing is written to
  system directories and nothing needs to be on `PATH`.
- `compress --check` reports what is installed: which FFmpeg was found and
  where it came from, which encoders it supports, Pillow and its formats,
  pikepdf, and Ghostscript. Exits `0` when everything needed is present and `3`
  when something is missing. `compress --check --json` gives the same
  information for scripts.
- `compress.ffmpeg.bundled_binary()` locates the bundled executables through
  installed-distribution metadata rather than by importing them, so the
  `ffmpeg-binaries` import name (`ffmpeg`) cannot be shadowed by the unrelated
  `ffmpeg-python` package. The real executable is preferred over the
  console-script shim pip installs alongside it.
- `compress-cli[ffmpeg]` extra to force the bundled FFmpeg on platforms the
  automatic marker skips (Linux ARM, musl, 32-bit Windows).

### Changed

- FFmpeg discovery order is now: `COMPRESS_FFMPEG`/`COMPRESS_FFPROBE` override,
  then the bundled copy, then `PATH`, then well-known install locations. The
  bundled copy is preferred over `PATH` so behaviour is identical on every
  machine regardless of what system FFmpeg happens to be installed.
- The "FFmpeg is not installed" message now leads with
  `pip install "compress-cli[ffmpeg]"` before system package managers.
- On Linux and macOS the bundled binaries are made executable if the
  executable bit did not survive installation.

## 1.0.0 - 2026-08-17

Never published to PyPI; superseded by 1.1.0 before release. Requires Python
3.10 or newer.

### Added

- `compress "PATH" TARGET_MB` command. The second argument is the maximum size
  of the result in decimal megabytes (`1 MB = 1,000,000 bytes`).
- Hard target-size guarantee: every candidate is measured on disk and re-parsed
  by an independent reader (Pillow, ffprobe or pikepdf) before it can be
  reported as a success. A missed target raises `TargetNotAchievableError` and
  exits with status 1 instead of being reported as success.
- Automatic media detection from file contents, with the extension only as a
  fallback. Containers that can hold either audio or video (MP4, MKV, WebM,
  Ogg, AVI, MOV) are disambiguated with `ffprobe`.
- Image backend (Pillow) for JPEG, PNG, WebP, AVIF, GIF, BMP and TIFF: binary
  search over encoder quality, then palette reduction, then a resolution
  ladder.
- Video backend (FFmpeg) for MP4, MKV, MOV, WebM, AVI and more: analytic
  bitrate seeding followed by proportional correction from measured output,
  with an automatic resolution ladder. H.264 + AAC for MP4/MOV/MKV, VP9 + Opus
  for WebM, H.264 + MP3 for AVI.
- Audio backend (FFmpeg) for MP3, WAV, M4A, AAC, FLAC, OGG, Opus and more.
  Lossless FLAC is tried first for lossless sources; lossy sources keep their
  own codec; Opus is used when the budget drops below roughly 48 kbps.
- PDF backend (pikepdf, optionally Ghostscript): lossless structural
  optimisation first, then image re-encoding, with page-count validation on
  every candidate.
- Python API: `from compress import compress`, returning `CompressionResult`
  with `output_path`, byte-accurate sizes, `reduction_percent`, `attempts`,
  `target_achieved`, `skipped` and `format_changed`.
- Files already below the target are left completely untouched and reported as
  `skipped`.
- Output naming: `<name>_compressed<ext>` next to the original, falling back to
  `<name>_compressed_1<ext>`, `_2`, ... Names are reserved atomically with
  `O_EXCL`, so concurrent runs cannot collide and the original is never
  overwritten.
- `--output`, `--overwrite`, `--quiet`, `--json` and `--timeout` for scripting.

### Security

- Every external tool is invoked with an argument list and `shell=False`. No
  user-controlled string is ever interpolated into a shell command line.
- Scratch files are created in a private per-run directory with
  `tempfile.mkdtemp`.

[2.0.0]: https://github.com/priyadip/compress-cli/releases/tag/v2.0.0
[1.3.0]: https://github.com/priyadip/compress-cli/releases/tag/v1.3.0
[1.2.0]: https://github.com/priyadip/compress-cli/releases/tag/v1.2.0
[1.1.0]: https://github.com/priyadip/compress-cli/releases/tag/v1.1.0
