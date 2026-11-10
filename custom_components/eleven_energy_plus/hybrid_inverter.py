"""Eleven Energy hybrid-inverter device representation.

This module owns the per-device side of the integration: one
:class:`HybridInverter` instance per inverter found in the site listing
(see ``Controller._reconcile_site``), plus the two entity classes
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
  An item whose index is missing, duplicated or outside the hardware's set
  is ignored and reported once (log and diagnostics), never guessed at.

* **Auto-discovered** - anything else the API returns that
  :func:`_infer_meta` can heuristically classify (numeric, boolean, string).
  These are created lazily during :meth:`update`, registered with HA via the
  controller's deferred ``add_entities`` callbacks, capped per device at
  :data:`~.const.MAX_DYNAMIC_ENTITIES_PER_DEVICE`, and named from the dotted
  JSON path via :func:`_humanise_path`.

Plus one diagnostic **API Connection** binary sensor, which says whether the
last attempt to read this inverter from the cloud worked. It stays available
when everything else is not, because it is the explanation.

What an entity shows, in order of precedence:

* *unavailable* when its inverter is: after
  :data:`~.const.UNAVAILABLE_AFTER_FAILED_POLLS` failed polls, while the
  credential is rejected, once the inverter has left the site listing, and
  after shutdown. Telemetry is also unavailable while the inverter reports
  ``online: false``; Online, Status, Firmware Version and the nameplate
  ratings stay available, since they are what explains the outage.
* *unknown* when the latest reading has no usable value for it - the field
  is null, absent, or not a valid value of its kind. A value is never
  carried over from an older reading.
* otherwise the value. Numeric sensors only report finite numbers, binary
  sensors only unambiguous booleans, and ``total_increasing`` counters never
  report a decrease Home Assistant would misread (see
  :class:`TotalIncreasingGuard`).

The walker (:meth:`_walk`) is deliberately defensive: every leaf is handled
inside its own ``try/except`` so a single malformed field cannot poison the
entire update, ``list`` values are capped at :data:`_MAX_LIST_EXPANSION`
entries, and nesting at :data:`_MAX_DEPTH` levels.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
import logging
import re
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import (
    MAX_LENGTH_STATE_STATE,
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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity, generate_entity_id
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    MAX_DYNAMIC_ENTITIES_PER_DEVICE,
    PERCENT,
    UNAVAILABLE_AFTER_FAILED_POLLS,
)
from .parsing import coerce_finite_number, finite_float, parse_bool

if TYPE_CHECKING:
    from .controller import Controller

_LOGGER = logging.getLogger(__name__)

# Key of the API Connection binary sensor in ``binary_sensor_entities``. The
# walker skips keys starting with ``_``, so no API field can collide with it.
API_CONNECTION_PATH = "_api_connection"

# Paths that stay available while the inverter reports ``online: false``:
# they describe the machine and its connection rather than live telemetry.
_OFFLINE_EXEMPT_PATHS: frozenset[str] = frozenset(
    {"online", "status", "firmwareVersion", API_CONNECTION_PATH}
)

# Paths whose value does not come from each reading, so a reading that lacks
# them must not clear them: the nameplate ratings (``model.*``) and the API
# Connection sensor.
_STATIC_PATH_PREFIXES: tuple[str, ...] = ("model.", API_CONNECTION_PATH)

# A later reading lower than the last published value of a total_increasing
# counter is a genuine reset only if it is at most this fraction of it -
# Home Assistant's recorder applies the same 10 % rule. Smaller drops are
# glitches or corrections, which the recorder would log as "not strictly
# increasing".
_COUNTER_RESET_FRACTION = 0.9


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

# Objects and lists nested deeper than this are logged once and ignored. Real
# readings are four levels deep at most; the cap keeps a pathological payload
# from exhausting the stack.
_MAX_DEPTH = 12

# Phase labels the API uses under ``grid.phases`` / ``system.phases``.
_PHASES: tuple[str, ...] = ("L1", "L2", "L3")


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
        icon: str  # every branch below sets it
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


def device_card_name(label: str, ordinal: int) -> str:
    """Return the device card name for ``label``; ``ordinal`` tells namesakes apart."""
    name = f"Eleven Energy Plus {label}"
    return name if ordinal == 1 else f"{name} ({ordinal})"


def item_index(item: dict[str, Any]) -> int | None:
    """Return a list item's own ``index`` field, or ``None`` without a usable one.

    Only a non-negative integer identifies a tracker or battery. Falling back
    to the item's position would shift readings from one to another the
    moment the API omits an entry, so an item without a usable index is
    ignored (and reported) instead.
    """
    index = item.get("index")
    if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
        return index
    return None


def _lookup(payload: dict[str, Any], dotted: str) -> Any:
    """Return the value at a dotted path in nested dicts, or ``None``."""
    current: Any = payload
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


@dataclass(frozen=True)
class IgnoredItem:
    """An item of an indexed list that was not used, and why.

    ``key`` identifies the problem so it is reported once rather than on
    every poll; ``reason`` is the human-readable explanation.
    """

    key: str
    reason: str


def scan_indexed_items(
    payload: dict[str, Any] | None, list_path: str
) -> tuple[list[tuple[int, int, dict[str, Any]]], list[IgnoredItem]]:
    """Split an indexed list into usable ``(position, index, item)`` and ignored items.

    An item is ignored when it is not an object, has no usable index, or
    repeats an index already seen in the same list (the first one wins).
    Items beyond :data:`_MAX_LIST_EXPANSION` are ignored as a block.
    """
    usable: list[tuple[int, int, dict[str, Any]]] = []
    ignored: list[IgnoredItem] = []
    if not isinstance(payload, dict):
        return usable, ignored
    items = _lookup(payload, list_path)
    if not isinstance(items, list):
        return usable, ignored
    seen: set[int] = set()
    for position, item in enumerate(items):
        if position >= _MAX_LIST_EXPANSION:
            ignored.append(
                IgnoredItem(
                    "truncated",
                    f"more than {_MAX_LIST_EXPANSION} items; the rest are ignored",
                )
            )
            break
        if not isinstance(item, dict):
            ignored.append(
                IgnoredItem(
                    f"not_object:{type(item).__name__}",
                    f"item {position} is a {type(item).__name__}, not an object",
                )
            )
            continue
        index = item_index(item)
        if index is None:
            raw = item.get("index")
            ignored.append(
                IgnoredItem(
                    f"no_index:{raw!r}",
                    f"item {position} has no usable index ({raw!r})",
                )
            )
            continue
        if index in seen:
            ignored.append(
                IgnoredItem(
                    f"duplicate:{index}",
                    f"duplicate index {index}; only the first such item is used",
                )
            )
            continue
        seen.add(index)
        usable.append((position, index, item))
    return usable, ignored


def indexed_items(
    payload: dict[str, Any] | None, list_path: str
) -> Iterator[tuple[int, int, dict[str, Any]]]:
    """Yield ``(position, index, item)`` for the usable items of an indexed list."""
    yield from scan_indexed_items(payload, list_path)[0]


class TotalIncreasingGuard:
    """Decide which readings of a ``total_increasing`` counter to publish.

    Home Assistant treats a drop of 10 % or more in such a counter as a meter
    reset and starts a new cycle, so a single glitch to zero would double the
    day's energy on the Energy dashboard; a smaller drop is logged as "not
    strictly increasing". Neither must reach it unless it is real:

    * a reading at or above the last published value is published;
    * a negative reading is never published;
    * the first reading below the last published value reads unknown;
    * a reading at most 90 % of the last published value (a daily counter
      at midnight) is a genuine reset only when the reading before it was
      such a drop too; then it is published;
    * any other lower reading is a small correction: the last published
      value is held until the counter passes it again.

    A reset is confirmed by its own next reading only: a held correction or
    a small dip before a one-off drop to zero must not count as that
    confirmation, or the drop would be booked as a reset.

    ``reference`` is the last published value; it is seeded from the state
    restored after a restart, so the first reading after one is judged too.
    """

    def __init__(self) -> None:
        """Start with nothing published."""
        self.reference: float | None = None
        # The previous reading was below the reference (withheld or held).
        self._pending_decrease = False
        # The previous reading was a reset-sized drop, withheld.
        self._pending_reset = False

    def judge(self, value: float) -> tuple[float | None, str | None]:
        """Return ``(value to publish, problem)`` for a new reading.

        ``problem`` is ``"negative"`` or ``"decrease"`` when the reading was
        withheld, else ``None``.
        """
        if value < 0:
            return None, "negative"
        reference = self.reference
        if reference is None or value >= reference:
            self.reference = value
            self._pending_decrease = self._pending_reset = False
            return value, None
        resets = value <= reference * _COUNTER_RESET_FRACTION
        if resets and self._pending_reset:
            self.reference = value
            self._pending_decrease = self._pending_reset = False
            return value, None
        first = not self._pending_decrease
        self._pending_decrease = True
        self._pending_reset = resets
        if first or resets:
            return None, "decrease"
        return reference, None

    def restore(self, value: float) -> None:
        """Seed the reference from the value published before a restart."""
        if value >= 0 and (self.reference is None or value > self.reference):
            self.reference = value


class HybridInverter:
    """A single Eleven Energy hybrid inverter and its entity collection.

    Constructed by ``Controller._reconcile_site`` for each listed inverter.
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
        ordinal: int = 1,
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
        :param ordinal: 2 or more when another inverter on the site already
            has this label; the device card name carries it so the two can
            be told apart (see :func:`device_card_name`).
        """
        self.type = "hybridinverter"
        self.device_id = device_id
        self.device_label = device_name
        self.ordinal = ordinal
        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, self.device_id)},
            name=device_card_name(device_name, ordinal),
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

        # Availability bookkeeping. ``available`` follows the poll results;
        # the other flags each make every entity unavailable on their own.
        self.available = True
        self._consecutive_failures = 0
        self.missing = False
        self.offline = False
        self._shut_down = False
        # Whether the last attempt to read this inverter worked; ``None``
        # until the first attempt. Shown by the API Connection sensor.
        self.api_connected: bool | None = None
        # When a reading was last applied (UTC), for diagnostics.
        self.last_success_at: datetime | None = None

        # Paths that received a value from the reading being processed.
        self._seen_paths: set[str] = set()
        # Remember which list paths we have already truncated, and which
        # paths were nested too deeply, so each warning fires once per path.
        self._truncated_list_paths: set[str] = set()
        self._too_deep_paths: set[str] = set()
        # Ignored indexed-list items, keyed ``list_path:key``, with the
        # reason; each is logged once and listed in diagnostics.
        self.ignored_list_items: dict[str, str] = {}
        self._dynamic_entity_count = 0
        self._dynamic_cap_warned = False

        for path, meta in _PREDEFINED_DEFS.items():
            self._create_entity_for(path, meta)

        self._create_entity_for(
            API_CONNECTION_PATH,
            {
                "kind": "binary_sensor",
                "entity_type": "api_connection",
                "device_class": BinarySensorDeviceClass.CONNECTIVITY,
                "icon": "mdi:cloud-sync-outline",
                "category": EntityCategory.DIAGNOSTIC,
            },
        )

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
    def display_name(self) -> str:
        """Return the name of this inverter's device card."""
        return device_card_name(self.device_label, self.ordinal)

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

    def all_entities(self) -> list[InverterEntity]:
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

    def _availability_for(self, path: str) -> Callable[[], bool]:
        """Return the availability rule for the entity at ``path``."""
        if path == API_CONNECTION_PATH:
            return self.is_running
        if path in _OFFLINE_EXEMPT_PATHS or path.startswith("model."):
            return self.is_available
        return self.telemetry_available

    def _entity_kwargs(
        self, path: str, meta: dict[str, Any], auto_discovered: bool
    ) -> dict[str, Any]:
        """Return the constructor arguments both entity classes share.

        Curated definitions are named through translations (``translation_key``
        defaulting to ``entity_type`` lowercased, plus optional
        ``translation_placeholders``); auto-discovered ones carry a humanised
        ``name`` instead, so they read sensibly without a translation for
        every possible API field.
        """
        entity_type = meta["entity_type"]
        if auto_discovered:
            translation_key = None
            name = meta.get("name")
        else:
            translation_key = meta.get("translation_key", entity_type.lower())
            name = None
        return {
            "hass": self.hass,
            "device_info": self.device_info,
            "device_id": self.device_id,
            "entity_type": entity_type,
            "icon": meta.get("icon"),
            "category": meta.get("category"),
            "device_class": meta.get("device_class"),
            "translation_key": translation_key,
            "translation_placeholders": meta.get("translation_placeholders"),
            "name": name,
            "availability": self._availability_for(path),
            "auto_discovered": auto_discovered,
        }

    def _create_sensor(
        self, path: str, meta: dict[str, Any], *, auto_discovered: bool = False
    ) -> InverterSensorEntity:
        """Create the sensor for ``path`` and stash it in :attr:`sensor_entities`."""
        sensor = InverterSensorEntity(
            unit_of_measurement=meta.get("unit"),
            state_class=meta.get("state_class"),
            decimals=meta.get("decimals", -1),
            **self._entity_kwargs(path, meta, auto_discovered),
        )
        self.sensor_entities[path] = sensor
        return sensor

    def _create_binary_sensor(
        self, path: str, meta: dict[str, Any], *, auto_discovered: bool
    ) -> InverterBinarySensorEntity:
        """Create the binary sensor for ``path`` and stash it in :attr:`binary_sensor_entities`."""
        binary_sensor = InverterBinarySensorEntity(
            **self._entity_kwargs(path, meta, auto_discovered)
        )
        self.binary_sensor_entities[path] = binary_sensor
        return binary_sensor

    def _create_entity_for(
        self,
        path: str,
        meta: dict[str, Any],
        *,
        auto_discovered: bool = False,
    ) -> InverterEntity:
        """Create the entity a meta dict describes (``kind`` picks the platform)."""
        if meta["kind"] == "binary_sensor":
            return self._create_binary_sensor(path, meta, auto_discovered=auto_discovered)
        return self._create_sensor(path, meta, auto_discovered=auto_discovered)

    def _create_phase_entities(self, group: str) -> list[InverterSensorEntity]:
        """Create per-phase power, voltage and current entities for ``group``."""
        settings = _PHASE_GROUPS[group]
        created: list[InverterSensorEntity] = []
        for phase in _PHASES:
            for field, field_meta in _PHASE_FIELDS.items():
                meta = {
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
                created.append(self._create_sensor(f"{group}.phases.{phase}.{field}", meta))
        return created

    def _create_indexed_entities(
        self, list_path: str, index: int
    ) -> list[InverterSensorEntity]:
        """Create the curated entities for one MPPT / battery ``index``."""
        created: list[InverterSensorEntity] = []
        for field, field_meta in _INDEXED_LISTS[list_path].items():
            path = f"{list_path}.{index}.{field}"
            meta = {
                **field_meta,
                "entity_type": field_meta["entity_type"].format(index=index),
                "translation_placeholders": {"index": str(index)},
                "category": EntityCategory.DIAGNOSTIC,
            }
            created.append(self._create_sensor(path, meta))
        return created

    def _create_rating_entity(self, field: str) -> InverterSensorEntity:
        """Create the nameplate rating entity for ``model.<field>``."""
        rating = _RATING_FIELDS[field]
        meta = {
            "entity_type": rating["entity_type"],
            "translation_key": rating["translation_key"],
            "unit": UnitOfPower.KILO_WATT,
            "device_class": SensorDeviceClass.POWER,
            "decimals": 1,
            "icon": rating["icon"],
            "category": EntityCategory.DIAGNOSTIC,
        }
        return self._create_sensor(f"model.{field}", meta)

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return whether this inverter's entities may show values at all.

        False after :data:`~.const.UNAVAILABLE_AFTER_FAILED_POLLS` failed
        polls, while the credential is rejected, once the inverter has left
        the site listing, and after shutdown.
        """
        return self.available and not self.missing and not self._shut_down

    def telemetry_available(self) -> bool:
        """Return whether live telemetry may be shown (also not offline)."""
        return self.is_available() and not self.offline

    def is_running(self) -> bool:
        """Return False once the integration has shut this inverter down."""
        return not self._shut_down

    def record_poll_success(self) -> None:
        """Reset the failure counter and become available again."""
        self._consecutive_failures = 0
        self.last_success_at = dt_util.utcnow()
        self._set_api_connected(True)
        self._set_available(True)

    def record_poll_failure(self) -> None:
        """Count a failed poll; go unavailable once the threshold is reached."""
        self._consecutive_failures += 1
        self._set_api_connected(False)
        if self._consecutive_failures >= UNAVAILABLE_AFTER_FAILED_POLLS:
            self._set_available(False)

    def mark_unavailable(self) -> None:
        """Go unavailable immediately (e.g. the credential was rejected).

        Only the next successful poll makes the inverter available again.
        """
        self._set_api_connected(False)
        self._set_available(False)

    def set_missing(self, missing: bool) -> None:
        """Record whether the inverter has left the site listing.

        Leaving it makes the inverter unavailable, as :meth:`mark_unavailable`
        does, so coming back does not revive the values from before: only a
        fresh reading does.
        """
        if self.missing == missing:
            return
        self.missing = missing
        _LOGGER.log(
            logging.INFO if not missing else logging.WARNING,
            "Eleven Energy Plus inverter %s is %s the site listing",
            self.device_id,
            "missing from" if missing else "back in",
        )
        if missing:
            self._set_api_connected(False)
            self.available = False
        self._write_all_states()

    def shut_down(self) -> None:
        """Make every entity unavailable for good: nothing will update them."""
        if self._shut_down:
            return
        self._shut_down = True
        self._write_all_states()

    def _set_api_connected(self, connected: bool) -> None:
        self.api_connected = connected
        self.binary_sensor_entities[API_CONNECTION_PATH].set_binary_value(connected)

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
        self._write_all_states()

    def _set_offline(self, offline: bool) -> None:
        if self.offline == offline:
            return
        self.offline = offline
        _LOGGER.log(
            logging.INFO if not offline else logging.WARNING,
            "Eleven Energy Plus inverter %s reports it is %s; its telemetry is %s",
            self.device_id,
            "offline" if offline else "online again",
            "unavailable until it is back" if offline else "shown again",
        )
        self._write_all_states()

    def _write_all_states(self) -> None:
        for entity in self.all_entities():
            entity.write_state()

    # ------------------------------------------------------------------
    # Payload processing
    # ------------------------------------------------------------------

    def update(self, payload: Any) -> None:
        """Apply one device reading.

        Every entity whose field the reading does not supply - null, absent,
        or its whole block absent - becomes unknown, so nothing shows a value
        from an older reading. The nameplate ratings are static and keep
        their value. ``online: false`` makes the telemetry unavailable.
        """
        if not isinstance(payload, dict):
            _LOGGER.warning(
                "Eleven Energy Plus ignoring non-object payload for device %s: %s",
                self.device_id,
                type(payload).__name__,
            )
            return
        if self.discovery_state is None:
            self._complete_hardware(payload)
        self._seen_paths = set()
        self._walk(payload, "")
        self._process_indexed_lists(payload)
        self._process_model(payload.get("model"))
        self._clear_unseen()
        self._set_offline(parse_bool(payload.get("online")) is False)

    def _complete_hardware(self, reading: dict[str, Any]) -> None:
        """Learn the hardware from the first reading when discovery could not.

        An inverter whose discovery read failed is built from its listing
        alone. Without a ``model`` block there, which phase and tracker
        entities it needs is unknown; its first reading settles that, as
        discovery would have, and the entities are registered at once.
        """
        self.discovery_state = reading
        model = reading.get("model")
        if self.model_known or not isinstance(model, dict) or not model:
            return
        self.model = dict(model)
        created: list[InverterSensorEntity] = []
        if self.three_phase:
            for group in _PHASE_GROUPS:
                created.extend(self._create_phase_entities(group))
        for index in range(1, self.mppt_count + 1):
            created.extend(self._create_indexed_entities("pv.mppts", index))
        _LOGGER.info(
            "Eleven Energy Plus learnt the hardware of device %s from its "
            "first reading (%s phase, %d MPPT)",
            self.device_id,
            3 if self.three_phase else 1,
            self.mppt_count,
        )
        self.controller.add_entities("sensor", created)

    def _clear_unseen(self) -> None:
        """Make every entity the latest reading did not supply unknown."""
        for path, sensor in self.sensor_entities.items():
            if path not in self._seen_paths and not path.startswith(_STATIC_PATH_PREFIXES):
                sensor.set_native_value(None)
        for path, binary_sensor in self.binary_sensor_entities.items():
            if path not in self._seen_paths and not path.startswith(_STATIC_PATH_PREFIXES):
                binary_sensor.set_binary_value(None)

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

    def _walk(self, value: Any, path: str, depth: int = 0) -> None:
        if path and self._is_suppressed(path, value):
            return

        if isinstance(value, (dict, list)) and depth >= _MAX_DEPTH:
            if path not in self._too_deep_paths:
                self._too_deep_paths.add(path)
                _LOGGER.warning(
                    "Eleven Energy Plus ignoring %s for device %s: nested more "
                    "than %d levels deep",
                    path,
                    self.device_id,
                    _MAX_DEPTH,
                )
            return

        if isinstance(value, dict):
            for key, sub_value in value.items():
                # Skip private-looking keys and keys on the global ignore-list.
                if isinstance(key, str) and key.startswith("_"):
                    continue
                sub_path = f"{path}.{key}" if path else str(key)
                if sub_path in _IGNORED_PATHS:
                    continue
                self._walk(sub_value, sub_path, depth + 1)
            return

        if isinstance(value, list):
            truncated = False
            for idx, item in enumerate(value):
                if idx >= _MAX_LIST_EXPANSION:
                    truncated = True
                    break
                self._walk(item, f"{path}.{idx}", depth + 1)
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
        except Exception:
            _LOGGER.warning(
                "Eleven Energy Plus failed handling leaf %s for device %s",
                path,
                self.device_id,
                exc_info=True,
            )

    def _ignore_list_item(self, list_path: str, item: IgnoredItem) -> None:
        """Record an unusable indexed-list item, warning once per problem."""
        key = f"{list_path}:{item.key}"
        if key in self.ignored_list_items:
            return
        self.ignored_list_items[key] = item.reason
        _LOGGER.warning(
            "Eleven Energy Plus ignoring a %s item for device %s: %s",
            list_path,
            self.device_id,
            item.reason,
        )

    def _process_indexed_lists(self, payload: dict[str, Any]) -> None:
        """Route MPPT and battery list items to their curated entities.

        Batteries that appear after discovery get their entity set created on
        the fly. An MPPT index outside the model's trackers, and any item
        :func:`scan_indexed_items` cannot use, is ignored and reported once.
        """
        for list_path in _INDEXED_LISTS:
            usable, ignored = scan_indexed_items(payload, list_path)
            for problem in ignored:
                self._ignore_list_item(list_path, problem)
            for _position, index, item in usable:
                if list_path == "pv.mppts" and not 1 <= index <= self.mppt_count:
                    self._ignore_list_item(
                        list_path,
                        IgnoredItem(
                            f"out_of_range:{index}",
                            f"MPPT index {index} is not one of this model's "
                            f"{self.mppt_count} trackers"
                            if self.model_known
                            else f"MPPT index {index} cannot be placed: the model is unknown",
                        ),
                    )
                    continue
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
                    path = f"{list_path}.{index}.{field}"
                    entity = self.sensor_entities.get(path)
                    if entity is None or value is None:
                        continue
                    self._seen_paths.add(path)
                    entity.set_native_value(value)

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
        self._seen_paths.add(path)

        if path in self.sensor_entities:
            self.sensor_entities[path].set_native_value(
                self._coerce_string_value(path, value)
            )
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
            # Only predefined paths are lowercased, so a new field is not.
            entity.set_native_value(value)
            self.controller.add_entities("sensor", [entity])


class _InverterEntityMixin(Entity):
    """Behaviour shared by the inverter's sensor and binary sensor entities.

    Entities are "push-driven": values arrive synchronously from
    :meth:`HybridInverter.update`, so ``should_poll`` is False. Their
    ``entity_id`` is generated from ``device_id + entity_type`` so it stays
    stable across releases, and its domain always matches the platform the
    entity is added through.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

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
        """Follow the owning inverter's availability rule for this entity."""
        if self._availability is None:
            return True
        return self._availability()

    def write_state(self) -> None:
        """Write the current state if the entity has been added to HA.

        Entities can be constructed (and receive values) before
        ``async_add_entities`` runs; until then ``hass`` is unset, there is
        nothing to write, and Home Assistant writes the value when it adds
        the entity. Writes after removal are ignored by Home Assistant.
        """
        if getattr(self, "hass", None) is None:
            return
        self.async_write_ha_state()

    @callback
    def async_entity_id_changed(self, old_entity_id: str) -> None:
        """Opt in to in-place ``entity_id`` changes (Home Assistant 2026.11+).

        Nothing here is keyed on the ``entity_id``, so a rename needs no
        work; defining the method is what opts in.
        """

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


class InverterSensorEntity(_InverterEntityMixin, RestoreSensor):
    """Sensor entity bound to a single JSON leaf on the inverter.

    Used for every numeric and string field, curated and auto-discovered.
    A sensor with a unit, device class, state class or display precision is
    numeric: it only ever reports finite numbers, and anything else (a
    string, NaN, a boolean) reads as unknown instead of raising inside Home
    Assistant on every poll. A ``total_increasing`` sensor additionally
    passes every reading through a :class:`TotalIncreasingGuard`, seeded
    from the value it had before a restart.
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
        self._counter = (
            TotalIncreasingGuard()
            if state_class == SensorStateClass.TOTAL_INCREASING
            else None
        )
        self._counter_warnings: set[str] = set()

    async def async_added_to_hass(self) -> None:
        """Seed a counter's guard with the value it had before the restart.

        The first reading after a restart has already been applied by the
        time the entity is added, so it is judged again against the restored
        value: a reading below it reads unknown until confirmed.
        """
        await super().async_added_to_hass()
        counter = self._counter
        if counter is None:
            return
        last = await self.async_get_last_sensor_data()
        restored = finite_float(last.native_value) if last is not None else None
        if restored is None:
            return
        counter.restore(restored)
        current = self._attr_native_value
        if isinstance(current, (int, float)):
            self._attr_native_value = self._judge_counter(counter, float(current))

    def _judge_counter(self, counter: TotalIncreasingGuard, value: float) -> float | None:
        """Pass a counter reading through ``counter``, warning once per problem."""
        reference = counter.reference
        published, problem = counter.judge(value)
        if problem is not None and problem not in self._counter_warnings:
            self._counter_warnings.add(problem)
            _LOGGER.warning(
                "Eleven Energy Plus withheld a %s reading of %s for %s (last "
                "reported %s); it reads unknown so the Energy dashboard does "
                "not record a false meter reset",
                problem,
                value,
                self.entity_id,
                reference,
            )
        return published

    def set_native_value(self, new_state: Any) -> None:
        """Validate a new value from the API and push it to Home Assistant.

        Unchanged values skip the state write (saves a Recorder write on
        every poll for fields that rarely move). Values that cannot be shown
        by this sensor become ``None`` (unknown) and are warned about once.
        ``None`` itself means "no value in this reading".
        """
        value: Any
        if self._numeric:
            value = coerce_finite_number(new_state)
        elif isinstance(new_state, (dict, list)) or (
            # Home Assistant would replace it with unknown and log an error
            # on every change.
            isinstance(new_state, str) and len(new_state) > MAX_LENGTH_STATE_STATE
        ):
            value = None
        else:
            value = new_state
        if value is None and new_state is not None:
            self._warn_invalid_value(new_state)
        if value is not None and self._counter is not None:
            value = self._judge_counter(self._counter, float(value))

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


type InverterEntity = InverterSensorEntity | InverterBinarySensorEntity
