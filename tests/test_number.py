"""Tests for number.py: the Update Interval entity."""

from __future__ import annotations

import math
from typing import Any

import pytest

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.eleven_energy_plus.const import BASE_URL, CONF_POLL_INTERVAL

from .common import DEVICE_ID, api_key_entry, mock_api, setup_entry

NUMBER = f"number.{DEVICE_ID}_poll_interval"


async def _set(hass: HomeAssistant, value: Any) -> None:
    await hass.services.async_call(
        "number", "set_value", {"entity_id": NUMBER, "value": value}, blocking=True
    )


@pytest.mark.covers("FC-OPT-05")
async def test_setting_the_interval_updates_the_options(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_api(aioclient_mock)
    entry = api_key_entry()
    assert await setup_entry(hass, entry)
    await _set(hass, 60)
    await hass.async_block_till_done()
    assert entry.options[CONF_POLL_INTERVAL] == 60
    assert hass.states.get(NUMBER).state == "60.0"


@pytest.mark.covers("FC-OPT-05")
async def test_setting_the_current_value_changes_nothing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_api(aioclient_mock)
    entry = api_key_entry()
    assert await setup_entry(hass, entry)
    options_before = entry.options
    await _set(hass, 30)
    assert entry.options is options_before


@pytest.mark.covers("FC-OPT-05")
@pytest.mark.parametrize("value", [math.nan, 47.5], ids=["nan", "fraction"])
async def test_unusable_values_are_refused_with_a_translated_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, value: float
) -> None:
    mock_api(aioclient_mock)
    entry = api_key_entry()
    assert await setup_entry(hass, entry)
    with pytest.raises(ServiceValidationError) as err:
        await _set(hass, value)
    assert err.value.translation_key == "poll_interval_invalid"
    assert err.value.translation_placeholders["min"] == "5"
    assert entry.options[CONF_POLL_INTERVAL] == 30


async def test_an_out_of_range_value_reaching_the_entity_is_refused(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Home Assistant checks the range first; the entity checks it again."""
    mock_api(aioclient_mock)
    entry = api_key_entry()
    assert await setup_entry(hass, entry)
    number = next(
        entity
        for entity in hass.data["number"].entities
        if entity.entity_id == NUMBER
    )
    with pytest.raises(ServiceValidationError):
        await number.async_set_native_value(301)


@pytest.mark.covers("FC-AVAIL-05")
async def test_the_number_stays_available_while_the_api_is_down(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_api(aioclient_mock)
    entry = api_key_entry()
    assert await setup_entry(hass, entry)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE_URL}devices/{DEVICE_ID}", status=500)
    for _ in range(5):
        await entry.runtime_data.poll_devices()
    await hass.async_block_till_done()
    assert hass.states.get(f"sensor.{DEVICE_ID}_pv_power").state == STATE_UNAVAILABLE
    assert hass.states.get(NUMBER).state == "30.0"


async def test_renaming_the_entity_id_is_supported_in_place(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_api(aioclient_mock)
    entry = api_key_entry()
    assert await setup_entry(hass, entry)
    number = next(
        entity for entity in hass.data["number"].entities if entity.entity_id == NUMBER
    )
    number.async_entity_id_changed("number.old_id")
    for sensor in entry.runtime_data.devices[DEVICE_ID].all_entities():
        sensor.async_entity_id_changed("sensor.old_id")
