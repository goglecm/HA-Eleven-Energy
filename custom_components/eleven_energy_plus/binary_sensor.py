"""Eleven Energy Plus ``binary_sensor`` platform.

Mirror of :mod:`custom_components.eleven_energy_plus.sensor` for boolean values.
Predefined binary sensors (currently just the ``Online`` connectivity entity)
are registered up front; the controller's add-entities callback is stashed so
auto-discovered binary fields (anything :func:`_infer_meta` identifies as a
``bool``) can be added on the fly during polling.
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
    """Register curated binary sensors and arm dynamic discovery."""
    controller = hass.data[DOMAIN]["controller"]

    entities: list = []
    for inverter in controller.devices.values():
        entities.extend(inverter.binary_sensor_entities.values())
    if entities:
        async_add_entities(entities)

    controller.complete_platform_setup("binary_sensor", async_add_entities)
