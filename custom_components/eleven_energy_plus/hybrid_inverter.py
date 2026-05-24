"""Eleven Energy hybrid-inverter device representation.

This module owns the per-device side of the integration: one
:class:`HybridInverter` instance per inverter discovered by
:meth:`Controller.poll_site`, plus the two entity classes
(:class:`InverterSensorEntity` / :class:`InverterBinarySensorEntity`)
that make their values visible to Home Assistant.

The inverter has two streams of entities:

* **Predefined** - hand-curated entries declared in :data:`_PREDEFINED_DEFS`
  with stable ``entity_type`` slugs, units, device classes, and
  translation keys (the user-visible names come from ``translations/en.json``).
  These are created up front in ``__init__`` so the sensor / binary sensor /
  number platforms can hand them straight to ``async_add_entities`` during
  their initial setup.

* **Auto-discovered** - anything else the API returns that
  :func:`_infer_meta` can heuristically classify (numeric, boolean, string).
  These are created lazily during :meth:`update` and registered with HA via
  the controller's deferred ``add_entities`` callbacks. Their names are
  derived from the dotted JSON path via :func:`_humanise_path` rather than a
  translation key, so they read sensibly without needing every possible
  field to be added to ``en.json``.

The walker (:meth:`_walk`) is deliberately defensive: every recursion and
every leaf write is wrapped in ``try/except`` so a single malformed field
cannot poison the entire update, and ``list`` values are capped at
:data:`_MAX_LIST_EXPANSION` entries to keep pathological payloads from
creating thousands of entities.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

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
    PERCENTAGE,
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

from .const import DOMAIN

if TYPE_CHECKING:
    from .controller import Controller

_LOGGER = logging.getLogger(__name__)


# Keys we never want to expose as entities (identifiers and type tags).
# ``firmwareVersion`` used to live here but is now surfaced as a diagnostic
# sensor; users want to see it for breaking-change visibility.
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


# Hand-tuned definitions for the well-known fields. Keys are dotted JSON paths.
# The values match the keyword arguments expected by ``InverterSensorEntity``
# (with ``kind`` discriminating sensor vs binary_sensor).
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
        "unit": PERCENTAGE,
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
            unit = PERCENTAGE
            icon = "mdi:heart-pulse"
            decimals = 0
        elif (
            "stateofcharge" in last_lower
            or last_lower == "soc"
            or last_lower.endswith("pc")
            or "percent" in last_lower
        ):
            unit = PERCENTAGE
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


class HybridInverter:
    """A single Eleven Energy hybrid inverter and its entity collection.

    Constructed by :meth:`Controller.poll_site` for each discovered device.
    Two entity dictionaries are kept, both keyed by the dotted JSON path of
    the leaf the entity represents (e.g. ``pv.power``,
    ``operatingMode.workMode``, or for auto-discovered fields
    ``pvStrings.0.power``):

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
    ) -> None:
        """Create an inverter wrapper and its predefined entities.

        :param hass: HA core, forwarded to entities for ``entity_id`` slug
            generation via :func:`generate_entity_id`.
        :param controller: parent :class:`Controller`; used to register any
            entities that get auto-discovered later.
        :param device_id: Eleven Energy API device id. Stored as the second
            element of ``DeviceInfo.identifiers`` so device-targeted services
            can map back to us.
        :param device_name: User-visible model/name string from the API. We
            prefix it with ``"Eleven Energy Plus "`` so multi-vendor sites
            show the integration brand in the HA UI *and* the fork's device
            cards are visually distinct from the original integration's, in
            case both are installed side by side.
        :param device_serial_number: Inverter serial; surfaced on the HA
            device card.
        """
        self.type = "hybridinverter"
        self.device_id = device_id
        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, self.device_id)},
            name=f"Eleven Energy Plus {device_name}",
            manufacturer="Eleven Energy",
            model=device_name,
            serial_number=device_serial_number,
        )
        self.hass = hass
        self.controller = controller

        self.sensor_entities: dict[str, InverterSensorEntity] = {}
        self.binary_sensor_entities: dict[str, InverterBinarySensorEntity] = {}
        # Remember which list paths we have already truncated so the
        # truncation warning fires at most once per path per process.
        self._truncated_list_paths: set[str] = set()

        # Build the predefined entities up front so the platforms can hand
        # them straight to ``async_add_entities`` during their initial
        # setup, before any device polling has happened.
        for path, meta in _PREDEFINED_DEFS.items():
            self._create_entity_for(path, meta, use_translation_key=True)

    def _create_entity_for(
        self,
        path: str,
        meta: dict[str, Any],
        *,
        use_translation_key: bool,
    ) -> InverterSensorEntity | InverterBinarySensorEntity | None:
        """Instantiate an entity from a meta dict and stash it under ``path``.

        :param path: dotted JSON path key in ``sensor_entities`` /
            ``binary_sensor_entities``.
        :param meta: descriptor dict, either from :data:`_PREDEFINED_DEFS`
            (curated path) or :func:`_infer_meta` (auto-discovered path).
        :param use_translation_key: when ``True`` (predefined fields), the
            entity's display name comes from ``translations/<lang>.json`` via
            ``_attr_translation_key``. When ``False`` (auto-discovered
            fields), :func:`_humanise_path` has already filled ``meta["name"]``
            and we set ``_attr_name`` directly so the user sees a sensible
            label without having to ship a translation for every possible
            API field.
        """
        kind = meta.get("kind", "sensor")
        entity_type = meta["entity_type"]
        translation_key = entity_type.lower() if use_translation_key else None
        name = meta.get("name") if not use_translation_key else None

        if kind == "binary_sensor":
            entity = InverterBinarySensorEntity(
                hass=self.hass,
                device_info=self.device_info,
                device_id=self.device_id,
                entity_type=entity_type,
                icon=meta.get("icon"),
                category=meta.get("category"),
                device_class=meta.get("device_class"),
                translation_key=translation_key,
                name=name,
            )
            self.binary_sensor_entities[path] = entity
            return entity

        entity = InverterSensorEntity(
            hass=self.hass,
            device_info=self.device_info,
            device_id=self.device_id,
            entity_type=entity_type,
            icon=meta.get("icon"),
            unit_of_measurement=meta.get("unit"),
            device_class=meta.get("device_class"),
            state_class=meta.get("state_class"),
            decimals=meta.get("decimals", -1),
            category=meta.get("category"),
            translation_key=translation_key,
            name=name,
        )
        self.sensor_entities[path] = entity
        return entity

    def update(self, payload: Any) -> None:
        """Walk the device payload and push values into the right entities."""
        self._walk(payload, "")

    def _walk(self, value: Any, path: str) -> None:
        if path in _IGNORED_PATHS:
            return

        if isinstance(value, dict):
            for key, sub_value in value.items():
                # Skip private-looking keys and keys on the global ignore-list.
                if isinstance(key, str) and key.startswith("_"):
                    continue
                sub_path = f"{path}.{key}" if path else key
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
            self.binary_sensor_entities[path].set_binary_value(bool(value))
            return

        # Unknown key: try to dynamically create an entity for it.
        meta = _infer_meta(path, value)
        if meta is None:
            return

        entity = self._create_entity_for(path, meta, use_translation_key=False)
        if entity is None:
            return

        _LOGGER.info(
            "Eleven Energy Plus auto-discovered %s entity for %s",
            meta.get("kind"),
            path,
        )

        if isinstance(entity, InverterBinarySensorEntity):
            entity.set_binary_value(bool(value))
            self.controller.add_entities("binary_sensor", [entity])
        else:
            entity.set_native_value(self._coerce_string_value(path, value))
            self.controller.add_entities("sensor", [entity])


