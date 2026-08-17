# ecompress

Compress any file below a target size. One command, two arguments.

```bash
ecompress "D:\Videos\movie.mp4" 50
```

> Make this file smaller than 50 MB.

That is the whole interface. You never specify a codec, a CRF, a bitrate, a
quality number or a resolution — the package works those out, encodes, measures
the real file on disk, and tries again until it fits.

```text
Compressing:
D:\Videos\movie.mp4

Original size: 82.4 MB
Target size:   50 MB

Detecting media type...
Video detected (1920x1080, 124.0s, 30 fps).

Optimizing...
  Attempt 1: 61.2 MB [3.85 Mbps]
  Attempt 2: 47.9 MB [2.98 Mbps]  <- best so far
  Attempt 3: 48.9 MB [3.06 Mbps]  <- best so far

Compression successful.

Original:    82.4 MB
Compressed:  48.9 MB
Saved:       33.5 MB
Reduction:   40.7%

Output:
D:\Videos\movie_compressed.mp4
```

## Install

```bash
pip install ecompress
```

That is the whole install. **FFmpeg comes with it** — you do not install
anything separately, and nothing needs to be on your `PATH`. Python 3.10 or
newer.

The PyPI name, the command and the import name are all `ecompress`. The
function you call is still `compress()`:

```python
from ecompress import compress
```

> **Renamed in 2.0.** This project was `compress-cli` (command `compress`) up
> to 1.3.0. The old import name shadowed an unrelated `compress` package on
> PyPI, and the old command collided with `/usr/bin/compress`, the classic Unix
> `.Z` tool, on Linux and macOS. Nothing about how it works changed — only the
> names.

Check what landed on your machine at any time:

```bash
ecompress --check
```

```text
ecompress 2.0.1
Python 3.12.10 on Windows 11 (AMD64)

Video and audio (FFmpeg)
  OK       ffmpeg version 6.0-essentials_build
           bundled with this package
           ffmpeg:  ...\site-packages\ffmpeg\binaries\bin\ffmpeg.exe
           ffprobe: ...\site-packages\ffmpeg\binaries\bin\ffprobe.exe

  OK       H.264 video (.mp4, .mkv, .mov) (libx264)
  OK       VP9 video (.webm) (libvpx-vp9)
  OK       AAC audio (.m4a) (aac)
  OK       MP3 audio (.mp3) (libmp3lame)
  OK       Opus audio (.opus) (libopus)
  OK       Vorbis audio (.ogg) (libvorbis)
  OK       FLAC audio (.flac) (flac)

Images (Pillow)
  OK       Pillow 11.3.0
           formats: JPEG, PNG, WEBP, AVIF, GIF

PDF (pikepdf)
  OK       pikepdf 10.11.0

Everything is installed. All supported file types will work.
```

### What gets installed, and where

| Component | Comes from | Installed where |
| --- | --- | --- |
| ffmpeg + ffprobe | `ffmpeg-binaries` | inside your environment's `site-packages` |
| Image support | `pillow` | `site-packages` |
| PDF support | `pikepdf` (bundles qpdf) | `site-packages` |

Everything lives inside the Python environment you installed into — a venv, a
conda env, or your user site-packages. Nothing is written to system
directories, nothing touches your `PATH`, and uninstalling removes all of it:

```bash
pip uninstall ecompress ffmpeg-binaries
```

### Platforms without a prebuilt FFmpeg

Windows x64, macOS (Intel and Apple Silicon) and Linux x86_64 get FFmpeg
automatically. On Linux ARM, Alpine/musl and 32-bit Windows there is no
prebuilt wheel, so `ecompress --check` will report it and you have two options:

```bash
pip install "ecompress[ffmpeg]"   # fetches a build for your platform
sudo apt install ffmpeg           # or use your system package manager
```

A system FFmpeg already on `PATH` is picked up automatically. To point at a
specific build, set `COMPRESS_FFMPEG` and `COMPRESS_FFPROBE`.

Images and PDFs never need FFmpeg at all.

## The target size is a hard limit

The second argument is the **maximum** size of the result in megabytes.

