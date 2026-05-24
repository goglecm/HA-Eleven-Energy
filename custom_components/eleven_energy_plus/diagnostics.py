"""Diagnostics support for the Eleven Energy Plus integration.

Home Assistant auto-discovers this module by name. When a user clicks
*Download Diagnostics* on the integration card, HA calls
:func:`async_get_config_entry_diagnostics` and serves the returned dict as a
JSON file. The dict is intentionally a *snapshot of state we already have in
memory*: the most recent raw site and per-device API payloads, the entry's
options, the manifest version, and which devices/services we currently know
about.

This is the recommended path for users to share troubleshooting data, since
it:

* doesn't require them to enable debug logging,
* doesn't require them to dig through ``home-assistant.log``, and
* redacts sensitive material (API token, serial numbers, identifying ids)
  before it leaves their machine.

The redaction relies on :func:`homeassistant.components.diagnostics.async_redact_data`
which walks the dict recursively and replaces values whose keys match
:data:`TO_REDACT`. New sensitive-looking fields the upstream API may add in
future should be added there; that's a single source of truth.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .controller import Controller

# Keys (case-insensitive substring match would be nicer, but
# ``async_redact_data`` does exact key matching) whose values we always strip
# from the diagnostics dump. ``token`` covers the bearer token stored on the
# entry; the other entries cover identifiers we don't strictly need to share
# to debug a labelling issue but which a cautious user might prefer not to
# leak in a public bug report.
TO_REDACT: frozenset[str] = frozenset(
    {
        "token",
        "serialNumber",
        "serial_number",
        "deviceId",
        "device_id",
    }
)


def _read_integration_version() -> str:
    """Best-effort read of the integration version out of ``manifest.json``.

    Diagnostics are most useful when the consumer can see *which* version of
    the integration produced them. We avoid importing the manifest reader
    helpers from Home Assistant core to keep this module trivially testable
    in isolation; the file is small and the read is one-shot.
    """
    manifest_path = Path(__file__).parent / "manifest.json"
    try:
        with manifest_path.open(encoding="utf-8") as fh:
            return json.load(fh).get("version", "unknown")
    except (OSError, ValueError):
        return "unknown"


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for an Eleven Energy Plus config entry.

    Returns a JSON-serialisable dict with the following shape:

    .. code-block:: python

        {
            "integration": {
                "version": "1.5.0",
                "domain": "eleven_energy_plus",
            },
            "entry": {
                "title": "Eleven Energy Plus",
                "data": {"token": "**REDACTED**"},
                "options": {"poll_interval_seconds": 15, "device_label_override": ""},
            },
            "controller": {
                "loaded": True,
                "poll_interval_seconds": 15,
                "device_label_override": "",
                "known_devices": [{"deviceId": "**REDACTED**"}],
                "registered_platforms": ["sensor", "binary_sensor"],
            },
            "raw_payloads": {
                "site": { ...redacted site payload... },
                "devices": [
                    {"deviceId": "**REDACTED**", "payload": { ... }},
                ],
            },
        }

    The controller block reflects the *live* in-memory state - useful for
    sanity-checking that a freshly-saved options change actually reached
    the controller. The ``raw_payloads`` block reflects the last
    *successful* poll for each endpoint, which is what makes this dump
    useful for diagnosing label / field-mapping issues.
    """
    diagnostics: dict[str, Any] = {
        "integration": {
            "version": _read_integration_version(),
            "domain": DOMAIN,
        },
        "entry": {
            "title": entry.title,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "controller": {"loaded": False},
        "raw_payloads": {"site": None, "devices": {}},
    }

    controller: Controller | None = hass.data.get(DOMAIN, {}).get("controller")
    if controller is not None:
        diagnostics["controller"] = {
            "loaded": True,
            "poll_interval_seconds": controller.poll_interval,
            "device_label_override": controller.device_label_override,
            # Wrap each device id in a dict keyed by ``deviceId`` so
            # :func:`async_redact_data` (which redacts by key-match, not by
            # value-match) actually sees and rewrites them. Reviewers can
            # still see the count of devices the integration knows about
            # via ``len(known_devices)``.
            "known_devices": [
                {"deviceId": device_id} for device_id in controller.devices
            ],
            # The controller doesn't track which platforms have registered
            # by *name*, only count - surface what we have so reviewers can
            # spot a half-loaded entry (e.g. sensor up but binary_sensor
            # never finished).
            "registered_platforms": sorted(
                controller._add_entities_callbacks.keys()  # noqa: SLF001
            ),
        }
        # Same trick for per-device payloads: keying the dict by raw
        # ``device_id`` would leak the id through the key, which
        # ``async_redact_data`` cannot rewrite. Lift it into the value as
        # ``deviceId`` so the redactor can find and rewrite it.
        diagnostics["raw_payloads"] = {
            "site": controller._last_site_payload,  # noqa: SLF001
            "devices": [
                {"deviceId": device_id, "payload": payload}
                for device_id, payload in (
                    controller._last_device_payloads  # noqa: SLF001
                ).items()
            ],
        }

    return async_redact_data(diagnostics, TO_REDACT)
