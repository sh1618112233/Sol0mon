"""Compatibility wrapper for the canonical offline pytest suite."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    app = Path(__file__).resolve().parent
    legacy = sorted(str(path) for path in app.glob("test_*.py")
                    if path.name != "test_suite.py")
    result = subprocess.call(
        [sys.executable, "-m", "pytest", "-ra", "Sol0mon/tests", *legacy],
        cwd=root,
    )
    if result == 0:
        result = subprocess.call([sys.executable, str(app / "test_suite.py")], cwd=app)
    raise SystemExit(result)
