"""Tests for binary_sensor.py and the binary sensors it registers."""

from __future__ import annotations

import pytest

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.eleven_energy_plus.const import DOMAIN

from .common import DEVICE_ID, api_key_entry, device_payload, mock_api, setup_entry

ONLINE = f"binary_sensor.{DEVICE_ID}_system_online"


@pytest.mark.covers("FC-ENT-01", "FC-ENT-09")
async def test_platform_registers_every_binary_sensor_in_its_domain(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_api(aioclient_mock)
    entry = api_key_entry()
    assert await setup_entry(hass, entry)
    registry = er.async_get(hass)
    entities = entry.runtime_data.devices[DEVICE_ID].binary_sensor_entities
    assert set(entities) == {"online", "_api_connection"}
    for entity in entities.values():
        reg_entry = registry.async_get(entity.entity_id)
        assert reg_entry is not None
        assert reg_entry.domain == "binary_sensor"
        assert reg_entry.platform == DOMAIN
    assert hass.states.get(ONLINE).state == STATE_ON


@pytest.mark.covers("FC-VAL-02", "FC-ENT-06")
@pytest.mark.parametrize(
    ("value", "expected"),
    [("false", STATE_OFF), (1, STATE_ON), ("maybe", STATE_UNKNOWN), (2, STATE_UNKNOWN)],
)
async def test_online_and_auto_discovered_flags_parse_strictly(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, value: object, expected: str
) -> None:
    mock_api(aioclient_mock)
    entry = api_key_entry()
    assert await setup_entry(hass, entry)
    reading = device_payload()
    reading["online"] = value
    reading["extra"] = {"gridFault": True}
    aioclient_mock.clear_requests()
    mock_api(aioclient_mock, devices={DEVICE_ID: reading})
    await entry.runtime_data.poll_devices()
    await hass.async_block_till_done()
    assert hass.states.get(ONLINE).state == expected
    fault = hass.states.get(f"binary_sensor.{DEVICE_ID}_extra_gridfault")
    assert fault is not None
    # Telemetry of an inverter that reports itself offline is unavailable.
    assert fault.state == (STATE_UNAVAILABLE if expected == STATE_OFF else STATE_ON)
    assert fault.attributes["device_class"] == "problem"
