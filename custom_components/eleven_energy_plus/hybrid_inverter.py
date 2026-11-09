"""Eleven Energy hybrid-inverter device representation.

This module owns the per-device side of the integration: one
:class:`HybridInverter` instance per inverter discovered by
:meth:`Controller.poll_site`, plus the two entity classes
(:class:`InverterSensorEntity` / :class:`InverterBinarySensorEntity`)
that make their values visible to Home Assistant.

The inverter has three streams of entities:

* **Predefined** - hand-curated entries declared in :data:`_PREDEFINED_DEFS`
  that exist on every inverter (PV power, battery state of charge, ...).

* **Model-driven** - curated entries whose *existence* depends on the
  hardware, decided from the API's ``model`` block and the first device
  reading rather than from whichever registers happen to be present:

  - per-phase power / voltage / current for ``grid`` and ``system``, only
    when ``model.threePhase`` is true;
  - per-MPPT (solar tracker) power / voltage / current - three trackers on
    ``model.dualBattery`` models, two otherwise, none for an unknown model;
  - per-battery raw state of charge / power / module count, one set per
    battery the reading lists;
  - nameplate AC / DC power ratings, when the model reports them.

  List items (``pv.mppts``, ``battery.batteries``) are keyed by each item's
  own ``index`` field rather than its position, so a payload that ever omits
  one entry cannot shift another tracker's or battery's readings onto it.

* **Auto-discovered** - anything else the API returns that
  :func:`_infer_meta` can heuristically classify (numeric, boolean, string).
  These are created lazily during :meth:`update`, registered with HA via the
  controller's deferred ``add_entities`` callbacks, capped per device at
  :data:`~.const.MAX_DYNAMIC_ENTITIES_PER_DEVICE`, and named from the dotted
  JSON path via :func:`_humanise_path`.

Every entity follows its inverter's availability (see
:meth:`HybridInverter.record_poll_failure`) and validates the values it is
handed: numeric sensors only ever report finite numbers (anything else reads
as *unknown*), and binary sensors only accept unambiguous booleans.

The walker (:meth:`_walk`) is deliberately defensive: every recursion and
every leaf write is wrapped in ``try/except`` so a single malformed field
cannot poison the entire update, and ``list`` values are capped at
:data:`_MAX_LIST_EXPANSION` entries.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import logging
import math
import re
from typing import TYPE_CHECKING, Any, cast

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfApparentPower,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfReactivePower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import generate_entity_id

from .const import (
    DOMAIN,
    MAX_DYNAMIC_ENTITIES_PER_DEVICE,
    PERCENT,
    UNAVAILABLE_AFTER_FAILED_POLLS,
)

if TYPE_CHECKING:
    from .controller import Controller

_LOGGER = logging.getLogger(__name__)


# Keys we never want to expose as entities (identifiers and type tags).
_IGNORED_PATHS: frozenset[str] = frozenset(
    {
        "deviceId",
        "type",
        "name",
        "serialNumber",
    }
)

# Predefined string sensors whose translation ``state:`` keys are lowercase.
# Values arriving for these paths must be lowercased so the translation lookup
# succeeds. Dynamically-discovered string sensors keep their original casing.
_LOWERCASE_STRING_PATHS: frozenset[str] = frozenset(
    {
        "status",
        "operatingMode.workMode",
    }
)

# Hard cap on how many entries we expand per list. Anything beyond is logged
# once and dropped, to keep pathological payloads from creating thousands of
# entities.
_MAX_LIST_EXPANSION = 32

# Phase labels the API uses under ``grid.phases`` / ``system.phases``.
_PHASES: tuple[str, ...] = ("L1", "L2", "L3")

# Strings accepted as booleans by :func:`parse_bool`.
_TRUE_STRINGS: frozenset[str] = frozenset({"true", "on", "yes", "1"})
_FALSE_STRINGS: frozenset[str] = frozenset({"false", "off", "no", "0"})


# Hand-tuned definitions for the well-known fields. Keys are dotted JSON paths.
# The values match the keyword arguments expected by ``InverterSensorEntity``
# (with ``kind`` discriminating sensor vs binary_sensor). The translation key
# is ``entity_type`` lowercased unless a definition says otherwise.
_PREDEFINED_DEFS: dict[str, dict[str, Any]] = {
    "pv.power": {
        "kind": "sensor",
        "entity_type": "pv_power",
        "unit": UnitOfPower.KILO_WATT,
        "device_class": SensorDeviceClass.POWER,
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:solar-power",
        "decimals": 2,
    },
    "pv.energyToday": {
        "kind": "sensor",
        "entity_type": "pv_energy_today",
        "unit": UnitOfEnergy.KILO_WATT_HOUR,
        "device_class": SensorDeviceClass.ENERGY,
        "state_class": SensorStateClass.TOTAL_INCREASING,
        "icon": "mdi:solar-power-variant",
        "decimals": 2,
    },
    "load.power": {
        "kind": "sensor",
        "entity_type": "load_power",
        "unit": UnitOfPower.KILO_WATT,
        "device_class": SensorDeviceClass.POWER,
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:home-lightning-bolt",
        "decimals": 2,
    },
    "load.energyToday": {
        "kind": "sensor",
        "entity_type": "load_energy_today",
        "unit": UnitOfEnergy.KILO_WATT_HOUR,
        "device_class": SensorDeviceClass.ENERGY,
        "state_class": SensorStateClass.TOTAL_INCREASING,
        "icon": "mdi:lightning-bolt",
        "decimals": 2,
    },
    "battery.stateOfCharge": {
        "kind": "sensor",
        "entity_type": "state_of_charge",
        "unit": PERCENT,
        "device_class": SensorDeviceClass.BATTERY,
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:battery",
        "decimals": 0,
    },
    "battery.power": {
        "kind": "sensor",
        "entity_type": "battery_power",
        "unit": UnitOfPower.KILO_WATT,
        "device_class": SensorDeviceClass.POWER,
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:battery-minus-variant",
        "decimals": 2,
    },
    "battery.energyInToday": {
        "kind": "sensor",
        "entity_type": "battery_energy_in_today",
        "unit": UnitOfEnergy.KILO_WATT_HOUR,
        "device_class": SensorDeviceClass.ENERGY,
        "state_class": SensorStateClass.TOTAL_INCREASING,
        "icon": "mdi:battery-plus",
        "decimals": 2,
    },
    "battery.energyOutToday": {
        "kind": "sensor",
        "entity_type": "battery_energy_out_today",
        "unit": UnitOfEnergy.KILO_WATT_HOUR,
        "device_class": SensorDeviceClass.ENERGY,
        "state_class": SensorStateClass.TOTAL_INCREASING,
        "icon": "mdi:battery-minus",
        "decimals": 2,
    },
    "grid.power": {
        "kind": "sensor",
        "entity_type": "grid_power",
        "unit": UnitOfPower.KILO_WATT,
        "device_class": SensorDeviceClass.POWER,
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:transmission-tower",
        "decimals": 2,
    },
    "grid.energyInToday": {
        "kind": "sensor",
        "entity_type": "grid_energy_in_today",
        "unit": UnitOfEnergy.KILO_WATT_HOUR,
        "device_class": SensorDeviceClass.ENERGY,
        "state_class": SensorStateClass.TOTAL_INCREASING,
        "icon": "mdi:transmission-tower-export",
        "decimals": 2,
    },
    "grid.energyOutToday": {
        "kind": "sensor",
        "entity_type": "grid_energy_out_today",
        "unit": UnitOfEnergy.KILO_WATT_HOUR,
        "device_class": SensorDeviceClass.ENERGY,
        "state_class": SensorStateClass.TOTAL_INCREASING,
        "icon": "mdi:transmission-tower-import",
        "decimals": 2,
    },
    "system.power": {
        "kind": "sensor",
        "entity_type": "system_power",
        "unit": UnitOfPower.KILO_WATT,
        "device_class": SensorDeviceClass.POWER,
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:flash",
        "decimals": 2,
        "category": EntityCategory.DIAGNOSTIC,
    },
    "system.voltage": {
        "kind": "sensor",
        "entity_type": "system_voltage",
        "unit": UnitOfElectricPotential.VOLT,
        "device_class": SensorDeviceClass.VOLTAGE,
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:flash",
        "decimals": 2,
        "category": EntityCategory.DIAGNOSTIC,
    },
    "operatingMode.workMode": {
        "kind": "sensor",
        "entity_type": "system_work_mode",
        "unit": None,
        "device_class": None,
        "state_class": None,
        "icon": "mdi:all-inclusive-box-outline",
    },
    "status": {
        "kind": "sensor",
        "entity_type": "system_status",
        "unit": None,
        "device_class": None,
        "state_class": None,
        "icon": "mdi:check-network-outline",
        "category": EntityCategory.DIAGNOSTIC,
    },
    "online": {
        "kind": "binary_sensor",
        "entity_type": "system_online",
        "device_class": BinarySensorDeviceClass.CONNECTIVITY,
        "icon": "mdi:cloud-check-variant",
        "category": EntityCategory.DIAGNOSTIC,
    },
    "firmwareVersion": {
        "kind": "sensor",
        "entity_type": "firmware_version",
        "unit": None,
        "device_class": None,
        "state_class": None,
        "icon": "mdi:chip",
        "category": EntityCategory.DIAGNOSTIC,
    },
}


# Per-item field definitions for the index-keyed lists. ``{index}`` in
# ``entity_type`` is the item's own ``index`` value; the translation key is
# shared between items and takes the index as a placeholder.
_MPPT_FIELDS: dict[str, dict[str, Any]] = {
    "power": {
        "entity_type": "mppt_{index}_power",
        "translation_key": "mppt_power",
        "unit": UnitOfPower.KILO_WATT,
        "device_class": SensorDeviceClass.POWER,
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:solar-power-variant",
        "decimals": 3,
    },
    "voltage": {
        "entity_type": "mppt_{index}_voltage",
        "translation_key": "mppt_voltage",
        "unit": UnitOfElectricPotential.VOLT,
        "device_class": SensorDeviceClass.VOLTAGE,
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:flash-triangle-outline",
        "decimals": 1,
    },
    "current": {
        "entity_type": "mppt_{index}_current",
        "translation_key": "mppt_current",
        "unit": UnitOfElectricCurrent.AMPERE,
        "device_class": SensorDeviceClass.CURRENT,
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:current-dc",
        "decimals": 2,
    },
}

_BATTERY_FIELDS: dict[str, dict[str, Any]] = {
    # Raw from the BMS, before the top and bottom buffer allowances. It will
    # not agree with the pack's usable state of charge, so it is named "raw"
    # to keep the two apart rather than look like a rounding error.
    "stateOfCharge": {
        "entity_type": "battery_{index}_raw_state_of_charge",
        "translation_key": "battery_unit_raw_state_of_charge",
        "unit": PERCENT,
        "device_class": SensorDeviceClass.BATTERY,
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:battery-heart-variant",
        "decimals": 0,
    },
    "power": {
        "entity_type": "battery_{index}_power",
        "translation_key": "battery_unit_power",
        "unit": UnitOfPower.KILO_WATT,
        "device_class": SensorDeviceClass.POWER,
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:battery-charging",
        "decimals": 2,
    },
    "moduleCount": {
        "entity_type": "battery_{index}_modules",
        "translation_key": "battery_unit_modules",
        "unit": None,
        "device_class": None,
        "state_class": None,
        "icon": "mdi:battery-sync",
        "decimals": 0,
    },
}

# List paths handled by the index-keyed processors (never by the generic
# walker), mapped to their per-item field definitions.
_INDEXED_LISTS: dict[str, dict[str, dict[str, Any]]] = {
    "pv.mppts": _MPPT_FIELDS,
    "battery.batteries": _BATTERY_FIELDS,
}

# Per-phase field definitions. ``entity_type`` intentionally matches what the
# generic walker would have produced for the same path (``grid_phases_L1_power``)
# so installations that already auto-discovered these keep their history.
_PHASE_FIELDS: dict[str, dict[str, Any]] = {
    "power": {
        "unit": UnitOfPower.KILO_WATT,
        "device_class": SensorDeviceClass.POWER,
        "decimals": 2,
    },
    "voltage": {
        "unit": UnitOfElectricPotential.VOLT,
        "device_class": SensorDeviceClass.VOLTAGE,
        "decimals": 1,
        "icon": "mdi:sine-wave",
        "category": EntityCategory.DIAGNOSTIC,
    },
    "current": {
        "unit": UnitOfElectricCurrent.AMPERE,
        "device_class": SensorDeviceClass.CURRENT,
        "decimals": 2,
        "icon": "mdi:current-ac",
        "category": EntityCategory.DIAGNOSTIC,
    },
}

# Per-group settings for the phase power entity: each group's phases sit in
# the same part of the device page as the group's own headline reading.
_PHASE_GROUPS: dict[str, dict[str, Any]] = {
    "grid": {"icon": "mdi:transmission-tower", "category": None},
    "system": {"icon": "mdi:flash", "category": EntityCategory.DIAGNOSTIC},
}

# Nameplate ratings from the ``model`` block. ``entity_type`` matches the
# generic walker's name for the same path so existing history continues.
_RATING_FIELDS: dict[str, dict[str, Any]] = {
    "maxAcPower": {
        "entity_type": "model_maxAcPower",
        "translation_key": "rated_ac_power",
        "icon": "mdi:transmission-tower",
    },
    "maxDcPower": {
        "entity_type": "model_maxDcPower",
        "translation_key": "rated_dc_power",
        "icon": "mdi:battery-charging-high",
    },
}


_CAMEL_SPLIT_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z0-9]+|[A-Z]+|\d+")
_KNOWN_ACRONYMS: frozenset[str] = frozenset(
    {"pv", "ac", "dc", "soc", "soh", "ev", "id", "ip", "url", "api", "kw", "kwh"}
)


def _humanise_path(path: str) -> str:
    """Turn ``pv.someThingValue`` into ``PV Some Thing Value``."""
    parts: list[str] = []
    for chunk in path.split("."):
        words = _CAMEL_SPLIT_RE.findall(chunk)
        if not words:
            words = [chunk]
        formatted: list[str] = []
        for word in words:
            lower = word.lower()
            if lower in _KNOWN_ACRONYMS:
                formatted.append(lower.upper())
            elif word.isupper() and len(word) <= 3:
                formatted.append(word)
            else:
                formatted.append(word.capitalize())
        parts.append(" ".join(formatted))
    return " ".join(parts)


def coerce_finite_number(value: Any) -> int | float | None:
    """Return ``value`` as a finite number, or ``None`` if it isn't one.

    Booleans are rejected (``True`` is not ``1 kW``), numeric strings are
    parsed, and NaN / infinity are treated as missing data rather than passed
    on to Home Assistant, which would either reject them or record nonsense
    statistics.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def parse_bool(value: Any) -> bool | None:
    """Parse an API value as a boolean, or ``None`` when it is ambiguous.

    ``bool("false")`` is ``True``, which is exactly the wrong answer for a
    connectivity sensor, so strings and numbers are interpreted explicitly
    and anything unrecognised reads as unknown instead of guessing.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_STRINGS:
            return True
        if lowered in _FALSE_STRINGS:
            return False
    return None


def _infer_meta(path: str, value: Any) -> dict[str, Any] | None:
    """Best-effort meta dict for a JSON leaf the user hadn't predefined."""
    if value is None:
        return None

    last = path.rsplit(".", 1)[-1]
    last_lower = last.lower()
    full_lower = path.lower()
    entity_type = path.replace(".", "_")

    base: dict[str, Any] = {
        "entity_type": entity_type,
        "name": _humanise_path(path),
        "category": EntityCategory.DIAGNOSTIC,
    }

    if isinstance(value, bool):
        device_class: BinarySensorDeviceClass | None = None
        if "online" in last_lower or "connected" in last_lower:
            device_class = BinarySensorDeviceClass.CONNECTIVITY
        elif "fault" in last_lower or "error" in last_lower or "alarm" in last_lower:
            device_class = BinarySensorDeviceClass.PROBLEM
        elif "running" in last_lower or "active" in last_lower:
            device_class = BinarySensorDeviceClass.RUNNING
        base.update(
            {
                "kind": "binary_sensor",
                "device_class": device_class,
                "icon": "mdi:checkbox-marked-circle-outline",
            }
        )
        return base

    if isinstance(value, (int, float)):
        unit: str | None = None
        sensor_class: SensorDeviceClass | None = None
        state_class: SensorStateClass | None = SensorStateClass.MEASUREMENT
        icon: str | None = None
        decimals = -1

        is_today_energy = "energy" in full_lower and "today" in full_lower
        is_lifetime_energy = "energy" in full_lower and (
            "lifetime" in full_lower or "total" in full_lower
        )

        if is_today_energy or is_lifetime_energy:
            unit = UnitOfEnergy.KILO_WATT_HOUR
            sensor_class = SensorDeviceClass.ENERGY
            state_class = SensorStateClass.TOTAL_INCREASING
            icon = "mdi:lightning-bolt"
            decimals = 2
        elif "reactivepower" in last_lower or last_lower.endswith("reactive"):
            unit = UnitOfReactivePower.VOLT_AMPERE_REACTIVE
            sensor_class = SensorDeviceClass.REACTIVE_POWER
            icon = "mdi:sine-wave"
            decimals = 0
        elif "apparentpower" in last_lower or last_lower.endswith("apparent"):
            unit = UnitOfApparentPower.VOLT_AMPERE
            sensor_class = SensorDeviceClass.APPARENT_POWER
            icon = "mdi:flash-outline"
            decimals = 0
        elif "power" in last_lower:
            unit = UnitOfPower.KILO_WATT
            sensor_class = SensorDeviceClass.POWER
            icon = "mdi:flash"
            decimals = 2
        elif last_lower in {
            "rate",
            "chargerate",
            "dischargerate",
            "maxchargerate",
            "maxdischargerate",
        }:
            unit = UnitOfPower.KILO_WATT
            sensor_class = SensorDeviceClass.POWER
            icon = "mdi:battery-arrow-up"
            decimals = 2
        elif "voltage" in last_lower:
            unit = UnitOfElectricPotential.VOLT
            sensor_class = SensorDeviceClass.VOLTAGE
            icon = "mdi:flash"
            decimals = 2
        elif "current" in last_lower:
            unit = UnitOfElectricCurrent.AMPERE
            sensor_class = SensorDeviceClass.CURRENT
            icon = "mdi:current-ac"
            decimals = 2
        elif "frequency" in last_lower:
            unit = UnitOfFrequency.HERTZ
            sensor_class = SensorDeviceClass.FREQUENCY
            icon = "mdi:sine-wave"
            decimals = 2
        elif "temperature" in last_lower or last_lower.endswith("temp"):
            unit = UnitOfTemperature.CELSIUS
            sensor_class = SensorDeviceClass.TEMPERATURE
            icon = "mdi:thermometer"
            decimals = 1
        elif "stateofhealth" in last_lower or last_lower == "soh":
            unit = PERCENT
            icon = "mdi:heart-pulse"
            decimals = 0
        elif (
            "stateofcharge" in last_lower
            or last_lower == "soc"
            or last_lower.endswith("pc")
            or "percent" in last_lower
        ):
            unit = PERCENT
            icon = "mdi:percent"
            decimals = 0
            if "battery" in full_lower or last_lower in ("stateofcharge", "soc"):
                sensor_class = SensorDeviceClass.BATTERY
        else:
            icon = "mdi:numeric"

        base.update(
            {
                "kind": "sensor",
                "unit": unit,
                "device_class": sensor_class,
                "state_class": state_class,
                "icon": icon,
                "decimals": decimals,
            }
        )
        return base

    if isinstance(value, str):
        version_like = (
            "version" in last_lower
            or last_lower == "model"
            or "firmware" in last_lower
            or "serial" in last_lower
        )
        base.update(
            {
                "kind": "sensor",
                "unit": None,
                "device_class": None,
                "state_class": None,
                "icon": "mdi:chip" if version_like else "mdi:information-outline",
            }
        )
        return base

    return None


