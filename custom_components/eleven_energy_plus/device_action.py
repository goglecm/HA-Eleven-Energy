"""Device actions for Eleven Energy Plus inverters.

These appear where an automation editor offers a device's actions: pick the
inverter, then choose a work mode, rather than knowing an action exists and
then pointing it at a machine. Recent Home Assistant automation editors no
longer ask integrations for device actions, so the README presents the
``eleven_energy_plus.set_work_mode_*`` actions first; device actions remain
for the websocket API, older editors and YAML.

Each one delegates to the matching action with the inverter as its target,
so there is exactly one implementation of what a work mode means. Field
validation is shared with the action schemas (see :mod:`.services`), so a
device action accepts precisely what calling the action directly accepts,
and the configuration is checked when the automation is loaded: a missing
required field, an out-of-range value or a field the work mode does not use
is reported up front instead of when the automation runs.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_TYPE
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    selector,
)
from homeassistant.helpers.typing import ConfigType, TemplateVarsType

from .const import DOMAIN, MAX_TARGET_MINUTES, MIN_TARGET_MINUTES, UI_MAX_POWER_KW
from .services import FIELD_VALIDATORS, SERVICE_FIELDS

_LOGGER = logging.getLogger(__name__)

# A list rather than a set: this is the order they are offered in. The
# everyday modes first, handing control back last.
ACTION_TYPES: list[str] = [
    "self_consumption",
    "force_charge",
    "grid_export",
    "pv_export",
    "idle_battery",
    "target_soc",
    "reset",
]

_SERVICE_FOR_TYPE: dict[str, str] = {
    action_type: f"set_work_mode_{action_type}" for action_type in ACTION_TYPES
}

_ALL_FIELDS: tuple[str, ...] = tuple(
    sorted({field for fields in SERVICE_FIELDS.values() for field in fields})
)

ACTION_SCHEMA = cv.DEVICE_ACTION_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In(ACTION_TYPES),
        **{vol.Optional(field): FIELD_VALIDATORS[field] for field in _ALL_FIELDS},
    }
)


def _percent_selector() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0,
            max=100,
            step=1,
            unit_of_measurement="%",
            mode=selector.NumberSelectorMode.SLIDER,
        )
    )


def _power_selector() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0,
            max=UI_MAX_POWER_KW,
            step=0.1,
            unit_of_measurement="kW",
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _minutes_selector() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=MIN_TARGET_MINUTES,
            max=MAX_TARGET_MINUTES,
            step=1,
            unit_of_measurement="min",
            mode=selector.NumberSelectorMode.BOX,
        )
    )


_FIELD_SELECTORS: dict[str, Any] = {
    "percent_to_battery": _percent_selector,
    "target_power": _power_selector,
    "target_percent": _percent_selector,
    "target_soc": _percent_selector,
    "target_minutes": _minutes_selector,
    "max_charge_power": _power_selector,
    "max_discharge_power": _power_selector,
    "allow_charging": selector.BooleanSelector,
    "allow_discharging": selector.BooleanSelector,
    "include_excess_solar": selector.BooleanSelector,
    "overdrive": selector.BooleanSelector,
}


async def async_validate_action_config(
    hass: HomeAssistant, config: ConfigType
) -> ConfigType:
    """Validate a device action, including which fields its work mode uses."""
    config = ACTION_SCHEMA(config)
    fields = SERVICE_FIELDS[_SERVICE_FOR_TYPE[config[CONF_TYPE]]]
    unused = [field for field in _ALL_FIELDS if field in config and field not in fields]
    if unused:
        raise vol.Invalid(
            f"{', '.join(unused)} not used by the {config[CONF_TYPE]} work mode"
        )
    missing = [field for field, required in fields.items() if required and field not in config]
    if missing:
        raise vol.Invalid(
            f"{config[CONF_TYPE]} work mode requires {', '.join(missing)}"
        )
    return config


async def async_get_actions(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, str]]:
    """List the actions an Eleven Energy Plus inverter offers."""
    device = dr.async_get(hass).async_get(device_id)

    # Debug rather than silence: when these actions do not appear in the UI,
    # the first thing worth knowing is whether Home Assistant asked at all.
    _LOGGER.debug(
        "Device actions asked for %s (%s)",
        device_id,
        "found" if device is not None else "unknown device",
    )

    if device is None or not any(
        domain == DOMAIN for domain, _identifier in device.identifiers
    ):
        return []

    return [
        {CONF_DEVICE_ID: device_id, CONF_DOMAIN: DOMAIN, CONF_TYPE: action_type}
        for action_type in ACTION_TYPES
    ]


async def async_call_action_from_config(
    hass: HomeAssistant,
    config: ConfigType,
    variables: TemplateVarsType,
    context: Context | None,
) -> None:
    """Run a device action by calling the action behind it.

    The call is blocking and asks for no response, so a failure raises and
    fails the automation step rather than passing silently.
    """
    service = _SERVICE_FOR_TYPE[config[CONF_TYPE]]
    service_data: dict[str, Any] = {"device_id": [config[CONF_DEVICE_ID]]}
    for field in SERVICE_FIELDS[service]:
        if field in config:
            service_data[field] = config[field]

    await hass.services.async_call(
        DOMAIN,
        service,
        service_data,
        blocking=True,
        context=context,
    )


async def async_get_action_capabilities(
    hass: HomeAssistant, config: ConfigType
) -> dict[str, vol.Schema]:
    """Return the form fields a given work mode asks for."""
    service = _SERVICE_FOR_TYPE[config[CONF_TYPE]]
    fields: dict[vol.Marker, Any] = {}
    for field, required in SERVICE_FIELDS[service].items():
        marker = vol.Required(field) if required else vol.Optional(field)
        fields[marker] = _FIELD_SELECTORS[field]()
    if not fields:
        return {}
    return {"extra_fields": vol.Schema(fields)}
