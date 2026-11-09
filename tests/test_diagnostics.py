"""Tests for the :mod:`custom_components.eleven_energy_plus.diagnostics` module.

Guarantees pinned down here:

1. The diagnostics dict has a stable, documented shape.
2. Sensitive values (API token, sign-in tokens, serial numbers, device ids)
   are redacted before the dict leaves the integration.
3. Without a loaded controller the dump is still well-formed.
4. Producing the dump does no file I/O in the event loop (Home Assistant
   reports that as a blocking call).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.eleven_energy_plus.const import (
    CONF_DEVICE_LABEL,
    CONF_POLL_INTERVAL,
    DOMAIN,
)
from custom_components.eleven_energy_plus.diagnostics import (
    TO_REDACT,
    async_get_config_entry_diagnostics,
)

from .common import MODEL_SINGLE_PHASE, device_payload, mock_api, setup_entry, site_summary

MANIFEST = Path(__file__).resolve().parent.parent / (
    "custom_components/eleven_energy_plus/manifest.json"
)


@pytest.fixture
def entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={"token": "super-secret-token"},
        options={CONF_POLL_INTERVAL: 30, CONF_DEVICE_LABEL: "My Label"},
        title="Eleven Energy Plus",
    )


def _mock(aioclient_mock: AiohttpClientMocker, device_id: str = "abc") -> None:
    payload = device_payload(device_id)
    payload["serialNumber"] = "SN-VERY-SECRET"
    summary = site_summary(device_id, serial="SN-VERY-SECRET", model=MODEL_SINGLE_PHASE)
    mock_api(aioclient_mock, devices={device_id: payload}, summaries=[summary])


class TestDiagnostics:
    """Snapshot-style coverage of the diagnostics dump."""

    async def test_dump_has_expected_top_level_shape(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        _mock(aioclient_mock)
        assert await setup_entry(hass, entry)
        assert entry.state is ConfigEntryState.LOADED

        dump = await async_get_config_entry_diagnostics(hass, entry)

        assert set(dump) == {"integration", "entry", "controller", "raw_payloads"}
        assert dump["integration"]["domain"] == DOMAIN
        manifest_version = json.loads(MANIFEST.read_text(encoding="utf-8"))["version"]
        assert dump["integration"]["version"] == manifest_version
        assert dump["entry"]["credential"] == "API token"

    async def test_no_file_io_in_the_event_loop(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        """Regression: reading manifest.json here was a blocking call in the loop."""
        _mock(aioclient_mock)
        assert await setup_entry(hass, entry)

        def _forbidden(*args, **kwargs):
            raise AssertionError("diagnostics opened a file in the event loop")

        with (
            patch.object(Path, "open", _forbidden),
            patch("builtins.open", _forbidden),
        ):
            dump = await async_get_config_entry_diagnostics(hass, entry)
        assert dump["integration"]["version"] not in ("", "unknown")

    async def test_token_is_redacted(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        _mock(aioclient_mock)
        assert await setup_entry(hass, entry)
        dump = await async_get_config_entry_diagnostics(hass, entry)
        assert dump["entry"]["data"]["token"] != "super-secret-token"
        assert "super-secret-token" not in repr(dump)

    async def test_sign_in_tokens_are_redacted(self, hass: HomeAssistant) -> None:
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                "auth_implementation": DOMAIN,
                "token": {"access_token": "acc-secret", "refresh_token": "ref-secret"},
            },
        )
        entry.add_to_hass(hass)
        dump = await async_get_config_entry_diagnostics(hass, entry)
        rendered = repr(dump)
        assert "acc-secret" not in rendered
        assert "ref-secret" not in rendered
        assert dump["entry"]["credential"] == "account sign-in"

    async def test_serial_and_device_ids_redacted_in_payloads(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        _mock(aioclient_mock, "very-real-id")
        assert await setup_entry(hass, entry)
        rendered = repr(await async_get_config_entry_diagnostics(hass, entry))
        assert "very-real-id" not in rendered
        assert "SN-VERY-SECRET" not in rendered

    async def test_user_facing_label_not_redacted(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        _mock(aioclient_mock)
        assert await setup_entry(hass, entry)
        dump = await async_get_config_entry_diagnostics(hass, entry)
        site = dump["raw_payloads"]["site"]
        assert site["devices"][0]["name"] == "North Sea 6 (6kW)"
        assert site["devices"][0]["model"]["name"] == "Mediterranean Sea 12"

    async def test_controller_block_reflects_state(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        _mock(aioclient_mock)
        assert await setup_entry(hass, entry)
        controller = (await async_get_config_entry_diagnostics(hass, entry))["controller"]
        assert controller["loaded"] is True
        assert controller["credential"] == "API token"
        assert controller["auth_failed"] is False
        assert controller["poll_interval_seconds"] == 30
        assert controller["device_label_override"] == "My Label"
        assert controller["registered_platforms"] == ["binary_sensor", "number", "sensor"]
        (device,) = controller["known_devices"]
        assert device["deviceId"] != "abc"
        assert device["label"] == "My Label"
        assert device["mppt_count"] == 2
        assert device["three_phase"] is False
        assert device["available"] is True
        assert device["entity_count"] > 10

    async def test_dump_without_loaded_controller(
        self, hass: HomeAssistant, entry: MockConfigEntry
    ) -> None:
        assert await async_setup_component(hass, DOMAIN, {})
        entry.add_to_hass(hass)
        dump = await async_get_config_entry_diagnostics(hass, entry)
        assert dump["controller"] == {"loaded": False}
        assert dump["raw_payloads"] == {"site": None, "devices": {}}
        assert dump["entry"]["data"]["token"] != "super-secret-token"


class TestRedactionSet:
    """Lock the redaction key set so dropping a key is a deliberate act."""

    def test_to_redact_is_a_frozenset(self) -> None:
        assert isinstance(TO_REDACT, frozenset)

    @pytest.mark.parametrize(
        "key",
        [
            "token",
            "access_token",
            "refresh_token",
            "serialNumber",
            "serial_number",
            "deviceId",
            "device_id",
        ],
    )
    def test_to_redact_covers_critical_keys(self, key: str) -> None:
        assert key in TO_REDACT