def _item_index(item: dict[str, Any], position: int) -> int:
    """Return a list item's own ``index`` field, or ``position + 1`` without one."""
    index = item.get("index")
    if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
        return index
    return position + 1


def _lookup(payload: dict[str, Any], dotted: str) -> Any:
    """Return the value at a dotted path in nested dicts, or ``None``."""
    current: Any = payload
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def indexed_items(
    payload: dict[str, Any] | None, list_path: str
) -> Iterator[tuple[int, int, dict[str, Any]]]:
    """Yield ``(position, index, item)`` for dict items of a list in ``payload``."""
    if not isinstance(payload, dict):
        return
    items = _lookup(payload, list_path)
    if not isinstance(items, list):
        return
    for position, item in enumerate(items[:_MAX_LIST_EXPANSION]):
        if isinstance(item, dict):
            yield position, _item_index(item, position), item


class HybridInverter:
    """A single Eleven Energy hybrid inverter and its entity collection.

    Constructed by :meth:`Controller.poll_site` for each discovered device.
    Two entity dictionaries are kept, both keyed by the dotted JSON path of
    the leaf the entity represents (e.g. ``pv.power``,
    ``operatingMode.workMode``, ``pv.mppts.1.power`` for MPPT index 1, or
    for auto-discovered fields ``someBlock.someField``):

    * :attr:`sensor_entities` - :class:`InverterSensorEntity` instances.
    * :attr:`binary_sensor_entities` - :class:`InverterBinarySensorEntity`
      instances.

    The same path-keyed dicts power :meth:`_handle_leaf`'s O(1) routing: the
    walker emits a path, looks it up in the appropriate dict, and pushes the
    new value into the entity's setter (which itself short-circuits on
    unchanged values).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        controller: Controller,
        device_id: str,
        device_name: str,
        device_serial_number: str,
        *,
        model: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
    ) -> None:
        """Create an inverter wrapper and its curated entities.

        :param hass: HA core, forwarded to entities for ``entity_id`` slug
            generation via :func:`generate_entity_id`.
        :param controller: parent :class:`Controller`; used to register any
            entities that get created after the platforms have set up.
        :param device_id: Eleven Energy API device id. Stored as the second
            element of ``DeviceInfo.identifiers`` so device-targeted services
            can map back to us.
        :param device_name: the device-card label, already resolved by the
            controller (user override, else the ``model`` block's ``name``,
            else the API ``name`` field). Prefixed with ``"Eleven Energy Plus "``
            for the device card so the fork is distinguishable from the
            original integration when both are installed.
        :param device_serial_number: inverter serial; surfaced on the device
            card.
        :param model: the API's ``model`` block (``threePhase``,
            ``dualBattery``, ``maxAcPower``, ...). Decides which hardware
            dependent entities exist.
        :param state: the first device reading, taken during discovery.
            Decides how many battery entity sets exist and drives the
            registry migration of older positional entities.
        """
        self.type = "hybridinverter"
        self.device_id = device_id
        self.device_label = device_name
        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, self.device_id)},
            name=f"Eleven Energy Plus {device_name}",
            manufacturer="Eleven Energy",
            model=device_name,
            serial_number=device_serial_number,
        )
        self.hass = hass
        self.controller = controller
        self.model: dict[str, Any] = dict(model) if isinstance(model, dict) else {}
        self.discovery_state: dict[str, Any] | None = (
            state if isinstance(state, dict) else None
        )

        self.sensor_entities: dict[str, InverterSensorEntity] = {}
        self.binary_sensor_entities: dict[str, InverterBinarySensorEntity] = {}

        # Availability bookkeeping, driven by the controller's poll results.
        self.available = True
        self._consecutive_failures = 0

        # Remember which list paths we have already truncated so the
        # truncation warning fires at most once per path per process.
        self._truncated_list_paths: set[str] = set()
        self._dynamic_entity_count = 0
        self._dynamic_cap_warned = False

        for path, meta in _PREDEFINED_DEFS.items():
            self._create_entity_for(path, meta)

        if self.three_phase:
            for group in _PHASE_GROUPS:
                self._create_phase_entities(group)

        for index in range(1, self.mppt_count + 1):
            self._create_indexed_entities("pv.mppts", index)

        for _position, index, _item in indexed_items(
            self.discovery_state, "battery.batteries"
        ):
            self._create_indexed_entities("battery.batteries", index)

        for field in _RATING_FIELDS:
            if field in self.model:
                entity = self._create_rating_entity(field)
                entity.set_native_value(self.model[field])

    # ------------------------------------------------------------------
    # Hardware description
    # ------------------------------------------------------------------

    @property
    def model_known(self) -> bool:
        """Return True when the API reported a ``model`` block for this device."""
        return bool(self.model)

    @property
    def three_phase(self) -> bool:
        """Return True when the model says this is a three-phase machine."""
        return self.model.get("threePhase") is True

    @property
    def mppt_count(self) -> int:
        """How many solar trackers this machine has, from the model.

        Every machine reports three MPPT register sets whether or not it has
        three trackers, so counting what is present would give a two-tracker
        machine a third one reading zero for ever. Dual-battery models have
        three inputs, the rest two, and an unknown model gets none rather than
        a guess.
        """
        if not self.model_known:
            return 0
        return 3 if self.model.get("dualBattery") is True else 2

    @property
    def battery_indices(self) -> list[int]:
        """Return the battery indices this inverter currently has entities for."""
        indices = {
            int(path.split(".")[2])
            for path in self.sensor_entities
            if path.startswith("battery.batteries.")
        }
        return sorted(indices)

    @property
    def curated_entity_types(self) -> set[str]:
        """Return the ``entity_type`` of every curated (non-dynamic) entity."""
        return {
            entity.entity_type
            for entity in self.all_entities()
            if not entity.auto_discovered
        }

    def all_entities(self) -> list[InverterSensorEntity | InverterBinarySensorEntity]:
        """Return every sensor and binary sensor entity of this inverter."""
        return [*self.sensor_entities.values(), *self.binary_sensor_entities.values()]

    # ------------------------------------------------------------------
    # Registry migration support (used by ``migration.py``)
    # ------------------------------------------------------------------

    def positional_migrations(self) -> dict[str, str]:
        """Map older positional ``entity_type``s to their index-keyed successors.

        Releases before 2.0 auto-discovered list items by position
        (``pv_mppts_0_power``). Those positions are mapped to each item's own
        index using the discovery reading, so the registry entry - and its
        history - moves to the curated entity instead of being orphaned.
        """
        migrations: dict[str, str] = {}
        for list_path, fields in _INDEXED_LISTS.items():
            old_prefix = list_path.replace(".", "_")
            for position, index, _item in indexed_items(
                self.discovery_state, list_path
            ):
                for field, meta in fields.items():
                    if f"{list_path}.{index}.{field}" not in self.sensor_entities:
                        continue
                    migrations[f"{old_prefix}_{position}_{field}"] = meta[
                        "entity_type"
                    ].format(index=index)
        return migrations

    def obsolete_entity_patterns(self) -> list[re.Pattern[str]]:
        """Return patterns of older auto-discovered ``entity_type``s to remove.

        Only returned once the hardware is known (a ``model`` block and a
        discovery reading), so nothing is removed on the strength of a guess.
        """
        if not self.model_known or self.discovery_state is None:
            return []
        patterns = [
            re.compile(r"^pv_mppts_\d+_"),
            re.compile(r"^battery_batteries_\d+_"),
            re.compile(r"^model_(?!maxAcPower$|maxDcPower$)"),
        ]
        if not self.three_phase:
            patterns.append(re.compile(r"^(grid|system)_phases_"))
        return patterns

    # ------------------------------------------------------------------
    # Entity construction
    # ------------------------------------------------------------------

    def _create_entity_for(
        self,
        path: str,
        meta: dict[str, Any],
        *,
        auto_discovered: bool = False,
    ) -> InverterSensorEntity | InverterBinarySensorEntity:
        """Instantiate an entity from a meta dict and stash it under ``path``.

        Curated definitions are named through translations (``translation_key``
        defaulting to ``entity_type`` lowercased, plus optional
        ``translation_placeholders``); auto-discovered ones carry a humanised
        ``name`` instead, so they read sensibly without a translation for
        every possible API field.
        """
        kind = meta.get("kind", "sensor")
        entity_type = meta["entity_type"]
        if auto_discovered:
            translation_key = None
            name = meta.get("name")
        else:
            translation_key = meta.get("translation_key", entity_type.lower())
            name = None

        common: dict[str, Any] = {
            "device_info": self.device_info,
            "device_id": self.device_id,
            "entity_type": entity_type,
            "icon": meta.get("icon"),
            "category": meta.get("category"),
            "device_class": meta.get("device_class"),
            "translation_key": translation_key,
            "translation_placeholders": meta.get("translation_placeholders"),
            "name": name,
            "availability": self.is_available,
            "auto_discovered": auto_discovered,
        }

        if kind == "binary_sensor":
            entity: InverterSensorEntity | InverterBinarySensorEntity = (
                InverterBinarySensorEntity(hass=self.hass, **common)
            )
            self.binary_sensor_entities[path] = entity
            return entity

        entity = InverterSensorEntity(
            hass=self.hass,
            unit_of_measurement=meta.get("unit"),
            state_class=meta.get("state_class"),
            decimals=meta.get("decimals", -1),
            **common,
        )
        self.sensor_entities[path] = entity
        return entity

    def _create_phase_entities(self, group: str) -> None:
        """Create per-phase power, voltage and current entities for ``group``."""
        settings = _PHASE_GROUPS[group]
        for phase in _PHASES:
            for field, field_meta in _PHASE_FIELDS.items():
                meta = {
                    "kind": "sensor",
                    "entity_type": f"{group}_phases_{phase}_{field}",
                    "translation_key": f"{group}_phase_{field}",
                    "translation_placeholders": {"phase": phase},
                    "unit": field_meta["unit"],
                    "device_class": field_meta["device_class"],
                    "state_class": SensorStateClass.MEASUREMENT,
                    "decimals": field_meta["decimals"],
                    "icon": field_meta.get("icon", settings["icon"]),
                    "category": field_meta.get("category", settings["category"]),
                }
                self._create_entity_for(f"{group}.phases.{phase}.{field}", meta)

    def _create_indexed_entities(
        self, list_path: str, index: int
    ) -> list[InverterSensorEntity | InverterBinarySensorEntity]:
        """Create the curated entities for one MPPT / battery ``index``."""
        created: list[InverterSensorEntity | InverterBinarySensorEntity] = []
        for field, field_meta in _INDEXED_LISTS[list_path].items():
            path = f"{list_path}.{index}.{field}"
            if path in self.sensor_entities:
                continue
            meta = {
                **field_meta,
                "kind": "sensor",
                "entity_type": field_meta["entity_type"].format(index=index),
                "translation_placeholders": {"index": str(index)},
                "category": EntityCategory.DIAGNOSTIC,
            }
            created.append(self._create_entity_for(path, meta))
        return created

    def _create_rating_entity(self, field: str) -> InverterSensorEntity:
        """Create the nameplate rating entity for ``model.<field>``."""
        rating = _RATING_FIELDS[field]
        meta = {
            "kind": "sensor",
            "entity_type": rating["entity_type"],
            "translation_key": rating["translation_key"],
            "unit": UnitOfPower.KILO_WATT,
            "device_class": SensorDeviceClass.POWER,
            "state_class": None,
            "decimals": 1,
            "icon": rating["icon"],
            "category": EntityCategory.DIAGNOSTIC,
        }
        return cast(
            InverterSensorEntity, self._create_entity_for(f"model.{field}", meta)
        )

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return whether this inverter's data is currently trustworthy."""
        return self.available

    def record_poll_success(self) -> None:
        """Reset the failure counter and become available again."""
        self._consecutive_failures = 0
        self._set_available(True)

    def record_poll_failure(self) -> None:
        """Count a failed poll; go unavailable once the threshold is reached."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= UNAVAILABLE_AFTER_FAILED_POLLS:
            self._set_available(False)

    def mark_unavailable(self) -> None:
        """Go unavailable immediately (e.g. the credential was rejected)."""
        self._consecutive_failures = max(
            self._consecutive_failures, UNAVAILABLE_AFTER_FAILED_POLLS
        )
        self._set_available(False)

    def _set_available(self, available: bool) -> None:
        if self.available == available:
            return
        self.available = available
        _LOGGER.log(
            logging.INFO if available else logging.WARNING,
            "Eleven Energy Plus inverter %s is now %s",
            self.device_id,
            "available" if available else "unavailable",
        )
        for entity in self.all_entities():
            entity.write_state()

    # ------------------------------------------------------------------
    # Payload processing
    # ------------------------------------------------------------------

    def update(self, payload: Any) -> None:
        """Walk the device payload and push values into the right entities."""
        if not isinstance(payload, dict):
            _LOGGER.warning(
                "Eleven Energy Plus ignoring non-object payload for device %s: %s",
                self.device_id,
                type(payload).__name__,
            )
            return
        self._walk(payload, "")
        self._process_indexed_lists(payload)
        self._process_model(payload.get("model"))

    def _is_suppressed(self, path: str, value: Any) -> bool:
        """Return True for subtrees the generic walker must not auto-discover."""
        if path == "model" and isinstance(value, dict):
            # Handled by ``_process_model``; ``name`` feeds the device card.
            return True
        if path in _INDEXED_LISTS and isinstance(value, list):
            # Handled by ``_process_indexed_lists``, keyed by own index.
            return True
        if path in ("grid.phases", "system.phases") and self.model_known:
            # Curated on three-phase machines (extra fields still discovered
            # below); phantom registers on single-phase machines are dropped.
            return not self.three_phase
        return False

    def _walk(self, value: Any, path: str) -> None:
        if path in _IGNORED_PATHS:
            return
        if path and self._is_suppressed(path, value):
            return

        if isinstance(value, dict):
            for key, sub_value in value.items():
                # Skip private-looking keys and keys on the global ignore-list.
                if isinstance(key, str) and key.startswith("_"):
                    continue
                sub_path = f"{path}.{key}" if path else str(key)
                if sub_path in _IGNORED_PATHS:
                    continue
                # Per-branch isolation: one malformed sub-tree never poisons
                # sibling branches.
                try:
                    self._walk(sub_value, sub_path)
                except Exception:  # noqa: BLE001
                    _LOGGER.warning(
                        "Eleven Energy Plus failed walking %s for device %s",
                        sub_path,
                        self.device_id,
                        exc_info=True,
                    )
            return

        if isinstance(value, list):
            truncated = False
            for idx, item in enumerate(value):
                if idx >= _MAX_LIST_EXPANSION:
                    truncated = True
                    break
                sub_path = f"{path}.{idx}"
                try:
                    self._walk(item, sub_path)
                except Exception:  # noqa: BLE001
                    _LOGGER.warning(
                        "Eleven Energy Plus failed walking %s for device %s",
                        sub_path,
                        self.device_id,
                        exc_info=True,
                    )
            if truncated and path not in self._truncated_list_paths:
                _LOGGER.warning(
                    "Eleven Energy Plus truncated list %s at %d entries for device %s",
                    path,
                    _MAX_LIST_EXPANSION,
                    self.device_id,
                )
                self._truncated_list_paths.add(path)
            return

        # Leaf value: shield siblings from a single bad field.
        try:
            self._handle_leaf(path, value)
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Eleven Energy Plus failed handling leaf %s for device %s",
                path,
                self.device_id,
                exc_info=True,
            )

    def _process_indexed_lists(self, payload: dict[str, Any]) -> None:
        """Route MPPT and battery list items to their curated entities.

        Batteries that appear after discovery get their entity set created on
        the fly; MPPT items beyond the model's tracker count are ignored.
        """
        for list_path in _INDEXED_LISTS:
            for _position, index, item in indexed_items(payload, list_path):
                if list_path == "battery.batteries" and not any(
                    path.startswith(f"{list_path}.{index}.")
                    for path in self.sensor_entities
                ):
                    created = self._create_indexed_entities(list_path, index)
                    _LOGGER.info(
                        "Eleven Energy Plus found battery %s on device %s",
                        index,
                        self.device_id,
                    )
                    self.controller.add_entities("sensor", created)
                for field, value in item.items():
                    entity = self.sensor_entities.get(f"{list_path}.{index}.{field}")
                    if entity is None:
                        continue
                    try:
                        entity.set_native_value(value)
                    except Exception:  # noqa: BLE001
                        _LOGGER.warning(
                            "Eleven Energy Plus failed handling %s.%s.%s for device %s",
                            list_path,
                            index,
                            field,
                            self.device_id,
                            exc_info=True,
                        )

    def _process_model(self, model: Any) -> None:
        """Update the nameplate rating entities from a ``model`` block."""
        if not isinstance(model, dict):
            return
        for field in _RATING_FIELDS:
            if field not in model:
                continue
            entity = self.sensor_entities.get(f"model.{field}")
            if entity is None:
                entity = self._create_rating_entity(field)
                self.controller.add_entities("sensor", [entity])
            entity.set_native_value(model[field])

    @staticmethod
    def _coerce_string_value(path: str, value: Any) -> Any:
        """Lowercase string values for predefined translation-keyed sensors only.

        Dynamic sensors keep their original casing so user-visible fields like
        firmware versions or error codes do not lose information.
        """
        if isinstance(value, str) and path in _LOWERCASE_STRING_PATHS:
            return value.lower()
        return value

    def _handle_leaf(self, path: str, value: Any) -> None:
        if value is None:
            return

        if path in self.sensor_entities:
            entity = self.sensor_entities[path]
            entity.set_native_value(self._coerce_string_value(path, value))
            return

        if path in self.binary_sensor_entities:
            self.binary_sensor_entities[path].set_binary_value(value)
            return

        # Unknown key: try to dynamically create an entity for it.
        meta = _infer_meta(path, value)
        if meta is None:
            return

        if self._dynamic_entity_count >= MAX_DYNAMIC_ENTITIES_PER_DEVICE:
            if not self._dynamic_cap_warned:
                _LOGGER.warning(
                    "Eleven Energy Plus reached the limit of %d auto-discovered "
                    "entities for device %s; further new fields (first: %s) "
                    "are ignored",
                    MAX_DYNAMIC_ENTITIES_PER_DEVICE,
                    self.device_id,
                    path,
                )
                self._dynamic_cap_warned = True
            return

        entity = self._create_entity_for(path, meta, auto_discovered=True)
        self._dynamic_entity_count += 1

        _LOGGER.info(
            "Eleven Energy Plus auto-discovered %s entity for %s",
            meta.get("kind"),
            path,
        )

        if isinstance(entity, InverterBinarySensorEntity):
            entity.set_binary_value(value)
            self.controller.add_entities("binary_sensor", [entity])
        else:
            entity.set_native_value(self._coerce_string_value(path, value))
            self.controller.add_entities("sensor", [entity])


class _InverterEntityMixin:
    """Behaviour shared by the inverter's sensor and binary sensor entities.

    Entities are "push-driven": values arrive synchronously from
    :meth:`HybridInverter.update`, so ``should_poll`` is False. Their
    ``entity_id`` is generated from ``device_id + entity_type`` so it stays
    stable across releases, and its domain always matches the platform the
    entity is added through.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    entity_type: str
    auto_discovered: bool
    _availability: Callable[[], bool] | None
    _invalid_value_warned: bool

    def _init_common(
        self,
        *,
        hass: HomeAssistant,
        platform_domain: str,
        device_info: DeviceInfo,
        device_id: str,
        entity_type: str,
        icon: str | None,
        category: EntityCategory | None,
        translation_key: str | None,
        translation_placeholders: dict[str, str] | None,
        name: str | None,
        availability: Callable[[], bool] | None,
        auto_discovered: bool,
    ) -> None:
        unique_id = f"{device_id}_{entity_type}"
        self.entity_type = entity_type
        self.auto_discovered = auto_discovered
        self._availability = availability
        self._invalid_value_warned = False
        self._attr_device_info = device_info
        self._attr_unique_id = unique_id
        self.entity_id = generate_entity_id(
            f"{platform_domain}.{{}}", unique_id, [], hass
        )
        if translation_key is not None:
            self._attr_translation_key = translation_key
            if translation_placeholders:
                self._attr_translation_placeholders = translation_placeholders
        if name is not None:
            self._attr_name = name
        if icon is not None:
            self._attr_icon = icon
        if category is not None:
            self._attr_entity_category = category

    @property
    def available(self) -> bool:
        """Follow the owning inverter's availability."""
        if self._availability is None:
            return True
        return self._availability()

    def write_state(self) -> None:
        """Write the current state if the entity has been added to HA.

        Entities can be constructed (and receive values) before
        ``async_add_entities`` runs; until then there is nothing to write and
        Home Assistant flushes the stored value on add.
        """
        if getattr(self, "hass", None) is None or self.entity_id is None:
            return
        try:
            self.async_write_ha_state()  # type: ignore[attr-defined]
        except RuntimeError:
            # Created but not yet (or no longer) registered with HA.
            pass

    def _warn_invalid_value(self, value: Any) -> None:
        if self._invalid_value_warned:
            return
        self._invalid_value_warned = True
        _LOGGER.warning(
            "Eleven Energy Plus received an unusable value %r for %s; "
            "reporting it as unknown",
            value,
            self.entity_id,
        )


