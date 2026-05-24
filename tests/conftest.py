"""Shared fixtures for the Eleven Energy test suite."""

from __future__ import annotations

from collections.abc import Generator
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


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None, None, None]:
    """Make the custom integration loader available to every test."""
    yield


@pytest.fixture
def patch_send_post() -> Generator[patch, None, None]:
    """Patch :meth:`Controller.send_reliable_post` so tests don't hit aiohttp."""
    with patch(
        "custom_components.eleven_energy_plus.controller.Controller.send_reliable_post"
    ) as mock:
        yield mock
