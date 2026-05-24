"""The Eleven Energy Plus integration.

This is the community fork's lifecycle entry point. It is responsible for:

* Declaring that Eleven Energy Plus is configured via the UI only (no YAML).
* Registering the ``eleven_energy_plus.set_work_mode_*`` services exactly
  once, with ``supports_response=OPTIONAL`` so automations can branch on
  the result.
* Setting up a :class:`Controller` per config entry, running the initial site
  poll, forwarding setup to the sensor / binary sensor / number platforms,
  and subscribing to OptionsFlow updates.
* Unwinding all of the above cleanly on unload, including stopping the
  background poller before tearing down the entities it might write to.
* Handling option/data updates - either reloading on a token change or
  poking the controller via :meth:`Controller.notify_options_changed`.

The implementation deliberately keeps state minimal: every config entry's
``Controller`` is stored at ``hass.data[DOMAIN]["controller"]``. Today only
one Eleven Energy Plus site is supported per HA instance (the config flow
enforces that), so a flat dict suffices.

The integration is deliberately *namespaced* under
``custom_components/eleven_energy_plus/`` (rather than ``eleven_energy``) so
HACS can install it alongside the original, unmaintained ``iPeel`` HACS
repository without colliding.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import CONF_DEVICE_LABEL, DOMAIN, PLATFORMS
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
                "No Eleven Energy Plus controller available to handle service %s",
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

    _LOGGER.debug("Registered Eleven Energy Plus services")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Eleven Energy Plus from a config entry.

    Lifecycle:

    1. Construct a :class:`Controller` bound to the stored token + entry.
    2. Park it in ``hass.data[DOMAIN]["controller"]`` so platforms and
       services can find it.
    3. ``controller.initialise()`` runs the initial site poll; it raises
       :class:`~homeassistant.exceptions.ConfigEntryNotReady` on transient
       failures so Home Assistant's standard retry/backoff kicks in.
    4. Forward setup to every platform in :data:`PLATFORMS`. The poller is
       started by the last platform that calls
       :meth:`Controller.complete_platform_setup`, guaranteeing that all
       ``async_add_entities`` callbacks are wired up before any data lands.
    5. Subscribe to options/data updates so token edits trigger a reload and
       cadence edits propagate without one.

    Any exception during steps 3-4 rolls back the in-flight controller so a
    repeat setup (after the user fixes the issue) starts from a clean slate.
    """

    hass.data.setdefault(DOMAIN, {})

    controller = Controller(entry.data["token"], hass, entry)
    hass.data[DOMAIN]["controller"] = controller

    _LOGGER.info("Eleven Energy Plus starting up")
    try:
        await controller.initialise()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        # Any failure during initialise/forward must roll back the in-flight
        # controller so a subsequent retry (or repeat config flow) starts
        # from a clean slate.
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


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """React to options or data updates from the OptionsFlow.

    Three paths:

    * **Token changed** - the existing controller is bound to the old token
      and cannot recover. Trigger a full integration reload so a fresh
      controller is created against the new credentials.
    * **Device-label override changed** - the override is consumed when
      :class:`~.hybrid_inverter.HybridInverter` is constructed and its
      ``DeviceInfo`` is registered with Home Assistant. Mutating an
      already-registered device's name/model cleanly requires re-running
      that registration, so we trigger a full reload here too. This is
      rare (users typically set the override once and forget it).
    * **Options-only change** (e.g. poll interval). Wake the poller via
      :meth:`Controller.notify_options_changed` so the new cadence takes
      effect on the next iteration without dropping any already-loaded
      entity state.
    """
    controller: Controller | None = hass.data.get(DOMAIN, {}).get("controller")
    if controller is None:
        return

    if controller.token != entry.data.get("token"):
        await hass.config_entries.async_reload(entry.entry_id)
        return

    new_override = entry.options.get(CONF_DEVICE_LABEL, "")
    if isinstance(new_override, str):
        new_override = new_override.strip()
    else:
        new_override = ""
    if new_override != controller.configured_device_label:
        await hass.config_entries.async_reload(entry.entry_id)
        return

    controller.notify_options_changed()
