"""Tests for sensor.py and the sensor entities it registers."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.eleven_energy_plus.const import DOMAIN

from .common import (
    DEVICE_ID,
    MODEL_SINGLE_PHASE,
    api_key_entry,
    device_payload,
    mock_api,
    restore_sensor_value,
    setup_entry,
    site_summary,
)

ENERGY_TODAY = f"sensor.{DEVICE_ID}_pv_energy_today"


async def _poll(hass: HomeAssistant, entry: Any, aioclient_mock: AiohttpClientMocker, reading: dict[str, Any]) -> str:
    aioclient_mock.clear_requests()
    mock_api(aioclient_mock, devices={DEVICE_ID: reading})
    await entry.runtime_data.poll_devices()
    await hass.async_block_till_done()
    return hass.states.get(ENERGY_TODAY).state


def _reading(energy_today: float) -> dict[str, Any]:
    reading = device_payload(model=MODEL_SINGLE_PHASE)
    reading["pv"]["energyToday"] = energy_today
    return reading


@pytest.mark.covers("FC-ENT-01", "FC-ENT-09")
async def test_platform_registers_every_sensor_in_the_sensor_domain(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_api(
        aioclient_mock,
        devices={DEVICE_ID: device_payload(model=MODEL_SINGLE_PHASE)},
        summaries=[site_summary(model=MODEL_SINGLE_PHASE)],
    )
    entry = api_key_entry()
    assert await setup_entry(hass, entry)
    inverter = entry.runtime_data.devices[DEVICE_ID]
    registry = er.async_get(hass)
    for sensor in inverter.sensor_entities.values():
        reg_entry = registry.async_get(sensor.entity_id)
        assert reg_entry is not None, sensor.entity_id
        assert reg_entry.domain == "sensor"
        assert reg_entry.platform == DOMAIN
    state = hass.states.get(ENERGY_TODAY)
    assert state.attributes["unit_of_measurement"] == "kWh"
    assert state.attributes["device_class"] == "energy"
    assert state.attributes["state_class"] == "total_increasing"


@pytest.mark.covers("FC-TIME-02", "FC-VAL-04")
@pytest.mark.parametrize("zone", ["Europe/London", "Pacific/Auckland", "America/Los_Angeles"])
async def test_a_midnight_reset_is_accepted_in_any_timezone(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: Any, zone: str
) -> None:
    """The counter resets at local midnight; two lower readings confirm it."""
    await hass.config.async_set_time_zone(zone)
    before_midnight = datetime(2026, 11, 9, 23, 59, tzinfo=ZoneInfo(zone))
    freezer.move_to(before_midnight)
    mock_api(aioclient_mock, devices={DEVICE_ID: _reading(12.4)})
    entry = api_key_entry()
    assert await setup_entry(hass, entry)
    assert hass.states.get(ENERGY_TODAY).state == "12.4"

    freezer.move_to(before_midnight + timedelta(minutes=1))
    assert await _poll(hass, entry, aioclient_mock, _reading(0.0)) == STATE_UNKNOWN
    freezer.move_to(before_midnight + timedelta(minutes=2))
    assert await _poll(hass, entry, aioclient_mock, _reading(0.01)) == "0.01"
    freezer.move_to(before_midnight + timedelta(minutes=3))
    assert await _poll(hass, entry, aioclient_mock, _reading(0.02)) == "0.02"


@pytest.mark.covers("FC-TIME-03", "FC-VAL-04")
async def test_the_counter_debounce_ignores_the_clocks_going_back(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: Any
) -> None:
    """01:00-02:00 happens twice on 25 October 2026 in London; readings still rule."""
    await hass.config.async_set_time_zone("Europe/London")
    freezer.move_to(datetime(2026, 10, 25, 0, 30, tzinfo=ZoneInfo("UTC")))
    mock_api(aioclient_mock, devices={DEVICE_ID: _reading(1.0)})
    entry = api_key_entry()
    assert await setup_entry(hass, entry)
    freezer.move_to(datetime(2026, 10, 25, 1, 30, tzinfo=ZoneInfo("UTC")))
    assert await _poll(hass, entry, aioclient_mock, _reading(1.2)) == "1.2"
    freezer.move_to(datetime(2026, 10, 25, 1, 45, tzinfo=ZoneInfo("UTC")))
    assert await _poll(hass, entry, aioclient_mock, _reading(1.1)) == STATE_UNKNOWN
    assert await _poll(hass, entry, aioclient_mock, _reading(1.3)) == "1.3"


@pytest.mark.covers("FC-VAL-05")
async def test_a_restored_value_without_a_reading_changes_nothing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = api_key_entry()
    entry.add_to_hass(hass)
    restore_sensor_value(hass, entry, ENERGY_TODAY, 5.0, "kWh")
    reading = device_payload()
    del reading["pv"]["energyToday"]
    mock_api(aioclient_mock, devices={DEVICE_ID: reading})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(ENERGY_TODAY).state == STATE_UNKNOWN
    assert await _poll(hass, entry, aioclient_mock, _reading(4.0)) == STATE_UNKNOWN
    assert await _poll(hass, entry, aioclient_mock, _reading(5.5)) == "5.5"


@pytest.mark.parametrize(
    "restored", [None, "not a number", -3.0], ids=["no_value", "text", "negative"]
)
async def test_an_unusable_restored_value_is_ignored(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, restored: Any
) -> None:
    entry = api_key_entry()
    entry.add_to_hass(hass)
    restore_sensor_value(hass, entry, ENERGY_TODAY, restored, "kWh")
    mock_api(aioclient_mock, devices={DEVICE_ID: _reading(0.5)})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(ENERGY_TODAY).state == "0.5"


async def test_measurements_are_not_debounced(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_api(aioclient_mock)
    entry = api_key_entry()
    assert await setup_entry(hass, entry)
    reading = device_payload()
    reading["pv"]["power"] = 0.0
    aioclient_mock.clear_requests()
    mock_api(aioclient_mock, devices={DEVICE_ID: reading})
    await entry.runtime_data.poll_devices()
    await hass.async_block_till_done()
    assert hass.states.get(f"sensor.{DEVICE_ID}_pv_power").state == "0.0"


async def test_sensors_go_unavailable_when_the_entry_unloads(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_api(aioclient_mock)
    entry = api_key_entry()
    assert await setup_entry(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(ENERGY_TODAY).state == STATE_UNAVAILABLE
