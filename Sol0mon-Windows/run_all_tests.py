"""Compatibility wrapper for the canonical offline pytest suite."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    raise SystemExit(subprocess.call(
        [sys.executable, "-m", "pytest", "-ra"], cwd=root,
    ))
