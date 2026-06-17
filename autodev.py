#!/usr/bin/env python3
"""Backward-compatible entry point.

The implementation now lives in the ``autodevloop`` package. This shim keeps
``python autodev.py run ...`` working. Prefer ``autodevloop`` (installed) or
``python -m autodevloop`` going forward.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from autodevloop.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
