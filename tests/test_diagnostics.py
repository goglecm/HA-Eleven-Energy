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


@pytest.mark.defect("D39")
@pytest.mark.covers("FC-DIAG-01", "FC-AVAIL-04")
async def test_a_stopped_controller_is_not_reported_as_loaded(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, entry: MockConfigEntry
) -> None:
    _mock(aioclient_mock)
    assert await setup_entry(hass, entry)
    with patch.object(hass.config_entries, "async_unload_platforms", return_value=False):
        assert not await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.FAILED_UNLOAD
    dump = await async_get_config_entry_diagnostics(hass, entry)
    assert dump["controller"]["loaded"] is False


def _mock(aioclient_mock: AiohttpClientMocker, device_id: str = "abc") -> None:
    payload = device_payload(device_id)
    payload["serialNumber"] = "SN-VERY-SECRET"
    summary = site_summary(device_id, serial="SN-VERY-SECRET", model=MODEL_SINGLE_PHASE)
    mock_api(aioclient_mock, devices={device_id: payload}, summaries=[summary])


class TestDiagnostics:
    """Snapshot-style coverage of the diagnostics dump."""

    @pytest.mark.covers("FC-DIAG-01")
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

    @pytest.mark.covers("FC-DIAG-01")
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

    @pytest.mark.covers("FC-DIAG-02")
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

    @pytest.mark.covers("FC-DIAG-02")
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

    @pytest.mark.covers("FC-DIAG-02")
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

    @pytest.mark.covers("FC-DIAG-03")
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

    @pytest.mark.defect("D34")
    @pytest.mark.covers("FC-DIAG-04")
    async def test_dump_without_loaded_controller(
        self, hass: HomeAssistant, entry: MockConfigEntry
    ) -> None:
        """The payload list has the same type loaded or not (it was a dict here)."""
        assert await async_setup_component(hass, DOMAIN, {})
        entry.add_to_hass(hass)
        dump = await async_get_config_entry_diagnostics(hass, entry)
        assert dump["controller"] == {"loaded": False}
        assert dump["raw_payloads"] == {"site": None, "devices": []}
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
            "address",
            "postcode",
            "postCode",
            "latitude",
            "longitude",
            "email",
            "phone",
        ],
    )
    def test_to_redact_covers_critical_keys(self, key: str) -> None:
        assert key in TO_REDACT


class TestFaultState:
    """Diagnostics say what is wrong right now."""

    @pytest.mark.covers("FC-DIAG-03")
    async def test_issues_and_inverter_state_are_reported(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        reading = device_payload(model=MODEL_SINGLE_PHASE, mppts=[{"index": 7, "power": 1.0}])
        reading["online"] = False
        mock_api(aioclient_mock, devices={"abc": reading})
        entry = MockConfigEntry(domain=DOMAIN, data={"token": "t"}, options={CONF_POLL_INTERVAL: 30})
        assert await setup_entry(hass, entry)
        controller = entry.runtime_data
        controller.faults.report_missing("abc", "Eleven Energy Plus X", True)
        dump = await async_get_config_entry_diagnostics(hass, entry)
        snapshot = dump["controller"]
        assert snapshot["issues"] == ["inverter_missing_abc"]
        (device,) = snapshot["known_devices"]
        assert device["offline"] is True
        assert device["missing"] is False
        assert device["api_connected"] is True
        assert device["last_successful_poll"] == controller.devices["abc"].last_success_at.isoformat()
        controller.devices["abc"].last_success_at = None
        dump = await async_get_config_entry_diagnostics(hass, entry)
        assert dump["controller"]["known_devices"][0]["last_successful_poll"] is None
        assert device["ignored_list_items"] == {
            "pv.mppts:out_of_range:7": "MPPT index 7 is not one of this model's 2 trackers"
        }

    @pytest.mark.covers("FC-DIAG-01")
    async def test_version_lookup_failure_is_logged(
        self, hass: HomeAssistant, entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
    ) -> None:
        entry.add_to_hass(hass)
        with patch(
            "custom_components.eleven_energy_plus.diagnostics.async_get_integration",
            side_effect=RuntimeError("loader broke"),
        ):
            dump = await async_get_config_entry_diagnostics(hass, entry)
        assert dump["integration"]["version"] == "unknown"
        assert "could not read its own version" in caplog.text
