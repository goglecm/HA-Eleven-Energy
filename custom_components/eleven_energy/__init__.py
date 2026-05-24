"""The Eleven Energy integration.

This module is the integration's lifecycle entry point. It is responsible for:

* Declaring that Eleven Energy is configured via the UI only (no YAML).
* Registering the ``eleven_energy.set_work_mode_*`` services exactly once,
  with ``supports_response=OPTIONAL`` so automations can branch on the result.
* Setting up a :class:`Controller` per config entry, running the initial site
  poll, migrating legacy entities, forwarding setup to the sensor / binary
  sensor / number platforms, and subscribing to OptionsFlow updates.
* Unwinding all of the above cleanly on unload, including stopping the
  background poller before tearing down the entities it might write to.
* Handling option/data updates - either reloading on a token change or
  poking the controller via :meth:`Controller.notify_options_changed`.

The implementation deliberately keeps state minimal: every config entry's
``Controller`` is stored at ``hass.data[DOMAIN]["controller"]``. Today only
one Eleven Energy site is supported per HA instance (the config flow enforces
that), so a flat dict suffices.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, PLATFORMS
from .controller import Controller

_LOGGER = logging.getLogger(__name__)

# This integration is configured exclusively through the UI config flow; YAML
# configuration is not supported. Declare that explicitly so Home Assistant
# does not emit a deprecation warning at startup.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# The complete list of service names registered against the ``eleven_energy``
# domain. Kept in one place so ``async_setup`` (register) and
# ``async_unload_entry`` (deregister) cannot drift out of sync, and so any new
# work mode only needs to be added in one place plus ``controller.py``.
SERVICE_NAMES = (
    "set_work_mode_self_consumption",
    "set_work_mode_force_charge",
    "set_work_mode_grid_export",
    "set_work_mode_idle_battery",
    "set_work_mode_pv_export",
    "set_work_mode_target_soc",
    "set_work_mode_reset",
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the Eleven Energy services.

    Home Assistant calls this once per HA boot, regardless of how many config
    entries exist. The actual API plumbing for each entry happens in
    :func:`async_setup_entry`; here we only register the service handlers so
    that the services are visible in the UI even before the first entry has
    finished loading.
    """

    hass.data.setdefault(DOMAIN, {})

    async def handle_set_workmode(call: ServiceCall) -> dict[str, Any]:
        """Forward a work-mode service call to the active controller.

        Always returns a response dict; never raises. Automations branch on
        ``result.success`` / ``result.error`` / ``result.status``. The full
        response schema is documented on :meth:`Controller.set_work_mode`.
        """
        controller: Controller | None = hass.data.get(DOMAIN, {}).get("controller")
        if controller is None:
            # Service was called before any config entry was loaded (or after
            # unload). Surface the same shape as a normal failure so
            # automations don't have to special-case "no controller".
            _LOGGER.warning(
                "No Eleven Energy controller available to handle service %s",
                call.service,
            )
            return {
                "success": False,
                "status": None,
                "device_id": None,
                "work_mode": None,
                "params": {},
                "attempts": 0,
                "error": "integration not loaded",
            }
        return await controller.set_work_mode(call.service, call.data)

    for service_name in SERVICE_NAMES:
        # Re-registering is harmless but noisy; ``has_service`` keeps the logs
        # clean when HA reloads the integration during development.
        if not hass.services.has_service(DOMAIN, service_name):
            hass.services.async_register(
                DOMAIN,
                service_name,
                handle_set_workmode,
                supports_response=SupportsResponse.OPTIONAL,
            )

    _LOGGER.debug("Registered Eleven Energy services")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Eleven Energy from a config entry.

    Lifecycle:

    1. Construct a :class:`Controller` bound to the stored token + entry.
    2. Park it in ``hass.data[DOMAIN]["controller"]`` so platforms and
       services can find it.
    3. ``controller.initialise()`` runs the initial site poll; it raises
       :class:`~homeassistant.exceptions.ConfigEntryNotReady` on transient
       failures so Home Assistant's standard retry/backoff kicks in.
    4. Migrate any legacy entities created by older versions.
    5. Forward setup to every platform in :data:`PLATFORMS`. The poller is
       started by the last platform that calls
       :meth:`Controller.complete_platform_setup`, guaranteeing that all
       ``async_add_entities`` callbacks are wired up before any data lands.
    6. Subscribe to options/data updates so token edits trigger a reload and
       cadence edits propagate without one.

    Any exception during steps 3-5 rolls back the in-flight controller so a
    repeat setup (after the user fixes the issue) starts from a clean slate.
    """

    hass.data.setdefault(DOMAIN, {})

    controller = Controller(entry.data["token"], hass, entry)
    hass.data[DOMAIN]["controller"] = controller

    _LOGGER.info("Eleven Energy starting up")
    try:
        await controller.initialise()
        _async_migrate_legacy_entities(hass, controller)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        # Any failure during initialise/migrate/forward must roll back the
        # in-flight controller so a subsequent retry (or repeat config flow)
        # starts from a clean slate.
        await controller.terminate()
        hass.data.get(DOMAIN, {}).pop("controller", None)
        raise

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry.

    Termination order matters: the background poller must be stopped *before*
    the platforms are unloaded, otherwise an in-flight poll could try to write
    state on entities that have already been removed from the registry and
    spam ``RuntimeError: Attribute hass is None`` into the log.

    Services are only deregistered when the *last* config entry for the
    integration goes away, which today is identical to "any entry unloads"
    since the config flow enforces a single entry.
    """

    # Stop polling FIRST so the background task can't try to write state on
    # entities that are about to be removed by async_unload_platforms below.
    controller: Controller | None = hass.data.get(DOMAIN, {}).get("controller")
    if controller is not None:
        await controller.terminate()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data.get(DOMAIN, {}).pop("controller", None)

        if not hass.data.get(DOMAIN):
            for service_name in SERVICE_NAMES:
                if hass.services.has_service(DOMAIN, service_name):
                    hass.services.async_remove(DOMAIN, service_name)
            hass.data.pop(DOMAIN, None)

    return unload_ok


