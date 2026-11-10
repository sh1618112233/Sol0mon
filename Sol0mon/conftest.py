from pathlib import Path

import pytest


def pytest_collection_modifyitems(items):
    """Keep explicitly live integration scripts out of offline pytest."""
    for item in items:
        path = Path(str(item.fspath)).name.lower()
        if "live" in path or "browser" in path:
            item.add_marker(pytest.mark.live)
