"""Allow ``python -m compress "file.mp4" 50``."""

from __future__ import annotations

from compress.cli import main

__all__ = ["main"]

if __name__ == "__main__":
    raise SystemExit(main())
