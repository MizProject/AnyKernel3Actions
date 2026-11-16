#!/usr/bin/env python3
"""Executable entrypoint for AnyKernel3Actions."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure repo root is importable regardless of the caller's working directory
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ak3_actions.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

