"""Tests for migration.py: moving pre-2.0 registry entries onto curated entities."""

from __future__ import annotations

import pytest

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.eleven_energy_plus.const import DOMAIN
from custom_components.eleven_energy_plus.migration import async_migrate_entity_registry

from .common import (
    MODEL_SINGLE_PHASE,
    api_key_entry,
    device_payload,
    mock_api,
    setup_entry,
    site_summary,
)


def _register(hass: HomeAssistant, entry: ConfigEntry, unique_id: str) -> str:
    return er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, unique_id, config_entry=entry
    ).entity_id


@pytest.mark.covers("FC-MIG-01", "FC-MIG-02", "FC-MIG-03")
async def test_each_inverter_migrates_only_its_own_entities_and_a_rerun_does_nothing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    readings = {
        device_id: device_payload(
            device_id,
            model=MODEL_SINGLE_PHASE,
            mppts=[{"index": 1, "power": 1.0}],
        )
        for device_id in ("a", "b")
    }
    mock_api(
        aioclient_mock,
        devices=readings,
        summaries=[site_summary(device_id, model=MODEL_SINGLE_PHASE) for device_id in readings],
    )
    entry = api_key_entry()
    entry.add_to_hass(hass)
    old_a = _register(hass, entry, "a_pv_mppts_0_power")
    phantom_b = _register(hass, entry, "b_grid_phases_L1_power")
    unrelated = _register(hass, entry, "zzz_pv_mppts_0_power")

    assert await setup_entry(hass, entry)
    registry = er.async_get(hass)
    assert registry.async_get(old_a).unique_id == "a_mppt_1_power", "history kept"
    assert registry.async_get(phantom_b) is None, "not on single-phase hardware"
    assert registry.async_get(unrelated) is not None, "another device's entity is left alone"

    assert async_migrate_entity_registry(hass, entry, entry.runtime_data) == (0, 0)