`1 MB = 1,000,000 bytes`, so `ecompress "video.mp4" 50` guarantees:

```text
actual_output_bytes < 50,000,000
```

This is not an estimate. Every candidate encode is measured on disk *and*
re-opened by an independent reader — Pillow for images, `ffprobe` for video and
audio, pikepdf for PDFs — before it can be accepted. A 50.1 MB result is never
reported as a success for a 50 MB request.

If the target genuinely cannot be reached, the command says so and exits with
status `1`:

```text
Error: Target size could not be achieved.

Target:               1.0 MB
Smallest valid output: 3.2 MB

Even at 256x144 and the minimum usable bitrate (24 kbps video + 32 kbps audio),
a 124.0s clip cannot fit in the requested size.
```

## Examples

```bash
# Video
ecompress "D:\Videos\movie.mp4" 50
ecompress "clip.mkv" 8
ecompress "screen-recording.webm" 25

# Images
ecompress "D:\Photos\holiday.jpg" 5
ecompress "screenshot.png" 0.5
ecompress "banner.webp" 1

# Audio
ecompress "D:\Music\song.wav" 10
ecompress "podcast.mp3" 20
ecompress "interview.m4a" 5

# PDF
ecompress "D:\Documents\report.pdf" 2
ecompress "scan.pdf" 1.5
```

Fractional targets work: `0.5`, `1.5`, `49.9` are all valid.

## Setting a minimum too

A bare ceiling lets the result land anywhere below it. Give a **range** and the
budget gets used instead of undershot:

```bash
ecompress "movie.mp4" 40-50     # below 50 MB, but not under 40 MB
```

`40-50`, `[40,50]`, `40..50` and `--min 40` all mean the same thing.

The two ends behave differently, deliberately:

- **The maximum is a hard limit.** The result is always strictly below it.
- **The minimum is a quality floor.** The search keeps raising quality until it
  reaches it — that is the point, since a bigger file at the same settings means
  fewer artefacts.

If a file genuinely cannot reach the floor — the source is already small, or the
format has nothing left to give — you get the result anyway, with a note saying
why. **Padding a file with junk to hit a number is never done**, because those
bytes would add size without adding quality.

`result.within_requested_range` tells you whether both ends were satisfied.

## Where the output goes

Next to the original, with `_compressed` before the extension:

```text
D:\Videos\movie.mp4      ->  D:\Videos\movie_compressed.mp4
D:\Photos\photo.jpg      ->  D:\Photos\photo_compressed.jpg
D:\Music\song.wav        ->  D:\Music\song_compressed.mp3
```

**The original is never modified or overwritten.** If
`movie_compressed.mp4` already exists, the next free name is used —
`movie_compressed_1.mp4`, `movie_compressed_2.mp4`, and so on. Names are
reserved atomically, so two runs at the same time cannot collide.

The absolute output path is printed on success and is available as
`result.output_path`.

## Already small enough?

Nothing is re-encoded and nothing is degraded:

```text
File is already below the requested target.

Original: 20.0 MB
Target:   50 MB

No compression necessary; the original was left untouched.
```

No new file is created — `result.output_path` is the original path and
`result.skipped` is `True`.

## Supported files

| Kind      | Extensions                                                                 | Engine             |
| --------- | -------------------------------------------------------------------------- | ------------------ |
| **Image** | `.jpg` `.jpeg` `.png` `.webp` `.avif` `.gif` `.bmp` `.tif` `.tiff`          | Pillow             |
| **Video** | `.mp4` `.mkv` `.mov` `.webm` `.avi` `.m4v` `.wmv` `.flv` `.mpg` `.ts` `.3gp` | FFmpeg             |
| **Audio** | `.mp3` `.wav` `.m4a` `.aac` `.flac` `.ogg` `.opus` `.wma` `.aiff`           | FFmpeg             |
| **PDF**   | `.pdf`                                                                      | pikepdf (+ Ghostscript when installed) |

The media type is detected from the file's **contents**, not its name, so a
mislabelled `.mp4` that actually holds a PDF is handled correctly. An `.mp4`
containing only an audio track is compressed as audio.

## When the format changes