def _async_migrate_legacy_entities(hass: HomeAssistant, controller: Controller) -> None:
    """Clean up the legacy ``sensor.*_system_online`` entry from before 1.3.

    Releases up to and including 1.2 accidentally registered the connectivity
    entity under the ``sensor`` platform. From 1.3 onwards it is correctly a
    ``binary_sensor`` (``binary_sensor.*_system_online``). The new entity is
    created with the canonical unique id, so the only thing left is to drop
    the stale registry row to avoid the "duplicate entity" warning.
    """
    ent_reg = er.async_get(hass)
    for device_id in controller.devices:
        legacy_unique_id = f"{device_id}_system_online"
        legacy_entity_id = ent_reg.async_get_entity_id(
            "sensor", DOMAIN, legacy_unique_id
        )
        if legacy_entity_id is not None:
            _LOGGER.info(
                "Removing legacy mis-domained entity %s", legacy_entity_id
            )
            ent_reg.async_remove(legacy_entity_id)


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """React to options or data updates from the OptionsFlow.

    Two paths:

    * **Token changed** - the existing controller is bound to the old token
      and cannot recover. Trigger a full integration reload so a fresh
      controller is created against the new credentials.
    * **Token unchanged** - only options moved (typically the poll interval).
      Wake the poller via :meth:`Controller.notify_options_changed` so the
      new cadence takes effect on the next iteration without dropping any
      already-loaded entity state.
    """
    controller: Controller | None = hass.data.get(DOMAIN, {}).get("controller")
    if controller is None:
        return

    if controller.token != entry.data.get("token"):
        await hass.config_entries.async_reload(entry.entry_id)
        return

    controller.notify_options_changed()
