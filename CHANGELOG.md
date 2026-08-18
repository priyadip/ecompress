# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.4.0] - 2026-08-18

### Added

- **A progress bar while encoding.** Long encodes printed nothing for minutes
  at a time, so a run was indistinguishable from a hang. FFmpeg's own
  `-progress` stream now drives a bar with a percentage, an estimate of the
  time left, and what is being encoded:

  ```text
    [#####...................]  21%  0:09 left  2560x1440 @ 24 fps
  ```

  It is drawn only to a real terminal - piped into a file or another program
  it stays silent, and `--quiet` and `--json` are unaffected. The estimate is
  withheld for the first few percent, where it would be dominated by start-up
  cost and read wildly wrong.

### Fixed

- **Quality targeting could replace a better result with a worse one.** On a 4K
  screen recording the bitrate search reached 38.3 MB, the CRF fill managed
  only 34.4 MB, and the fill's result was adopted anyway - delivering the
  smaller of the two against a 40 MB floor. The fill now reports the size it
  achieved, and is only taken if it actually improves on what came before.

- The CRF ladder stopped at 16, which was not always enough to fill a window on
  very compressible 4K. It now runs down to 10, close to visually lossless.

- `--timeout` was not enforced during an encode. Reading FFmpeg's progress
  stream blocks until it exits, so the deadline could only be noticed after the
  work had already finished. A watchdog now stops the encode on time.

### Notes

- `test_hostile_arguments_arrive_unchanged` depended on the *child* process's
  stdout encoding, which is cp1252 on a Windows console; it passed in CI only
  because `PYTHONUTF8=1` is set there. The test now compares bytes, so it
  checks what it meant to check - that the argument survives the process
  boundary - regardless of console encoding.

## [2.3.0] - 2026-08-18

### Fixed

- **A bitrate proven to work is no longer thrown away when the resolution
  changes.** Every rung of the ladder restarted its search from the original
  opening estimate, so having learned that 3.13 Mbps worked at 1440p, the
  search would drop back to 965 kbps on moving to 2160p — and the higher
  resolution produced a *smaller* file than the rung below it:

  ```text
  Attempt 2: 21.0 MB [3.13 Mbps @ 2560x1440]   learned 3.13 Mbps works
  Attempt 3: 16.2 MB [965 kbps  @ 3840x2160]   threw it away
  ```

  The learned rate now carries forward, so each climb makes the file larger,
  monotonically, and the size floor is reached in fewer attempts.

- The decision to climb is now judged on what the *current* frame size
  achieved, rather than on a global best that may have come from a different
  rung. The saturation test likewise compares results within one rung instead
  of assuming any second encode means the encoder gave up.

### Changed

- **Long sources are searched on a 30-second sample.** Each search pass had to
  encode the whole file, and on a long 4K source a single pass runs well below
  realtime — a run needing five passes could take over an hour. Above 90
  seconds the search now runs on a slice taken a quarter of the way in, and
  only the settings that win get a full-length encode.

  The prediction is never the result: the full encode is measured and validated
  exactly as before, and if the sample misjudged the file the bitrate is
  corrected and the encode repeated (up to twice). Quality is unaffected — the
  delivered file is a full-quality encode either way.

## [2.2.0] - 2026-08-18

### Fixed

- **A size floor is now reachable even at source resolution.** 2.0.1 taught the
  search to climb when the encoder could not spend its budget, and 2.1.0 made
  the opening guess accurate — but both worked through `-b:v`, an *average
  bitrate* target. libx264 will not pad a stream with bits the content does not
  call for, so on very compressible video it undershoots whatever number it is
  given. Once the climb reached the source resolution there was nowhere left to
  go and the result still fell short of the floor.

  The search now switches to `-crf`, which fixes a *quality level* rather than
  an average rate, so lowering it always produces more bits. On static but
  detailed 4K the difference is decisive:

  ```text
  ABR 400 kbps  ->  280 kbps   (saturated, will not spend more)
  CRF 24        ->  564 kbps
  CRF 16        ->  786 kbps   (2.8x)
  ```

  A 4K clip that previously stopped at 752 KB against a 1.01 MB floor now lands
  at 1.18 MB, still at full 3840x2160.

