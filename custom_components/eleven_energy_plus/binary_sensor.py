"""Eleven Energy Plus ``binary_sensor`` platform.

Mirror of :mod:`custom_components.eleven_energy_plus.sensor` for boolean values.
Known binary sensors (the ``Online`` connectivity entity, plus anything
discovered from the reading taken during setup) are registered up front; the
add-entities callback is stashed so auto-discovered binary fields can be
added on the fly during polling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

if TYPE_CHECKING:
    from . import ElevenEnergyConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ElevenEnergyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Register the known binary sensors and arm dynamic discovery."""
    controller = entry.runtime_data

    entities: list[Any] = []
    for inverter in controller.devices.values():
        entities.extend(inverter.binary_sensor_entities.values())
    async_add_entities(entities)

    controller.complete_platform_setup("binary_sensor", async_add_entities)
