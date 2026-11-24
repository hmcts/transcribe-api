"""The heavy cases of the long-recording end-to-end test are opt-in.

They generate real 1 h to 8 h recordings and convert and split them (minutes of ffmpeg, up to a few
GB of temporary disk), so they are deselected (not skipped: they do not count as skips) unless
``RUN_LONG_E2E=1``. Every scenario also runs at a scaled-down size in the normal suite.
"""

from __future__ import annotations

import os

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "long_e2e: real 1 h to 8 h recordings (opt-in: RUN_LONG_E2E=1)")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("RUN_LONG_E2E") == "1":
        return
    heavy = [item for item in items if item.get_closest_marker("long_e2e")]
    if heavy:
        config.hook.pytest_deselected(items=heavy)
        items[:] = [item for item in items if item not in heavy]
