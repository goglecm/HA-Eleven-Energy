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
  had changed mode. Each ``error_kind`` maps to exactly one exception and
  message (:data:`_RAISED`): invalid input or target raises
  :class:`ServiceValidationError`; a rejected credential, an API failure, a
  superseded or aborted command, an internal error and "not loaded" each
  raise :class:`HomeAssistantError` with their own translation key.

Schemas are built with ``voluptuous``, which Home Assistant 2026.9+ aliases
to ``probatio`` at runtime. The two are different types to a type checker,
so the schema objects handed to Home Assistant are annotated ``Any``.
"""

from __future__ import annotations

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
    ErrorKind,
)
from .parsing import finite_float, parse_bool, parse_whole_number

if TYPE_CHECKING:
    from .controller import Controller


def _finite_number(value: Any) -> float:
    """Validate a finite, non-boolean number (numeric strings accepted)."""
    number = finite_float(value)
    if number is None:
        raise vol.Invalid(f"expected a finite number, got {value!r}")
    return number


def _whole_minutes(value: Any) -> int:
    """Validate a whole number of minutes (``60`` and ``"60.0"`` both pass)."""
    minutes = parse_whole_number(value)
    if minutes is None:
        raise vol.Invalid(f"expected a whole number of minutes, got {value!r}")
    return minutes


def _strict_boolean(value: Any) -> bool:
    """Validate an unambiguous boolean.

    Home Assistant's ``cv.boolean`` turns any number into a boolean (``2``
    becomes True). Only booleans, 0/1 and the usual words are accepted.
    """
    parsed = parse_bool(value)
    if parsed is None:
        raise vol.Invalid(f"expected true or false, got {value!r}")
    return parsed


PERCENT_VALUE = vol.All(_finite_number, vol.Range(min=0, max=100))
POWER_KW_VALUE = vol.All(
    _finite_number, vol.Range(min=MIN_POWER_KW, max=MAX_POWER_KW)
)
MINUTES_VALUE = vol.All(
    _whole_minutes, vol.Range(min=MIN_TARGET_MINUTES, max=MAX_TARGET_MINUTES)
)
BOOLEAN_VALUE = _strict_boolean

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


def build_schema(service: str) -> Any:
    """Build the validation schema for one action (at registration)."""
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


# What a failure raises when the caller did not ask for a response:
# exception class and translation key, one per error kind.
_RAISED: dict[ErrorKind, tuple[type[HomeAssistantError], str]] = {
    ErrorKind.INVALID_REQUEST: (ServiceValidationError, "invalid_request"),
    ErrorKind.CREDENTIAL_REJECTED: (HomeAssistantError, "credential_rejected"),
    ErrorKind.API_ERROR: (HomeAssistantError, "work_mode_failed"),
    ErrorKind.SUPERSEDED: (HomeAssistantError, "work_mode_superseded"),
    ErrorKind.ABORTED: (HomeAssistantError, "work_mode_aborted"),
    ErrorKind.INTERNAL_ERROR: (HomeAssistantError, "internal_error"),
    ErrorKind.NOT_LOADED: (HomeAssistantError, "not_loaded"),
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
        "error_kind": ErrorKind.NOT_LOADED,
    }


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the work-mode actions (idempotent)."""

    async def _async_handle_work_mode(call: ServiceCall) -> ServiceResponse:
        data = dict(call.data)
        if _PERCENT_ALIAS in data:
            data["percent_to_battery"] = data.pop(_PERCENT_ALIAS)

        # Only one entry is allowed (single_config_entry); a loaded entry
        # always holds its controller.
        loaded = hass.config_entries.async_loaded_entries(DOMAIN)
        if loaded:
            controller: Controller = loaded[0].runtime_data
            result = await controller.set_work_mode(call.service, data)
        else:
            result = _not_loaded_result()

        if result["success"]:
            return result if call.return_response else None
        if call.return_response:
            return result

        exception, translation_key = _RAISED[ErrorKind(result["error_kind"])]
        raise exception(
            translation_domain=DOMAIN,
            translation_key=translation_key,
            translation_placeholders={
                "service": call.service,
                "error": str(result["error"]),
            },
        )

    for service in SERVICE_NAMES:
        if hass.services.has_service(DOMAIN, service):
            continue
        hass.services.async_register(
            DOMAIN,
            service,
            _async_handle_work_mode,
            schema=build_schema(service),
            supports_response=SupportsResponse.OPTIONAL,
        )
