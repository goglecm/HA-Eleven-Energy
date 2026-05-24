"""Eleven Energy Plus ``number`` platform.

Currently exposes a single, per-inverter "Update Interval" Number entity
(:class:`PollIntervalNumber`) so the user can change how often the
integration polls the API without leaving the dashboard.

The Number entity is a *config* entity (``EntityCategory.CONFIG``): writing
to it persists the new value on the config entry's options (which then fans
out to the OptionsFlow + the controller via the standard
:meth:`ConfigEntry.add_update_listener` plumbing in ``__init__.py``), and
reading from it returns the controller's currently-effective interval. The
entity also subscribes to controller-side option changes so the displayed
value stays in sync when the user edits the cadence in the OptionsFlow.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import generate_entity_id
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_POLL_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL_SECONDS,
    MIN_POLL_INTERVAL_SECONDS,
    POLL_INTERVAL_STEP_SECONDS,
)

if TYPE_CHECKING:
    from .controller import Controller
    from .hybrid_inverter import HybridInverter

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Register one poll-interval Number entity per discovered inverter.

    Unlike the sensor and binary_sensor platforms, the Number platform does
    not support dynamic discovery: inverters added at runtime (after the
    initial site poll) will only get a poll-interval entity on the next HA
    restart. That is an intentional simplification - the cadence is a
    site-wide setting, so having one Number per inverter is already a
    convenience rather than a strict requirement.
    """
    controller = hass.data[DOMAIN]["controller"]

    entities: list[PollIntervalNumber] = [
        PollIntervalNumber(hass, entry, inverter, controller)
        for inverter in controller.devices.values()
    ]
    if entities:
        async_add_entities(entities)

    controller.complete_platform_setup("number", async_add_entities)


class PollIntervalNumber(NumberEntity):
    """User-settable poll-interval entity for an inverter.

    Bounds, step and unit are pulled from
    :mod:`custom_components.eleven_energy_plus.const` so the Number entity, the
    OptionsFlow slider and the controller all enforce the same range
    (5-300 seconds in 5-second steps).
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_native_min_value = float(MIN_POLL_INTERVAL_SECONDS)
    _attr_native_max_value = float(MAX_POLL_INTERVAL_SECONDS)
    _attr_native_step = float(POLL_INTERVAL_STEP_SECONDS)
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:timer-cog-outline"
    _attr_translation_key = "poll_interval"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        inverter: HybridInverter,
        controller: Controller,
    ) -> None:
        """Build the entity bound to the controller's poll interval option."""
        self.hass = hass
        self._entry = entry
        self._controller = controller
        self._attr_device_info = inverter.device_info
        self._attr_unique_id = f"{inverter.device_id}_poll_interval"
        self.entity_id = generate_entity_id(
            "number.{}",
            f"{inverter.device_id}_poll_interval",
            [],
            hass,
        )

    @property
    def native_value(self) -> float:
        """Return the current poll interval from the controller."""
        return float(self._controller.poll_interval)

    async def async_set_native_value(self, value: float) -> None:
        """Persist a new poll interval in the config entry options."""
        new_value = int(value)
        new_value = max(
            MIN_POLL_INTERVAL_SECONDS, min(MAX_POLL_INTERVAL_SECONDS, new_value)
        )

        if new_value == self._entry.options.get(CONF_POLL_INTERVAL):
            return

        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, CONF_POLL_INTERVAL: new_value},
        )
        # Write our new value straight away so the UI feels responsive; the
        # async update listener will also fan out via the controller shortly.
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Subscribe to controller-side option changes so the state stays fresh."""
        await super().async_added_to_hass()

        @callback
        def _refresh() -> None:
            self.async_write_ha_state()

        self.async_on_remove(self._controller.add_option_listener(_refresh))
