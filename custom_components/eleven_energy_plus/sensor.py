"""Eleven Energy Plus ``sensor`` platform.

This module is a thin shim that wires Home Assistant's ``sensor`` platform up
to the :class:`~custom_components.eleven_energy_plus.controller.Controller`
stored on the config entry. The interesting logic - curated, model-driven
and auto-discovered entities - lives in
:mod:`custom_components.eleven_energy_plus.hybrid_inverter`.

Two-stage entity registration:

1. Every sensor that already exists on the inverters (curated ones, plus
   anything discovered from the reading taken during setup) is handed to
   ``async_add_entities`` immediately.
2. The callback is then stashed via
   :meth:`Controller.complete_platform_setup`, so entities created later
   (newly reported fields, a newly fitted battery, a new inverter) are
   registered on the fly.
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
    """Register the known sensors and arm dynamic discovery."""
    controller = entry.runtime_data

    entities: list[Any] = []
    for inverter in controller.devices.values():
        entities.extend(inverter.sensor_entities.values())
    async_add_entities(entities)

    controller.complete_platform_setup("sensor", async_add_entities)
