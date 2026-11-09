"""Tests for the Eleven Energy Plus device actions."""

from __future__ import annotations

import pytest
import voluptuous as vol

from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_TYPE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.eleven_energy_plus import device_action
from custom_components.eleven_energy_plus.const import BASE_URL, DOMAIN
from custom_components.eleven_energy_plus.services import SERVICE_FIELDS

from .common import DEVICE_ID, api_key_entry, mock_api, setup_entry

POST_URL = f"{BASE_URL}devices/{DEVICE_ID}/operatingMode"


async def _setup(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> dr.DeviceEntry:
    mock_api(aioclient_mock)
    entry = api_key_entry()
    assert await setup_entry(hass, entry)
    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    return device


def _config(device_id: str, action_type: str, **fields) -> dict:
    return {
        CONF_DEVICE_ID: device_id,
        CONF_DOMAIN: DOMAIN,
        CONF_TYPE: action_type,
        **fields,
    }


class TestListing:
    """Every work mode is offered for an Eleven Energy Plus inverter."""

    async def test_actions_for_our_device(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        device = await _setup(hass, aioclient_mock)
        actions = await device_action.async_get_actions(hass, device.id)
        assert [action[CONF_TYPE] for action in actions] == device_action.ACTION_TYPES
        assert all(action[CONF_DEVICE_ID] == device.id for action in actions)
        assert all(action[CONF_DOMAIN] == DOMAIN for action in actions)

    async def test_no_actions_for_other_devices(self, hass: HomeAssistant) -> None:
        other = MockConfigEntry(domain="other")
        other.add_to_hass(hass)
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=other.entry_id, identifiers={("other", "x")}
        )
        assert await device_action.async_get_actions(hass, device.id) == []
        assert await device_action.async_get_actions(hass, "missing") == []

    def test_one_action_per_service(self) -> None:
        assert {f"set_work_mode_{t}" for t in device_action.ACTION_TYPES} == set(
            SERVICE_FIELDS
        )


class TestCapabilities:
    """Each action offers exactly the fields its work mode uses."""

    @pytest.mark.parametrize("action_type", device_action.ACTION_TYPES)
    async def test_fields_match_service(
        self, hass: HomeAssistant, action_type: str
    ) -> None:
        capabilities = await device_action.async_get_action_capabilities(
            hass, _config("x", action_type)
        )
        expected = SERVICE_FIELDS[f"set_work_mode_{action_type}"]
        if not expected:
            assert capabilities == {}
            return
        schema = capabilities["extra_fields"].schema
        assert {str(key): isinstance(key, vol.Required) for key in schema} == expected


class TestValidation:
    """Device action configs are validated when the automation loads."""

    async def test_valid_config(self, hass: HomeAssistant) -> None:
        config = await device_action.async_validate_action_config(
            hass, _config("x", "force_charge", target_power="3.5", target_percent=80)
        )
        assert config["target_power"] == 3.5

    @pytest.mark.parametrize(
        ("action_type", "fields", "message"),
        [
            ("force_charge", {"target_power": 3}, "requires target_percent"),
            ("reset", {"target_power": 3}, "not used"),
            ("force_charge", {"target_power": 99, "target_percent": 50}, None),
            ("target_soc", {"target_soc": 50, "target_minutes": "nan"}, None),
            ("idle_battery", {"allow_charging": "maybe"}, None),
            ("not_a_mode", {}, None),
        ],
    )
    async def test_invalid_config(
        self, hass: HomeAssistant, action_type: str, fields: dict, message: str | None
    ) -> None:
        with pytest.raises(vol.Invalid) as err:
            await device_action.async_validate_action_config(
                hass, _config("x", action_type, **fields)
            )
        if message:
            assert message in str(err.value)


class TestCalling:
    """A device action calls the action behind it, targeting its device."""

    async def test_call_targets_the_device(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        device = await _setup(hass, aioclient_mock)
        aioclient_mock.post(POST_URL, status=200)
        config = await device_action.async_validate_action_config(
            hass, _config(device.id, "grid_export", target_power=4, target_percent=20, overdrive=True)
        )
        await device_action.async_call_action_from_config(hass, config, {}, None)
        assert aioclient_mock.mock_calls[-1][2] == {
            "workMode": "gridExport",
            "rate": 4.0,
            "targetSoc": 20.0,
            "overdrive": True,
        }

    async def test_failure_raises(
        self,
        hass: HomeAssistant,
        aioclient_mock: AiohttpClientMocker,
        instant_backoff,
    ) -> None:
        device = await _setup(hass, aioclient_mock)
        aioclient_mock.post(POST_URL, status=500)
        config = await device_action.async_validate_action_config(
            hass, _config(device.id, "reset")
        )
        with pytest.raises(HomeAssistantError):
            await device_action.async_call_action_from_config(hass, config, {}, None)