Your extension is preserved whenever it can be. It changes only when keeping it
would make the target unreachable, and the change is always reported:

| Input   | Output    | Why                                                                    |
| ------- | --------- | ---------------------------------------------------------------------- |
| `.wav`  | `.flac`   | Lossless and typically 40–60% smaller. Audio is bit-for-bit identical.  |
| `.wav`  | `.mp3`    | When even FLAC will not fit.                                           |
| `.mp3`  | `.opus`   | Only below roughly 48 kbps, where MP3 stops being listenable.          |
| `.png`  | `.webp`   | Last resort, after lossless optimisation, palette reduction and downscaling have all failed. Transparency is preserved. |
| `.webm` | `.mp4`    | Only if this FFmpeg build has no VP9 encoder.                          |

`result.format_changed` tells you whether this happened.

## How it picks settings

The problem being solved is:

```text
maximise quality   subject to   output_size < target_size
```

Not "make it as small as possible" — a 50 MB request should come back at
48.9 MB with good quality, not 12 MB with bad quality.

- **Images** — binary search over encoder quality at full resolution. PNGs try
  maximum-effort lossless deflate first, then adaptive palette reduction, and
  only then downscaling.
- **Video** — the byte budget is split into audio + container overhead + video,
  an opening bitrate is derived from the duration, and each measurement
  corrects the next guess proportionally. Resolution and frame rate are then
  chosen **together** (see below).
- **Audio** — lossless FLAC first for lossless sources. Otherwise the source's
  own codec at a searched bitrate, dropping to mono, then to a lower sample
  rate, then to Opus.
- **PDF** — lossless structural optimisation first (text, fonts and vectors are
  never touched). Then embedded images are re-encoded at a searched quality.
  Images that would grow, and stencil masks, are left alone.

Encodes are budgeted (six to eight per run), so a run converges instead of
grinding through dozens of attempts.

### Resolution and frame rate are traded together

When a video budget is too thin for the source, there are two things to give
up: pixels per frame, and frames per second. Sacrificing only resolution — the
obvious approach — throws away detail that a small frame-rate cut would have
paid for.

A 5-minute 4K clip at 62 fps squeezed into 50 MB is the worst case: those bits
have to cover twice as many frames as a 30 fps video, starving every one of
them.

| | Resolution only | Both levers |
| --- | --- | --- |
| Result | 640x360 @ 62 fps | **1024x576 @ 31 fps** |
| Pixels per frame | 230,400 | **589,824** (2.6x) |

The cost of a combination follows published streaming ladders, where doubling
frame rate costs about **1.5x** the bitrate rather than 2x — consecutive frames
are more alike the faster you sample:

```text
required_bitrate = 1.5 * width * height * (fps / 30) ** 0.585
```

Every affordable combination is then ranked, weighting resolution above frame
rate, and the best one is encoded. Frame rate is never reduced below 24 fps,
never raised, and never touched at all when the budget comfortably covers the
source.

That first choice is only a prediction, and measurement overrides it in **both**
directions. Video with little movement — a screen recording, a slide deck —
cannot absorb the bitrate it is given: past a point the encoder is already at
its best quality for that frame size, and more bits change nothing. When that
happens the spare budget is spent on **pixels instead of bitrate**, climbing
back towards the source resolution and frame rate until the file fills the
window. The ceiling is still absolute; the climb stops before crossing it.

Both constants are heuristics tuned to established practice, not measurements
of your specific clip — very high-motion footage or a slideshow will not match
them exactly.

## Python API

```python
from ecompress import compress

result = compress(r"D:\Videos\movie.mp4", 50)

print(result.output_path)  # D:\Videos\movie_compressed.mp4
print(result.output_size_mb)  # 48.87
assert result.output_size_bytes < 50_000_000
```

`CompressionResult` carries:

