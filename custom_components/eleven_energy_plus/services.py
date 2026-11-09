"""The ``eleven_energy_plus.set_work_mode_*`` actions.

Registered once from ``async_setup`` (never per config entry, and never
removed on unload, so a reload can't leave the integration without its
actions). Each action has a schema that validates every field before
anything reaches an inverter: numbers must be finite and in range, booleans
must be unambiguous, unknown keys are refused. Invalid input therefore
raises a validation error instead of being clamped or silently dropped.

Failure semantics (stable, documented in the README):

* The caller asked for a response (``response_variable`` in an automation,
  ``return_response=True`` in code): the structured result dict is always
  returned, success or not, so the automation can branch on ``success``.
* The caller did not ask for a response: a failure raises, so the
  automation step fails visibly instead of carrying on as if the inverter
  had changed mode. Target / input problems raise
  :class:`ServiceValidationError`; API and credential problems raise
  :class:`HomeAssistantError`.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN,
    MAX_POWER_KW,
    MAX_TARGET_MINUTES,
    MIN_POWER_KW,
    MIN_TARGET_MINUTES,
)

if TYPE_CHECKING:
    from .controller import Controller


def _finite_number(value: Any) -> float:
    """Validate a finite, non-boolean number (numeric strings accepted)."""
    if isinstance(value, bool):
        raise vol.Invalid(f"expected a number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as err:
        raise vol.Invalid(f"expected a number, got {value!r}") from err
    if not math.isfinite(number):
        raise vol.Invalid(f"expected a finite number, got {value!r}")
    return number


def _whole_minutes(value: Any) -> int:
    """Validate a whole number of minutes (``60`` and ``"60.0"`` both pass)."""
    number = _finite_number(value)
    if not number.is_integer():
        raise vol.Invalid(f"expected a whole number of minutes, got {value!r}")
    return int(number)


PERCENT_VALUE = vol.All(_finite_number, vol.Range(min=0, max=100))
POWER_KW_VALUE = vol.All(
    _finite_number, vol.Range(min=MIN_POWER_KW, max=MAX_POWER_KW)
)
MINUTES_VALUE = vol.All(
    _whole_minutes, vol.Range(min=MIN_TARGET_MINUTES, max=MAX_TARGET_MINUTES)
)
BOOLEAN_VALUE = cv.boolean

# Validators per field, shared with ``device_action.py`` so a device action
# accepts exactly what calling the action directly accepts.
FIELD_VALIDATORS: dict[str, Any] = {
    "percent_to_battery": PERCENT_VALUE,
    "target_power": POWER_KW_VALUE,
    "target_percent": PERCENT_VALUE,
    "target_soc": PERCENT_VALUE,
    "target_minutes": MINUTES_VALUE,
    "max_charge_power": POWER_KW_VALUE,
    "max_discharge_power": POWER_KW_VALUE,
    "allow_charging": BOOLEAN_VALUE,
    "allow_discharging": BOOLEAN_VALUE,
    "include_excess_solar": BOOLEAN_VALUE,
    "overdrive": BOOLEAN_VALUE,
}

# Fields per action, mapped to whether they are required. Order is the order
# they are offered in the device action form.
SERVICE_FIELDS: dict[str, dict[str, bool]] = {
    "set_work_mode_self_consumption": {"percent_to_battery": False},
    "set_work_mode_force_charge": {"target_power": True, "target_percent": True},
    "set_work_mode_grid_export": {
        "target_power": True,
        "target_percent": True,
        "include_excess_solar": False,
        "overdrive": False,
    },
    "set_work_mode_pv_export": {},
    "set_work_mode_idle_battery": {
        "allow_charging": False,
        "allow_discharging": False,
    },
    "set_work_mode_target_soc": {
        "target_soc": True,
        "target_minutes": True,
        "max_charge_power": False,
        "max_discharge_power": False,
    },
    "set_work_mode_reset": {},
}

SERVICE_NAMES: tuple[str, ...] = tuple(SERVICE_FIELDS)

# ``percentage_to_battery`` is the field name the original integration's
# services.yaml once offered; it is accepted as an alias so automations
# written against it keep working. The two are mutually exclusive.
_PERCENT_ALIAS = "percentage_to_battery"

# Target keys Home Assistant may merge into the call data. Accepted by every
# schema; ``Controller._resolve_target_device`` decides what they mean.
_TARGET_FIELDS: dict[Any, Any] = {
    vol.Optional(key): vol.All(cv.ensure_list, [cv.string])
    for key in ("device_id", "entity_id", "area_id", "floor_id", "label_id")
}


def _build_schema(service: str) -> vol.Schema:
    """Build the validation schema for one action."""
    schema: dict[Any, Any] = dict(_TARGET_FIELDS)
    for field, required in SERVICE_FIELDS[service].items():
        if service == "set_work_mode_self_consumption":
            marker: Any = vol.Exclusive(field, "percent_to_battery")
        elif required:
            marker = vol.Required(field)
        else:
            marker = vol.Optional(field)
        schema[marker] = FIELD_VALIDATORS[field]
    if service == "set_work_mode_self_consumption":
        schema[vol.Exclusive(_PERCENT_ALIAS, "percent_to_battery")] = PERCENT_VALUE
    return vol.Schema(schema)


SERVICE_SCHEMAS: dict[str, vol.Schema] = {
    service: _build_schema(service) for service in SERVICE_NAMES
}


def _not_loaded_result() -> dict[str, Any]:
    return {
        "success": False,
        "status": None,
        "device_id": None,
        "work_mode": None,
        "params": {},
        "attempts": 0,
        "error": "integration not loaded",
    }


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the work-mode actions (idempotent)."""

    async def _async_handle_work_mode(call: ServiceCall) -> ServiceResponse:
        data = dict(call.data)
        if _PERCENT_ALIAS in data:
            data["percent_to_battery"] = data.pop(_PERCENT_ALIAS)

        controller: Controller | None = None
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            controller = getattr(entry, "runtime_data", None)
            if controller is not None:
                break

        if controller is None:
            result = _not_loaded_result()
        else:
            result = await controller.set_work_mode(call.service, data)

        if result["success"]:
            return result if call.return_response else None
        if call.return_response:
            return result

        placeholders = {"service": call.service, "error": str(result["error"])}
        if controller is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="not_loaded",
                translation_placeholders=placeholders,
            )
        if result["attempts"] == 0 and not controller.auth_failed:
            # Nothing was sent: the request itself (target, fields) is wrong.
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_request",
                translation_placeholders=placeholders,
            )
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="work_mode_failed",
            translation_placeholders=placeholders,
        )

    for service in SERVICE_NAMES:
        if hass.services.has_service(DOMAIN, service):
            continue
        hass.services.async_register(
            DOMAIN,
            service,
            _async_handle_work_mode,
            schema=SERVICE_SCHEMAS[service],
            supports_response=SupportsResponse.OPTIONAL,
        )
