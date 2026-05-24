"""Eleven Energy Plus ``sensor`` platform.

This module is a thin shim that wires Home Assistant's ``sensor`` platform up
to the central :class:`~custom_components.eleven_energy_plus.controller.Controller`.
The interesting logic - both predefined and auto-discovered entities - lives
in :mod:`custom_components.eleven_energy_plus.hybrid_inverter`.

Two-stage entity registration:

1. Predefined sensors that already exist on the inverter (PV power, battery
   state of charge, etc.) are handed to ``async_add_entities`` immediately,
   so the user sees curated entities on first load.
2. The controller's ``async_add_entities`` callback is then stashed via
   :meth:`Controller.complete_platform_setup`, allowing the dynamic
   discovery in :meth:`HybridInverter._handle_leaf` to register new sensors
   on the fly as the API surfaces additional fields.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Register the curated sensors and arm dynamic discovery."""
    controller = hass.data[DOMAIN]["controller"]

    entities: list = []
    for inverter in controller.devices.values():
        entities.extend(inverter.sensor_entities.values())
    if entities:
        async_add_entities(entities)

    controller.complete_platform_setup("sensor", async_add_entities)
