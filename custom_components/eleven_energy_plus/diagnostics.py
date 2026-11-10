"""Diagnostics support for the Eleven Energy Plus integration.

Home Assistant auto-discovers this module by name. When a user clicks
*Download Diagnostics* on the integration card, HA calls
:func:`async_get_config_entry_diagnostics` and serves the returned dict as a
JSON file. The dict is intentionally a *snapshot of state we already have in
memory*: the most recent raw site and per-device API payloads, the entry's
options, the integration version, what the controller knows about each
inverter (model, phases, trackers, batteries, availability, ignored list
items) and the repair issues currently raised.

This is the recommended path for users to share troubleshooting data, since
it:

* doesn't require them to enable debug logging,
* doesn't require them to dig through ``home-assistant.log``, and
* redacts sensitive material (API token, account sign-in tokens, serial
  numbers, identifying ids, location and contact details) before it leaves
  their machine.

The redaction relies on :func:`homeassistant.components.diagnostics.async_redact_data`
which walks the dict recursively and replaces values whose keys match
:data:`TO_REDACT`. New sensitive-looking fields the upstream API may add in
future should be added there; that's a single source of truth.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import CONF_AUTH_IMPLEMENTATION, DOMAIN

if TYPE_CHECKING:
    from . import ElevenEnergyConfigEntry
    from .controller import Controller

_LOGGER = logging.getLogger(__name__)

# Keys whose values are always stripped from the diagnostics dump
# (``async_redact_data`` matches keys exactly). ``token`` covers both the API
# key and the whole OAuth token structure stored on the entry; the OAuth
# fields are listed too in case they ever appear elsewhere. The identifiers
# are not needed to debug a labelling issue, and a cautious user would rather
# not post them, their address or their contact details in a public bug
# report.
TO_REDACT: frozenset[str] = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "serialNumber",
        "serial_number",
        "deviceId",
        "device_id",
        "address",
        "postcode",
        "postCode",
        "latitude",
        "longitude",
        "email",
        "phone",
        # Network identifiers of the inverter or its connection.
        "ip",
        "ipAddress",
        "mac",
        "macAddress",
        "ssid",
        "wifiSsid",
        "imei",
        "iccid",
        # UK electricity and gas meter numbers, tied to the address.
        "mpan",
        "mprn",
    }
)


async def _async_integration_version(hass: HomeAssistant) -> str:
    """Return the integration version from Home Assistant's loader.

    The loader has already read ``manifest.json``, so this costs no file I/O
    inside the event loop (which Home Assistant reports as a blocking call).
    A failure is logged and the version reported as ``unknown``, so the rest
    of the diagnostics still download.
    """
    try:
        integration = await async_get_integration(hass, DOMAIN)
    except Exception:
        _LOGGER.warning(
            "Eleven Energy Plus could not read its own version for diagnostics",
            exc_info=True,
        )
        return "unknown"
    version = integration.version
    return str(version) if version else "unknown"


def _controller_snapshot(controller: Controller) -> dict[str, Any]:
    """Describe the live controller state."""
    return {
        "loaded": not controller.terminated,
        "credential": (
            "account sign-in" if controller.uses_account_sign_in else "API token"
        ),
        "auth_failed": controller.auth_failed,
        "poll_interval_seconds": controller.poll_interval,
        "device_label_override": controller.device_label_override,
        "issues": controller.faults.active_issues,
        "optimiser_off_since": [
            {"deviceId": device_id, "since": since}
            for device_id, since in controller.faults.optimiser_off_since.items()
        ],
        # Device ids are wrapped in dicts keyed by ``deviceId`` so the
        # redactor (which matches keys, not values) rewrites them.
        "known_devices": [
            {
                "deviceId": device_id,
                "label": inverter.device_label,
                "model": inverter.model,
                "three_phase": inverter.three_phase,
                "mppt_count": inverter.mppt_count,
                "battery_indices": inverter.battery_indices,
                "available": inverter.available,
                "missing": inverter.missing,
                "offline": inverter.offline,
                "api_connected": inverter.api_connected,
                "last_successful_poll": (
                    inverter.last_success_at.isoformat()
                    if inverter.last_success_at is not None
                    else None
                ),
                "ignored_list_items": inverter.ignored_list_items,
                "entity_count": len(inverter.all_entities()),
            }
            for device_id, inverter in controller.devices.items()
        ],
        "registered_platforms": controller.registered_platforms,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ElevenEnergyConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for an Eleven Energy Plus config entry.

    Shape::

        {
            "integration": {"version": "2.0.1", "domain": "eleven_energy_plus"},
            "entry": {"title": ..., "data": {...redacted...}, "options": {...}},
            "controller": {"loaded": True, "credential": "API token", ...},
            "raw_payloads": {
                "site": {...redacted site payload...},
                "devices": [{"deviceId": "**REDACTED**", "payload": {...}}],
            },
        }

    When no controller is loaded, ``controller`` is ``{"loaded": False}`` and
    ``raw_payloads`` is ``{"site": None, "devices": []}``, so consumers can
    tell "not yet polled" from "polled with no devices". A controller that
    has stopped but is still attached (after a failed unload or setup) is
    described as usual, with ``loaded`` False.
    """
    data = dict(entry.data)
    diagnostics: dict[str, Any] = {
        "integration": {
            "version": await _async_integration_version(hass),
            "domain": DOMAIN,
        },
        "entry": {
            "title": entry.title,
            "credential": (
                "account sign-in" if CONF_AUTH_IMPLEMENTATION in data else "API token"
            ),
            "data": data,
            "options": dict(entry.options),
        },
        "controller": {"loaded": False},
        "raw_payloads": {"site": None, "devices": []},
    }

    controller: Controller | None = getattr(entry, "runtime_data", None)
    if controller is not None:
        diagnostics["controller"] = _controller_snapshot(controller)
        # Keying the dict by raw device id would leak the id through the key,
        # which ``async_redact_data`` cannot rewrite, so it is lifted into the
        # value as ``deviceId``.
        diagnostics["raw_payloads"] = {
            "site": controller.last_site_payload,
            "devices": [
                {"deviceId": device_id, "payload": payload}
                for device_id, payload in controller.last_device_payloads.items()
            ],
        }

    return async_redact_data(diagnostics, TO_REDACT)