### Changed

- When the encoder is known to be saturated, the climb jumps straight to the
  source resolution instead of stepping one rung at a time. The intermediate
  rungs cannot help — for content that cannot absorb bits, the source is both
  the largest file and the best quality — and skipping them saves several
  encodes on large sources.

## [2.1.0] - 2026-08-18

### Changed

- **The opening guess is now calibrated from the source instead of a generic
  constant.** 2.0.1 could recover from a bad first guess by climbing, but it
  still *started* from an assumption about typical footage — and then spent
  several encodes discovering the assumption was wrong.

  The source file already answers the question. Its bitrate, resolution and
  frame rate give a measured bits-per-pixel for this specific content, free,
  before anything is re-encoded. A 4K 62.5 fps screen recording at 3.3 Mbps
  measures 0.26 against the generic 1.5 — 5.7x more compressible — and the
  starting plan moves accordingly:

  | | Starting plan | Pixels per frame |
  | --- | --- | --- |
  | Generic constant | 1024x576 @ 31.25 fps | 589,824 |
  | Source-calibrated | 2560x1440 @ 24 fps | 3,686,400 (6.2x) |

  The measurement can only ever *lower* the requirement, never raise it: a
  visually-lossless source shows the content **can** absorb bits, not that it
  needs them. Without that cap a high-quality clip would be judged unable to
  hold its own resolution at 90% of its own bitrate, and would lose frame rate
  for no reason. A crushed source is floored for the same reason in reverse.

  The measurement-driven climb from 2.0.1 is unchanged and still corrects
  whatever the opening guess gets wrong; it now has far less to correct.

## [2.0.1] - 2026-08-17

### Fixed

- **Easily-compressed video no longer settles far below the requested size.**
  Content with little movement — a screen recording, a slide deck, a talking
  head on a static background — cannot absorb the bitrate it is given. Past a
  point the encoder is already at its best quality for that frame size and
  tripling the bitrate produces the same file.

  The search only knew how to move *down* the quality ladder, so it accepted
  that undersized result: a 151 MB 4K recording asked for 40-50 MB came back as
  12.5 MB of 576p, when the budget could have paid for something far sharper.

  It now recognises a saturated encoder and spends the spare budget on **pixels
  instead of bitrate**, climbing back towards the source resolution and frame
  rate. The step size comes from the measured bits-per-pixel of the encode that
  undershot, so it converges in a few attempts rather than crawling one rung at
  a time, and the bitrate hunt is skipped on the way up because it is already
  known not to help.

  The ceiling is still absolute — climbing stops the moment a candidate would
  cross it.

- Derived frame rates are no longer reported with false precision: a 62.4999 fps
  source now halves to `31.25 fps` rather than `31.2499 fps`.

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

[2.4.0]: https://github.com/priyadip/ecompress/releases/tag/v2.4.0
[2.3.0]: https://github.com/priyadip/ecompress/releases/tag/v2.3.0
[2.2.0]: https://github.com/priyadip/ecompress/releases/tag/v2.2.0
[2.1.0]: https://github.com/priyadip/ecompress/releases/tag/v2.1.0
[2.0.1]: https://github.com/priyadip/compress-cli/releases/tag/v2.0.1
[2.0.0]: https://github.com/priyadip/compress-cli/releases/tag/v2.0.0
[1.3.0]: https://github.com/priyadip/compress-cli/releases/tag/v1.3.0
[1.2.0]: https://github.com/priyadip/compress-cli/releases/tag/v1.2.0
[1.1.0]: https://github.com/priyadip/compress-cli/releases/tag/v1.1.0
