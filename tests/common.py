"""Shared payload builders and helpers for the Eleven Energy Plus tests."""

from __future__ import annotations

import copy
from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.eleven_energy_plus.const import (
    BASE_URL,
    CONF_POLL_INTERVAL,
    DOMAIN,
)

DEVICE_ID = "abc"

# A single-phase, single-battery machine as the API describes it: the device
# ``name`` is the fleet-wide provisioning default, the ``model`` block carries
# the real product name.
MODEL_SINGLE_PHASE: dict[str, Any] = {
    "name": "Mediterranean Sea 12",
    "threePhase": False,
    "dualBattery": False,
    "maxAcPower": 12.0,
    "maxDcPower": 10.0,
}

MODEL_THREE_PHASE_DUAL: dict[str, Any] = {
    "name": "Atlantic 20",
    "threePhase": True,
    "dualBattery": True,
    "maxAcPower": 20.0,
    "maxDcPower": 15.0,
}


def site_summary(
    device_id: str = DEVICE_ID,
    *,
    name: Any = "North Sea 6 (6kW)",
    model: Any = None,
    serial: Any = "SN-A",
    device_type: str = "hybridinverter",
) -> dict[str, Any]:
    """Return one entry of the ``/site`` ``devices`` list."""
    summary: dict[str, Any] = {
        "deviceId": device_id,
        "type": device_type,
        "name": name,
        "serialNumber": serial,
    }
    if model is not None:
        summary["model"] = copy.deepcopy(model)
    return summary


def site_payload(*summaries: dict[str, Any]) -> dict[str, Any]:
    """Return a ``/site`` payload listing ``summaries``."""
    return {"devices": [copy.deepcopy(summary) for summary in summaries]}


def device_payload(
    device_id: str = DEVICE_ID,
    *,
    model: dict[str, Any] | None = None,
    batteries: list[dict[str, Any]] | None = None,
    mppts: list[dict[str, Any]] | None = None,
    phases: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Return a realistic ``/devices/<id>`` payload."""
    payload: dict[str, Any] = {
        "deviceId": device_id,
        "type": "hybridinverter",
        "name": "North Sea 6 (6kW)",
        "serialNumber": "SN-A",
        "online": True,
        "status": "OnGrid",
        "firmwareVersion": "1.2.3",
        "pv": {"power": 1.5, "energyToday": 3.2},
        "battery": {"stateOfCharge": 80, "power": -0.5},
        "grid": {"power": 0.2},
        "load": {"power": 1.2},
        "operatingMode": {"workMode": "selfConsumption"},
    }
    if model is not None:
        payload["model"] = copy.deepcopy(model)
    if batteries is not None:
        payload["battery"]["batteries"] = copy.deepcopy(batteries)
    if mppts is not None:
        payload["pv"]["mppts"] = copy.deepcopy(mppts)
    if phases is not None:
        payload["grid"]["phases"] = copy.deepcopy(phases)
    payload.update(extra)
    return payload


def mock_api(
    aioclient_mock: AiohttpClientMocker,
    *,
    devices: dict[str, dict[str, Any]] | None = None,
    summaries: list[dict[str, Any]] | None = None,
) -> None:
    """Register ``/site`` and ``/devices/<id>`` mocks for the given devices."""
    if devices is None:
        devices = {DEVICE_ID: device_payload(DEVICE_ID)}
    if summaries is None:
        summaries = [site_summary(device_id) for device_id in devices]
    aioclient_mock.get(f"{BASE_URL}site", json=site_payload(*summaries))
    for device_id, payload in devices.items():
        aioclient_mock.get(f"{BASE_URL}devices/{device_id}", json=payload)


def api_key_entry(**kwargs: Any) -> MockConfigEntry:
    """Return an API-key config entry."""
    options = kwargs.pop("options", {CONF_POLL_INTERVAL: 30})
    return MockConfigEntry(
        domain=DOMAIN,
        data=kwargs.pop("data", {"token": "test-token"}),
        options=options,
        title="Eleven Energy Plus",
        **kwargs,
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> bool:
    """Add ``entry`` to hass and set it up."""
    entry.add_to_hass(hass)
    result = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return result


def device_field(info: Any, key: str) -> Any:
    """Read a ``DeviceInfo`` field on any supported Home Assistant version.

    ``DeviceInfo`` is a ``TypedDict`` before 2026.11 and a dataclass from
    2026.11 on (where dict access is only a compatibility layer).
    """
    if isinstance(info, dict):
        return info[key]
    return getattr(info, key)