| Field                | Meaning                                            |
| -------------------- | -------------------------------------------------- |
| `input_path`         | the original file, untouched                       |
| `output_path`        | absolute path of the result                        |
| `input_size_bytes`   | original size                                      |
| `output_size_bytes`  | measured size of the result                        |
| `target_size_bytes`  | the ceiling, `target_mb * 1_000_000`               |
| `min_size_bytes`     | the floor, when a range was given, else `None`     |
| `within_requested_range` | whether both ends were satisfied               |
| `saved_bytes`        | bytes removed                                      |
| `reduction_percent`  | percentage removed                                 |
| `media_type`         | `MediaType.VIDEO`, `.IMAGE`, `.AUDIO` or `.PDF`    |
| `attempts`           | every measured encode, with its settings and size  |
| `target_achieved`    | always `True` on a returned result                 |
| `skipped`            | `True` when the input was already small enough     |
| `format_changed`     | `True` when the extension had to change            |
| `notes`              | human-readable explanations of any decisions       |

Optional keyword arguments:

```python
compress(
    path,
    target_mb,  # 50, "50", "40-50", (40, 50)
    min_mb=None,  # a floor, as an alternative to the range form
    output_path=None,  # write somewhere specific
    reporter=None,  # progress callbacks; ConsoleReporter() prints them
    overwrite=False,  # allow output_path to replace an existing file
    timeout=None,  # seconds per encoder invocation
)
```

### Errors

All of them subclass `CompressError`:

| Exception                  | Raised when                                   |
| -------------------------- | --------------------------------------------- |
| `InputFileError`           | missing, empty, unreadable, or a directory     |
| `InvalidTargetError`       | the target is not a positive, finite number    |
| `UnsupportedFormatError`   | no backend handles this file type              |
| `MissingDependencyError`   | FFmpeg is needed but not installed             |
| `TargetNotAchievableError` | no valid output fits under the target          |
| `OutputValidationError`    | a produced file failed its final check         |

```python
from ecompress import compress, TargetNotAchievableError

try:
    result = compress("video.mp4", 0.1)
except TargetNotAchievableError as exc:
    print(exc.smallest_valid_bytes)  # what was actually achievable
```

## Command-line options

The two positional arguments are the whole primary interface. These exist for
scripting:

```text
-o, --output PATH   write here instead of <name>_compressed<ext>
    --overwrite     allow --output to replace an existing file
-q, --quiet         print only the output path
    --json          print the result as JSON
    --timeout SEC   give up on a single encoder run after this long
    --version
```

Exit codes: `0` success, `1` target not achievable, `2` bad input or usage,
`3` FFmpeg missing.

```bash
ecompress "movie.mp4" 50 --json
```

```json
{
  "input_path": "D:\\Videos\\movie.mp4",
  "output_path": "D:\\Videos\\movie_compressed.mp4",
  "output_size_bytes": 48871234,
  "target_size_bytes": 50000000,
  "reduction_percent": 40.6912,
  "media_type": "video",
  "attempts": 3,
  "target_achieved": true
}
```

## What this tool will not do

The size guarantee is met by genuinely re-encoding, never by faking it. The
package will not truncate files, strip bytes, break containers, rename
extensions to disguise content, or write sparse files. If a valid file below
the target cannot be produced, it says so and writes nothing.

## Notes and limits

- Sizes are decimal (`1 MB = 1,000,000 bytes`), matching how storage is sold
  and how upload limits are usually quoted. Windows Explorer shows binary MB
  (1,048,576 bytes), so it will report a slightly *smaller* number than this
  tool does — never a larger one.
- A video needs a readable duration for bitrate targeting; without one the
  tool falls back to a quality-based search.
- A PDF that is pure text or vector art cannot be shrunk much. Rather than
  mangling the document, the tool reports what it managed and why.
- Subtitle, data and chapter tracks are dropped from re-encoded video.
- Encrypted or password-protected PDFs are rejected with a clear message.
- FFmpeg can be pointed at explicitly with the `COMPRESS_FFMPEG` and
  `COMPRESS_FFPROBE` environment variables; Ghostscript with
  `COMPRESS_GHOSTSCRIPT`.

## Development

```bash
git clone https://github.com/priyadip/compress-cli
cd ecompress
pip install -e ".[dev]"

pytest                  # add -m "not slow" to skip real encodes
ruff check . && ruff format --check .
mypy .
python -m build && twine check dist/*
```

## License

MIT — see [LICENSE](LICENSE).
