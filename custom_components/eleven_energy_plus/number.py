"""Eleven Energy Plus ``number`` platform.

Currently exposes a single, per-inverter "Update Interval" Number entity
(:class:`PollIntervalNumber`) so the user can change how often the
integration polls the API without leaving the dashboard.

The Number entity is a *config* entity (``EntityCategory.CONFIG``): writing
to it persists the new value on the config entry's options (which then fans
out to the controller via the update listener in ``__init__.py``), and
reading from it returns the controller's currently-effective interval. The
entity also subscribes to controller-side option changes so the displayed
value stays in sync when the user edits the cadence in the OptionsFlow.

Unlike the telemetry entities it stays available while the API is
unreachable: it reflects local configuration, and lengthening the interval
is a reasonable thing to want to do during an outage. It goes unavailable
only once the integration has shut down.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import generate_entity_id
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_POLL_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL_SECONDS,
    MIN_POLL_INTERVAL_SECONDS,
    POLL_INTERVAL_STEP_SECONDS,
)
from .parsing import parse_whole_number

if TYPE_CHECKING:
    from . import ElevenEnergyConfigEntry
    from .controller import Controller
    from .hybrid_inverter import HybridInverter

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ElevenEnergyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Register one poll-interval Number entity per inverter.

    The factory handed to the controller builds the entity for an inverter
    found later, at runtime, so it gets one without a restart.
    """
    controller = entry.runtime_data

    def _entities_for(inverter: HybridInverter) -> list[Any]:
        return [PollIntervalNumber(hass, entry, inverter, controller)]

    entities: list[Any] = []
    for inverter in controller.devices.values():
        entities.extend(_entities_for(inverter))
    async_add_entities(entities)

    controller.complete_platform_setup("number", async_add_entities, _entities_for)


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
        entry: ElevenEnergyConfigEntry,
        inverter: HybridInverter,
        controller: Controller,
    ) -> None:
        """Build the entity bound to the controller's poll interval option."""
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
    def available(self) -> bool:
        """Available until the integration shuts down."""
        return not self._controller.terminated

    @property
    def native_value(self) -> float:
        """Return the current poll interval from the controller."""
        return float(self._controller.poll_interval)

    async def async_set_native_value(self, value: float) -> None:
        """Persist a new poll interval in the config entry options.

        Home Assistant checks the range but lets NaN through (every
        comparison with NaN is false), and does not enforce whole seconds,
        so the value is checked here and refused rather than converted.
        """
        seconds = parse_whole_number(value)
        if seconds is None or not (
            MIN_POLL_INTERVAL_SECONDS <= seconds <= MAX_POLL_INTERVAL_SECONDS
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="poll_interval_invalid",
                translation_placeholders={
                    "value": str(value),
                    "min": str(MIN_POLL_INTERVAL_SECONDS),
                    "max": str(MAX_POLL_INTERVAL_SECONDS),
                },
            )

        if seconds == self._entry.options.get(CONF_POLL_INTERVAL):
            return

        self.hass.config_entries.async_update_entry(
            self._entry,
            options={**self._entry.options, CONF_POLL_INTERVAL: seconds},
        )
        # Write our new value straight away so the UI feels responsive; the
        # update listener will also fan out via the controller shortly.
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Subscribe to controller-side option changes so the state stays fresh."""
        await super().async_added_to_hass()

        @callback
        def _refresh() -> None:
            self.async_write_ha_state()

        self.async_on_remove(self._controller.add_option_listener(_refresh))

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Opt in to in-place ``entity_id`` changes (Home Assistant 2026.11+).

        Nothing this entity sets up is keyed on its own ``entity_id``, so a
        rename needs no work; the base implementation is a no-op too.
        Defining the method is what opts in. Older Home Assistant versions
        never call it.
        """
