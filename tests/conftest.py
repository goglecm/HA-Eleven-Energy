"""Shared fixtures for the Eleven Energy Plus test suite."""

from __future__ import annotations

from collections.abc import Generator
import logging
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

# Make ``custom_components.eleven_energy_plus`` importable as a top-level
# package so pytest-homeassistant-custom-component picks it up via its custom
# integration loader.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


pytest_plugins = ["pytest_homeassistant_custom_component"]

# Messages Home Assistant logs when an integration uses a deprecated or
# unsafe API ("Detected that custom integration ... This will stop working
# in ...", "Detected blocking call to open ... inside the event loop ...").
# Any of these mentioning this integration fails the test that caused it.
_HA_USAGE_REPORT_MARKERS = (
    "Detected that custom integration",
    "Detected blocking call",
    "Detected code that",
    "will stop working",
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Make the custom integration loader available to every test."""
    yield


@pytest.fixture(autouse=True)
def fail_on_ha_usage_reports(
    caplog: pytest.LogCaptureFixture,
) -> Generator[None]:
    """Fail any test during which Home Assistant reports misuse by this integration."""
    caplog.set_level(logging.WARNING, logger="homeassistant")
    yield
    offending = [
        record.getMessage()
        for when in ("setup", "call")
        for record in caplog.get_records(when)
        if record.levelno >= logging.WARNING
        and "eleven_energy_plus" in record.getMessage()
        and any(marker in record.getMessage() for marker in _HA_USAGE_REPORT_MARKERS)
    ]
    if offending:
        pytest.fail(
            "Home Assistant reported misuse by eleven_energy_plus:\n"
            + "\n".join(offending)
        )


@pytest.fixture
def instant_backoff() -> Generator[list[float]]:
    """Make POST retry backoff instantaneous; yields the requested delays."""
    delays: list[float] = []

    async def _no_wait(self, delay: float) -> None:
        delays.append(delay)

    with patch(
        "custom_components.eleven_energy_plus.controller.Controller._wait_backoff",
        _no_wait,
    ):
        yield delays
