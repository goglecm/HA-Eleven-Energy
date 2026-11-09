"""The Eleven Energy Plus integration.

This is the integration's lifecycle entry point. It is responsible for:

* Declaring that Eleven Energy Plus is configured via the UI only (no YAML).
* Registering, once per Home Assistant start, the account sign-in (OAuth)
  implementation and the ``eleven_energy_plus.set_work_mode_*`` actions
  (see :mod:`.services`). The actions are never removed on unload, so a
  reload cannot leave the integration without them.
* Setting up a :class:`Controller` per config entry - with either the stored
  API key or an ``OAuth2Session`` for an account sign-in - storing it on
  ``entry.runtime_data``, running discovery, migrating pre-2.0 registry
  entries, and forwarding setup to the sensor / binary sensor / number
  platforms.
* Unwinding all of the above cleanly on unload, stopping the background
  poller before tearing down the entities it might write to.
* Reacting to option updates: a device-label override change reloads the
  entry, anything else (the poll interval) is applied live. Credential
  changes are reloaded by the reauthentication / reconfigure flows
  themselves, and an account sign-in refreshing its access token changes
  nothing at all.

Only one Eleven Energy Plus entry is supported per Home Assistant instance
(``single_config_entry`` in ``manifest.json``).

The integration is deliberately *namespaced* under
``custom_components/eleven_energy_plus/`` (rather than ``eleven_energy``) so
it can be installed alongside the original ``iPeel/HA-Eleven-Energy``
integration without colliding.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_entry_oauth2_flow, config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import CONF_AUTH_IMPLEMENTATION, CONF_TOKEN, DOMAIN, PLATFORMS
from .controller import Controller
from .migration import async_migrate_entity_registry
from .oauth import async_register_implementation
from .services import SERVICE_NAMES, async_setup_services

_LOGGER = logging.getLogger(__name__)

type ElevenEnergyConfigEntry = ConfigEntry[Controller]

# This integration is configured exclusively through the UI config flow; YAML
# configuration is not supported. Declare that explicitly so Home Assistant
# does not emit a deprecation warning at startup.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

__all__ = ["SERVICE_NAMES", "ElevenEnergyConfigEntry"]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the sign-in implementation and the work-mode actions.

    Home Assistant calls this once per start. The config flow registers the
    sign-in implementation as well, because with no config entry yet this
    may not have run when somebody first adds the integration.
    """
    async_register_implementation(hass)
    async_setup_services(hass)
    _LOGGER.debug("Registered Eleven Energy Plus actions")
    return True


async def _async_create_controller(
    hass: HomeAssistant, entry: ElevenEnergyConfigEntry
) -> Controller:
    """Build the controller for whichever kind of credential the entry holds."""
    if CONF_AUTH_IMPLEMENTATION in entry.data:
        try:
            implementation = (
                await config_entry_oauth2_flow.async_get_config_entry_implementation(
                    hass, entry
                )
            )
        except (ConfigEntryAuthFailed, ConfigEntryNotReady):
            raise
        except ValueError as err:
            raise ConfigEntryAuthFailed(
                "The Eleven Energy account sign-in is no longer available; "
                "sign in again"
            ) from err
        session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
        return Controller(None, hass, entry, session=session)

    token = entry.data.get(CONF_TOKEN)
    if not isinstance(token, str) or not token.strip():
        raise ConfigEntryAuthFailed("No Eleven Energy API token is stored")
    return Controller(token.strip(), hass, entry)


async def async_setup_entry(hass: HomeAssistant, entry: ElevenEnergyConfigEntry) -> bool:
    """Set up Eleven Energy Plus from a config entry.

    1. Build a :class:`Controller` for the stored credential and park it on
       ``entry.runtime_data`` so platforms, actions and diagnostics find it.
    2. ``controller.initialise()`` discovers the inverters and reads each
       once. It raises ``ConfigEntryAuthFailed`` (Home Assistant starts
       reauthentication) or ``ConfigEntryNotReady`` (Home Assistant retries).
    3. Migrate pre-2.0 positional entity registry entries.
    4. Forward setup to every platform in :data:`PLATFORMS`. The poller is
       started by the last platform to call
       :meth:`Controller.complete_platform_setup`.
    5. Subscribe to entry updates.

    Any exception during steps 2-4 stops the in-flight controller so a retry
    starts from a clean slate.
    """
    controller = await _async_create_controller(hass, entry)
    entry.runtime_data = controller

    _LOGGER.info("Eleven Energy Plus starting up")
    try:
        await controller.initialise()
        async_migrate_entity_registry(hass, entry, controller)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await controller.terminate()
        raise

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: ElevenEnergyConfigEntry
) -> bool:
    """Unload a config entry.

    The background poller is stopped *before* the platforms are unloaded, so
    an in-flight poll cannot write state on entities that are being removed.
    The actions stay registered; with no loaded entry they report that the
    integration is not loaded.
    """
    controller: Controller | None = getattr(entry, "runtime_data", None)
    if controller is not None:
        await controller.terminate()

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(
    hass: HomeAssistant, entry: ElevenEnergyConfigEntry
) -> None:
    """React to entry updates (see :meth:`Controller.async_entry_updated`)."""
    controller: Controller | None = getattr(entry, "runtime_data", None)
    if controller is None:
        return
    if controller.async_entry_updated():
        hass.config_entries.async_schedule_reload(entry.entry_id)