class InverterSensorEntity(SensorEntity):
    """Sensor entity bound to a single JSON leaf on the inverter.

    Used for every numeric and string field, both predefined and
    auto-discovered. The entity is "push-driven": its value is updated
    synchronously from :meth:`HybridInverter._handle_leaf` via
    :meth:`set_native_value`, which is why ``_attr_should_poll`` is False.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

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
    ) -> None:
        """Build the sensor.

        ``device_id + entity_type`` form the stable, unique identifier used
        for both the registry's ``unique_id`` and the auto-generated
        ``entity_id`` slug. Only one of ``translation_key`` or ``name`` is
        expected to be set: predefined entities use the former, auto-discovered
        ones the latter.
        """
        unique_id = f"{device_id}_{entity_type}"
        self._attr_device_info = device_info
        self._attr_unique_id = unique_id
        self.entity_id = generate_entity_id("sensor.{}", unique_id, [], hass)

        if translation_key is not None:
            self._attr_translation_key = translation_key
        if name is not None:
            self._attr_name = name

        self._attr_native_unit_of_measurement = unit_of_measurement
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        if icon is not None:
            self._attr_icon = icon

        if category is not None:
            self._attr_entity_category = category

        if decimals >= 0:
            self._attr_suggested_display_precision = decimals

    def set_native_value(self, new_state: Any) -> None:
        """Apply a new value from the API and push it to Home Assistant.

        Three short-circuits keep this cheap and safe:

        1. If the value is unchanged we skip the registry write entirely
           (saves a Recorder write on every poll for fields that rarely move).
        2. If the entity hasn't been bound to ``hass`` yet (it can be
           constructed before ``async_add_entities`` runs), we only update
           ``_attr_native_value`` and rely on HA to flush it on add.
        3. ``async_write_ha_state`` raises ``RuntimeError`` while the entity
           is created-but-not-yet-added; we swallow it for the same reason.
        """
        if self._attr_native_value == new_state:
            return

        self._attr_native_value = new_state

        if self.hass is None or self.entity_id is None:
            return
        try:
            self.async_write_ha_state()
        except RuntimeError:
            # Entity has been created but is not yet registered with Home Assistant.
            # The initial value will be written when the entity is added.
            pass


class InverterBinarySensorEntity(BinarySensorEntity):
    """Binary sensor entity bound to a single boolean JSON leaf.

    Same push-driven model as :class:`InverterSensorEntity`; values arrive
    via :meth:`set_binary_value` instead of ``set_native_value``.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

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
    ) -> None:
        """Build the binary sensor; see :class:`InverterSensorEntity` for the
        shared semantics around ``device_id + entity_type``, ``translation_key``
        and ``name``."""
        unique_id = f"{device_id}_{entity_type}"
        self._attr_device_info = device_info
        self.entity_id = generate_entity_id("binary_sensor.{}", unique_id, [], hass)

        if translation_key is not None:
            self._attr_translation_key = translation_key
        if name is not None:
            self._attr_name = name
        self._attr_unique_id = unique_id

        self._attr_is_on = False

        self._attr_device_class = device_class
        if icon is not None:
            self._attr_icon = icon

        if category is not None:
            self._attr_entity_category = category

    def set_binary_value(self, new_state: bool) -> None:
        """Apply a new boolean value from the API.

        Uses the same change-detect-then-write pattern as
        :meth:`InverterSensorEntity.set_native_value` (see that method for
        the rationale behind each short-circuit).
        """
        if self._attr_is_on == new_state:
            return

        self._attr_is_on = new_state

        if self.hass is None or self.entity_id is None:
            return
        try:
            self.async_write_ha_state()
        except RuntimeError:
            # Entity created but not yet registered; initial value will be
            # written when ``async_add_entities`` fires.
            pass
