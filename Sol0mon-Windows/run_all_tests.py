"""Compatibility wrapper for the canonical offline pytest suite."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    legacy = sorted(str(path) for path in root.glob("test_*.py")
                    if path.name != "test_suite.py")
    result = subprocess.call(
        [sys.executable, "-m", "pytest", "-ra", "tests", *legacy], cwd=root,
    )
    if result == 0:
        result = subprocess.call([sys.executable, str(root / "test_suite.py")], cwd=root)
    raise SystemExit(result)