class InverterSensorEntity(_InverterEntityMixin, SensorEntity):
    """Sensor entity bound to a single JSON leaf on the inverter.

    Used for every numeric and string field, curated and auto-discovered.
    A sensor with a unit, device class, state class or display precision is
    numeric: it only ever reports finite numbers, and anything else (a
    string, NaN, a boolean) reads as unknown instead of raising inside Home
    Assistant on every poll.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        device_info: DeviceInfo,
        device_id: str,
        entity_type: str,
        icon: str | None,
        unit_of_measurement: str | None = None,
        device_class: SensorDeviceClass | None = None,
        state_class: SensorStateClass | None = None,
        decimals: int = -1,
        category: EntityCategory | None = None,
        translation_key: str | None = None,
        name: str | None = None,
        translation_placeholders: dict[str, str] | None = None,
        availability: Callable[[], bool] | None = None,
        auto_discovered: bool = False,
    ) -> None:
        """Build the sensor.

        ``device_id + entity_type`` form the stable, unique identifier used
        for both the registry's ``unique_id`` and the auto-generated
        ``entity_id`` slug. Only one of ``translation_key`` or ``name`` is
        expected to be set: curated entities use the former, auto-discovered
        ones the latter.
        """
        self._init_common(
            hass=hass,
            platform_domain="sensor",
            device_info=device_info,
            device_id=device_id,
            entity_type=entity_type,
            icon=icon,
            category=category,
            translation_key=translation_key,
            translation_placeholders=translation_placeholders,
            name=name,
            availability=availability,
            auto_discovered=auto_discovered,
        )
        self._attr_native_unit_of_measurement = unit_of_measurement
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        if decimals >= 0:
            self._attr_suggested_display_precision = decimals
        self._numeric = (
            unit_of_measurement is not None
            or device_class is not None
            or state_class is not None
            or decimals >= 0
        )

    def set_native_value(self, new_state: Any) -> None:
        """Validate a new value from the API and push it to Home Assistant.

        Unchanged values skip the state write (saves a Recorder write on
        every poll for fields that rarely move). Values that cannot be shown
        by this sensor become ``None`` (unknown) and are warned about once.
        """
        value: Any
        if self._numeric:
            value = coerce_finite_number(new_state)
        elif isinstance(new_state, (dict, list)):
            value = None
        else:
            value = new_state
        if value is None and new_state is not None:
            self._warn_invalid_value(new_state)

        if self._attr_native_value == value:
            return

        self._attr_native_value = value
        self.write_state()


class InverterBinarySensorEntity(_InverterEntityMixin, BinarySensorEntity):
    """Binary sensor entity bound to a single boolean JSON leaf.

    Same push-driven model as :class:`InverterSensorEntity`; values arrive
    via :meth:`set_binary_value` and are parsed with :func:`parse_bool`. The
    state is unknown until the first value arrives, rather than defaulting
    to off - for the connectivity sensor "off" would wrongly claim offline.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        device_info: DeviceInfo,
        device_id: str,
        entity_type: str,
        icon: str | None,
        category: EntityCategory | None = None,
        device_class: BinarySensorDeviceClass | None = None,
        translation_key: str | None = None,
        name: str | None = None,
        translation_placeholders: dict[str, str] | None = None,
        availability: Callable[[], bool] | None = None,
        auto_discovered: bool = False,
    ) -> None:
        """Build the binary sensor; see :class:`InverterSensorEntity`."""
        self._init_common(
            hass=hass,
            platform_domain="binary_sensor",
            device_info=device_info,
            device_id=device_id,
            entity_type=entity_type,
            icon=icon,
            category=category,
            translation_key=translation_key,
            translation_placeholders=translation_placeholders,
            name=name,
            availability=availability,
            auto_discovered=auto_discovered,
        )
        self._attr_is_on = None
        self._attr_device_class = device_class

    def set_binary_value(self, new_state: Any) -> None:
        """Apply a new value from the API, parsed strictly as a boolean."""
        value = parse_bool(new_state)
        if value is None and new_state is not None:
            self._warn_invalid_value(new_state)

        if self._attr_is_on == value:
            return

        self._attr_is_on = value
        self.write_state()
