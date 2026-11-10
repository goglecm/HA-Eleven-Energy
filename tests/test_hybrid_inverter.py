"""Tests for the :mod:`custom_components.eleven_energy_plus.hybrid_inverter` module."""

from __future__ import annotations

import logging
import math
from typing import Any
from unittest.mock import MagicMock

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from custom_components.eleven_energy_plus import hybrid_inverter as hi
from custom_components.eleven_energy_plus.const import UNAVAILABLE_AFTER_FAILED_POLLS
from custom_components.eleven_energy_plus.hybrid_inverter import (
    _MAX_LIST_EXPANSION,
    HybridInverter,
    InverterBinarySensorEntity,
    InverterSensorEntity,
)

from .common import MODEL_SINGLE_PHASE, MODEL_THREE_PHASE_DUAL, device_field

_LOGGER_NAME = "custom_components.eleven_energy_plus.hybrid_inverter"


def _make(
    hass: HomeAssistant,
    *,
    model: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> HybridInverter:
    controller = MagicMock()
    controller.add_entities = MagicMock()
    return HybridInverter(
        hass,
        controller,
        "abc123",
        "Hybrid",
        "SN-1",
        model=model,
        state=state,
    )


@pytest.fixture
def inverter(hass: HomeAssistant) -> HybridInverter:
    """Return a HybridInverter with no model information (legacy portal)."""
    return _make(hass)


def _attach(entity: Any, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Pretend ``entity`` is added to HA and record its state writes."""
    entity.hass = hass
    entity.platform = MagicMock()
    writes: list[Any] = []
    monkeypatch.setattr(
        type(entity),
        "async_write_ha_state",
        lambda self: writes.append(
            getattr(self, "_attr_native_value", getattr(self, "_attr_is_on", None))
        ),
    )
    return writes


class TestPredefinedEntities:
    """Predefined entities are created up front so platforms can register them."""

    @pytest.mark.covers("FC-ENT-01")
    def test_predefined_sensors_created(self, inverter: HybridInverter) -> None:
        assert "pv.power" in inverter.sensor_entities
        assert "battery.stateOfCharge" in inverter.sensor_entities
        assert "operatingMode.workMode" in inverter.sensor_entities
        assert "firmwareVersion" in inverter.sensor_entities

    @pytest.mark.covers("FC-ENT-01")
    def test_predefined_binary_sensor_created(
        self, inverter: HybridInverter
    ) -> None:
        assert "online" in inverter.binary_sensor_entities

    def test_device_info_uses_provided_name_and_serial(
        self, inverter: HybridInverter
    ) -> None:
        info = inverter.device_info
        assert device_field(info, "name") == "Eleven Energy Plus Hybrid"
        assert device_field(info, "model") == "Hybrid"
        assert device_field(info, "serial_number") == "SN-1"
        # Manufacturer keeps the hardware vendor's brand.
        assert device_field(info, "manufacturer") == "Eleven Energy"

    @pytest.mark.covers("FC-ENT-09")
    def test_entity_ids_match_platform_domain(self, inverter: HybridInverter) -> None:
        """Entity ids must never be minted in the wrong domain (stops in 2027.5)."""
        for entity in inverter.sensor_entities.values():
            assert entity.entity_id.startswith("sensor.")
        for entity in inverter.binary_sensor_entities.values():
            assert entity.entity_id.startswith("binary_sensor.")

    @pytest.mark.covers("FC-VAL-02")
    def test_binary_sensor_starts_unknown(self, inverter: HybridInverter) -> None:
        """Before the first reading, Online must not claim "off" (offline)."""
        assert inverter.binary_sensor_entities["online"].is_on is None

    @pytest.mark.covers("FC-ENT-05")
    def test_no_model_entities_without_model(self, inverter: HybridInverter) -> None:
        assert inverter.mppt_count == 0
        assert not inverter.three_phase
        assert not any(
            path.startswith(("pv.mppts.", "battery.batteries.", "model.", "grid.phases."))
            for path in inverter.sensor_entities
        )


class TestWalkLeafRouting:
    """The walker pushes values into the right pre-existing entities."""

    @pytest.mark.covers("FC-VAL-01")
    def test_known_numeric_path_updates_sensor(
        self, inverter: HybridInverter
    ) -> None:
        inverter.update({"pv": {"power": 1.23}})
        assert inverter.sensor_entities["pv.power"].native_value == 1.23

    @pytest.mark.covers("FC-VAL-07")
    def test_known_string_lowercase_path_lowercased(
        self, inverter: HybridInverter
    ) -> None:
        inverter.update({"status": "OnGrid"})
        assert inverter.sensor_entities["status"].native_value == "ongrid"

    @pytest.mark.covers("FC-ENT-06")
    def test_dynamic_string_keeps_case(self, inverter: HybridInverter) -> None:
        inverter.update({"hardwareVersion": "Rev-B-2"})
        path = "hardwareVersion"
        assert path in inverter.sensor_entities
        assert inverter.sensor_entities[path].native_value == "Rev-B-2"
        assert inverter.sensor_entities[path].auto_discovered

    def test_known_binary_path_updates_binary_sensor(
        self, inverter: HybridInverter
    ) -> None:
        inverter.update({"online": True})
        assert inverter.binary_sensor_entities["online"].is_on is True

    @pytest.mark.covers("FC-ENT-01")
    def test_firmware_version_is_exposed(self, inverter: HybridInverter) -> None:
        inverter.update({"firmwareVersion": "v1.2.3"})
        assert inverter.sensor_entities["firmwareVersion"].native_value == "v1.2.3"

    def test_non_dict_payload_is_ignored(
        self, inverter: HybridInverter, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
        inverter.update(["not", "a", "dict"])
        inverter.update(None)
        assert any("non-object payload" in r.message for r in caplog.records)


class TestValueValidation:
    """Entities only report values Home Assistant can trust."""

    @pytest.mark.parametrize("bad", ["N/A", math.nan, math.inf, True, "", {"x": 1}])
    @pytest.mark.covers("FC-VAL-01")
    def test_numeric_sensor_rejects_unusable_values(
        self, inverter: HybridInverter, bad: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        sensor = inverter.sensor_entities["pv.power"]
        sensor.set_native_value(2.0)
        caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
        sensor.set_native_value(bad)
        assert sensor.native_value is None
        assert any("unusable value" in r.message for r in caplog.records)

    @pytest.mark.covers("FC-VAL-01")
    def test_numeric_sensor_accepts_numeric_strings(
        self, inverter: HybridInverter
    ) -> None:
        sensor = inverter.sensor_entities["pv.power"]
        sensor.set_native_value("2.5")
        assert sensor.native_value == 2.5

    @pytest.mark.covers("FC-VAL-01")
    def test_invalid_value_warned_once(
        self, inverter: HybridInverter, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
        sensor = inverter.sensor_entities["pv.power"]
        sensor.set_native_value("bad")
        sensor.set_native_value(1.0)
        sensor.set_native_value("bad")
        assert sum("unusable value" in r.message for r in caplog.records) == 1

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("false", False), ("true", True), (0, False), (1, True), ("maybe", None)],
    )
    @pytest.mark.covers("FC-VAL-02")
    def test_binary_sensor_parses_strictly(
        self, inverter: HybridInverter, raw: Any, expected: bool | None
    ) -> None:
        """Regression: the string "false" used to read as on."""
        inverter.update({"online": raw})
        assert inverter.binary_sensor_entities["online"].is_on is expected

    def test_string_sensor_keeps_strings(self, inverter: HybridInverter) -> None:
        sensor = inverter.sensor_entities["firmwareVersion"]
        sensor.set_native_value("1.0")
        assert sensor.native_value == "1.0"
        sensor.set_native_value(["list"])
        assert sensor.native_value is None


class TestWalkSkipping:
    """Ignored paths and underscore-prefixed keys must never produce entities."""

    def test_ignored_device_id_path(self, inverter: HybridInverter) -> None:
        before = set(inverter.sensor_entities)
        inverter.update({"deviceId": "abc", "type": "hybridinverter"})
        assert set(inverter.sensor_entities) == before

    def test_underscore_prefixed_keys_are_skipped(
        self, inverter: HybridInverter
    ) -> None:
        before = set(inverter.sensor_entities)
        inverter.update({"_private": {"power": 99}, "_internal_flag": True})
        assert set(inverter.sensor_entities) == before

    def test_serial_number_path_skipped(self, inverter: HybridInverter) -> None:
        before = set(inverter.sensor_entities)
        inverter.update({"serialNumber": "SHOULD-BE-IGNORED"})
        assert set(inverter.sensor_entities) == before

    def test_model_block_is_never_auto_discovered(
        self, inverter: HybridInverter
    ) -> None:
        inverter.update({"model": {"name": "X", "threePhase": False, "other": 1}})
        assert not any(path.startswith("model.") and path not in (
            "model.maxAcPower", "model.maxDcPower") for path in inverter.sensor_entities)
        assert not any(path.startswith("model.") for path in inverter.binary_sensor_entities)


class TestLeafIsolation:
    """A single bad leaf must not poison sibling entities."""

    def test_failing_leaf_does_not_block_siblings(
        self, inverter: HybridInverter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = InverterSensorEntity.set_native_value

        def patched(self: InverterSensorEntity, new_state) -> None:
            if self._attr_unique_id == "abc123_pv_power":
                raise RuntimeError("boom")
            original(self, new_state)

        monkeypatch.setattr(InverterSensorEntity, "set_native_value", patched)

        inverter.update(
            {
                "pv": {"power": 5},
                "battery": {"stateOfCharge": 80},
                "load": {"power": 1.5},
            }
        )

        assert (
            inverter.sensor_entities["battery.stateOfCharge"].native_value == 80
        )
        assert inverter.sensor_entities["load.power"].native_value == 1.5

    def test_failing_branch_logs_warning(
        self,
        inverter: HybridInverter,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        original = InverterSensorEntity.set_native_value

        def patched(self: InverterSensorEntity, new_state) -> None:
            if self._attr_unique_id == "abc123_pv_power":
                raise RuntimeError("boom")
            original(self, new_state)

        monkeypatch.setattr(InverterSensorEntity, "set_native_value", patched)
        caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
        inverter.update({"pv": {"power": 5}})

        assert any(
            "failed handling leaf pv.power" in record.message
            for record in caplog.records
        )


class TestListWalking:
    """Generic lists are expanded with index-based paths and capped."""

    @pytest.mark.covers("FC-ENT-08")
    def test_list_expands_to_indexed_paths(
        self, inverter: HybridInverter
    ) -> None:
        inverter.update({"pvStrings": [{"power": 1.1}, {"power": 2.2}]})
        assert inverter.sensor_entities["pvStrings.0.power"].native_value == 1.1
        assert inverter.sensor_entities["pvStrings.1.power"].native_value == 2.2

    @pytest.mark.covers("FC-ENT-08")
    def test_list_truncates_beyond_max(
        self, inverter: HybridInverter, caplog: pytest.LogCaptureFixture
    ) -> None:
        oversized = [{"v": i} for i in range(_MAX_LIST_EXPANSION + 5)]
        caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
        inverter.update({"items": oversized})

        assert f"items.{_MAX_LIST_EXPANSION - 1}.v" in inverter.sensor_entities
        assert f"items.{_MAX_LIST_EXPANSION}.v" not in inverter.sensor_entities
        assert any("truncated list items" in r.message for r in caplog.records)

    @pytest.mark.covers("FC-ENT-07")
    def test_dynamic_entity_cap(
        self,
        hass: HomeAssistant,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(hi, "MAX_DYNAMIC_ENTITIES_PER_DEVICE", 3)
        inverter = _make(hass)
        caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
        inverter.update({f"field{i}": i for i in range(10)})
        inverter.update({f"other{i}": i for i in range(10)})
        dynamic = [e for e in inverter.all_entities() if e.auto_discovered]
        assert len(dynamic) == 3
        assert sum("limit of 3" in r.message for r in caplog.records) == 1


class TestDeduplication:
    """Pushing the same value twice should short-circuit before state-write."""

    def test_equal_value_skips_write(
        self,
        hass: HomeAssistant,
        inverter: HybridInverter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sensor = inverter.sensor_entities["pv.power"]
        writes = _attach(sensor, hass, monkeypatch)

        sensor.set_native_value(1.0)
        sensor.set_native_value(1.0)
        sensor.set_native_value(2.0)
        sensor.set_native_value(2.0)

        assert writes == [1.0, 2.0]

    def test_write_before_add_is_safe(self, inverter: HybridInverter) -> None:
        """Values can arrive before the entity is added to Home Assistant."""
        inverter.sensor_entities["pv.power"].set_native_value(3.0)
        inverter.binary_sensor_entities["online"].set_binary_value(True)
        inverter.mark_unavailable()


class TestModelDrivenEntities:
    """Hardware-dependent entities follow the model block, not register presence."""

    @pytest.mark.covers("FC-ENT-02")
    def test_three_phase_creates_phase_entities(self, hass: HomeAssistant) -> None:
        inverter = _make(hass, model=MODEL_THREE_PHASE_DUAL)
        for group in ("grid", "system"):
            for phase in ("L1", "L2", "L3"):
                for field in ("power", "voltage", "current"):
                    assert f"{group}.phases.{phase}.{field}" in inverter.sensor_entities

        grid_l1 = inverter.sensor_entities["grid.phases.L1.power"]
        # Keeps the unique id the pre-2.0 auto-discovery produced for this path.
        assert grid_l1.unique_id == "abc123_grid_phases_L1_power"
        assert grid_l1.translation_key == "grid_phase_power"
        assert grid_l1.translation_placeholders == {"phase": "L1"}
        # Grid power is a headline figure, so are its phases; system power is
        # diagnostic, so are its phases; voltage and current always are.
        assert grid_l1.entity_category is None
        assert (
            inverter.sensor_entities["system.phases.L1.power"].entity_category
            is EntityCategory.DIAGNOSTIC
        )
        assert (
            inverter.sensor_entities["grid.phases.L2.voltage"].entity_category
            is EntityCategory.DIAGNOSTIC
        )

    @pytest.mark.covers("FC-ENT-01", "FC-ENT-02")
    def test_every_phase_entity_has_its_documented_metadata(self, hass: HomeAssistant) -> None:
        inverter = _make(hass, model=MODEL_THREE_PHASE_DUAL)
        expected = {
            "power": ("kW", "power", 2, None),
            "voltage": ("V", "voltage", 1, "mdi:sine-wave"),
            "current": ("A", "current", 2, "mdi:current-ac"),
        }
        group_icons = {"grid": "mdi:transmission-tower", "system": "mdi:flash"}
        for group in ("grid", "system"):
            for phase in ("L1", "L2", "L3"):
                for field, (unit, device_class, precision, icon) in expected.items():
                    entity = inverter.sensor_entities[f"{group}.phases.{phase}.{field}"]
                    assert entity.unique_id == f"abc123_{group}_phases_{phase}_{field}"
                    assert entity.translation_key == f"{group}_phase_{field}"
                    assert entity.translation_placeholders == {"phase": phase}
                    assert entity.native_unit_of_measurement == unit
                    assert entity.device_class == device_class
                    assert entity.state_class == "measurement"
                    assert entity.suggested_display_precision == precision
                    assert entity.icon == (icon or group_icons[group])
                    headline = group == "grid" and field == "power"
                    assert entity.entity_category is (None if headline else EntityCategory.DIAGNOSTIC)

    def test_three_phase_updates_and_discovers_extra_phase_fields(
        self, hass: HomeAssistant
    ) -> None:
        inverter = _make(hass, model=MODEL_THREE_PHASE_DUAL)
        inverter.update(
            {"grid": {"phases": {"L1": {"power": 1.1, "voltage": 231.0, "frequency": 50.0}}}}
        )
        assert inverter.sensor_entities["grid.phases.L1.power"].native_value == 1.1
        assert inverter.sensor_entities["grid.phases.L1.voltage"].native_value == 231.0
        assert "grid.phases.L1.frequency" in inverter.sensor_entities

    @pytest.mark.covers("FC-ENT-02")
    def test_single_phase_model_suppresses_phase_registers(
        self, hass: HomeAssistant
    ) -> None:
        inverter = _make(hass, model=MODEL_SINGLE_PHASE)
        inverter.update({"grid": {"phases": {"L2": {"power": 0}}}})
        assert not any(p.startswith("grid.phases") for p in inverter.sensor_entities)

    def test_unknown_model_keeps_legacy_phase_discovery(
        self, inverter: HybridInverter
    ) -> None:
        inverter.update({"grid": {"phases": {"L1": {"power": 0.5}}}})
        entity = inverter.sensor_entities["grid.phases.L1.power"]
        assert entity.auto_discovered
        assert entity.native_value == 0.5

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            (MODEL_SINGLE_PHASE, 2),
            (MODEL_THREE_PHASE_DUAL, 3),
            ({"name": "Unknown flags"}, 2),
            (None, 0),
        ],
    )
    @pytest.mark.covers("FC-ENT-03")
    def test_mppt_count_comes_from_model(
        self, hass: HomeAssistant, model: dict | None, expected: int
    ) -> None:
        inverter = _make(hass, model=model)
        assert inverter.mppt_count == expected
        created = {
            int(path.split(".")[2])
            for path in inverter.sensor_entities
            if path.startswith("pv.mppts.")
        }
        assert created == set(range(1, expected + 1))

    def test_mppt_entities_use_translation_placeholders(
        self, hass: HomeAssistant
    ) -> None:
        inverter = _make(hass, model=MODEL_SINGLE_PHASE)
        entity = inverter.sensor_entities["pv.mppts.2.voltage"]
        assert entity.unique_id == "abc123_mppt_2_voltage"
        assert entity.translation_key == "mppt_voltage"
        assert entity.translation_placeholders == {"index": "2"}
        assert entity.entity_category is EntityCategory.DIAGNOSTIC

    def test_mppt_updates_keyed_by_own_index(self, hass: HomeAssistant) -> None:
        """An omitted tracker must not shift another's readings onto it."""
        inverter = _make(hass, model=MODEL_SINGLE_PHASE)
        inverter.update({"pv": {"mppts": [{"index": 2, "power": 0.7, "voltage": 300}]}})
        assert inverter.sensor_entities["pv.mppts.2.power"].native_value == 0.7
        assert inverter.sensor_entities["pv.mppts.1.power"].native_value is None

    def test_phantom_third_mppt_is_ignored(self, hass: HomeAssistant) -> None:
        inverter = _make(hass, model=MODEL_SINGLE_PHASE)
        inverter.update(
            {"pv": {"mppts": [{"index": i, "power": 0.0} for i in (1, 2, 3)]}}
        )
        assert "pv.mppts.3.power" not in inverter.sensor_entities
        assert not any(p.startswith("pv.mppts.2.index") for p in inverter.sensor_entities)

    @pytest.mark.covers("FC-ENT-04")
    def test_batteries_from_discovery_reading(self, hass: HomeAssistant) -> None:
        state = {"battery": {"batteries": [{"index": 1}, {"index": 2}]}}
        inverter = _make(hass, model=MODEL_THREE_PHASE_DUAL, state=state)
        assert inverter.battery_indices == [1, 2]
        raw = inverter.sensor_entities["battery.batteries.2.stateOfCharge"]
        assert raw.unique_id == "abc123_battery_2_raw_state_of_charge"
        assert raw.translation_placeholders == {"index": "2"}
        assert (
            inverter.sensor_entities["battery.batteries.1.power"].entity_category
            is EntityCategory.DIAGNOSTIC
        )

    @pytest.mark.covers("FC-ENT-04")
    def test_battery_updates_and_new_battery_added_later(
        self, hass: HomeAssistant
    ) -> None:
        state = {"battery": {"batteries": [{"index": 1}]}}
        inverter = _make(hass, model=MODEL_SINGLE_PHASE, state=state)
        inverter.update(
            {
                "battery": {
                    "batteries": [
                        {"index": 1, "stateOfCharge": 83, "power": -0.4, "moduleCount": 4},
                        {"index": 2, "stateOfCharge": 81, "power": -0.3, "moduleCount": 4},
                    ]
                }
            }
        )
        assert inverter.sensor_entities["battery.batteries.1.stateOfCharge"].native_value == 83
        assert inverter.sensor_entities["battery.batteries.2.moduleCount"].native_value == 4
        assert inverter.battery_indices == [1, 2]
        added = [
            entity
            for call in inverter.controller.add_entities.call_args_list
            for entity in call.args[1]
        ]
        assert {e.unique_id for e in added} >= {
            "abc123_battery_2_raw_state_of_charge",
            "abc123_battery_2_power",
            "abc123_battery_2_modules",
        }

    @pytest.mark.covers("FC-ENT-05")
    def test_ratings_from_model(self, hass: HomeAssistant) -> None:
        inverter = _make(hass, model=MODEL_SINGLE_PHASE)
        rated = inverter.sensor_entities["model.maxAcPower"]
        assert rated.native_value == 12.0
        assert rated.unique_id == "abc123_model_maxAcPower"
        assert rated.state_class is None
        inverter.update({"model": {"maxAcPower": 11.5}})
        assert rated.native_value == 11.5

    def test_rating_added_when_reported_later(self, hass: HomeAssistant) -> None:
        inverter = _make(hass, model={"name": "X"})
        assert "model.maxDcPower" not in inverter.sensor_entities
        inverter.update({"model": {"maxDcPower": 9.0}})
        assert inverter.sensor_entities["model.maxDcPower"].native_value == 9.0
        inverter.controller.add_entities.assert_called()


class TestAvailability:
    """Entities go unavailable after repeated failed polls and recover."""

    @pytest.mark.covers("FC-AVAIL-01")
    def test_threshold_and_recovery(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inverter = _make(hass)
        sensor = inverter.sensor_entities["pv.power"]
        writes = _attach(sensor, hass, monkeypatch)

        for _ in range(UNAVAILABLE_AFTER_FAILED_POLLS - 1):
            inverter.record_poll_failure()
        assert inverter.available and sensor.available

        inverter.record_poll_failure()
        assert not inverter.available
        assert not sensor.available
        assert not inverter.binary_sensor_entities["online"].available
        assert len(writes) == 1

        inverter.record_poll_failure()
        assert len(writes) == 1, "no repeated writes while already unavailable"

        inverter.record_poll_success()
        assert sensor.available
        assert len(writes) == 2

    def test_mark_unavailable_is_immediate(self, hass: HomeAssistant) -> None:
        inverter = _make(hass)
        inverter.mark_unavailable()
        assert not inverter.available
        inverter.record_poll_success()
        assert inverter.available

    def test_entity_without_availability_callback_is_available(
        self, hass: HomeAssistant, inverter: HybridInverter
    ) -> None:
        entity = InverterBinarySensorEntity(
            hass, inverter.device_info, "x", "standalone", None
        )
        assert entity.available


class TestMigrationSupport:
    """Mappings used by ``migration.py``."""

    @pytest.mark.covers("FC-MIG-01")
    def test_positional_migrations(self, hass: HomeAssistant) -> None:
        state = {
            "pv": {"mppts": [{"index": 1}, {"index": 2}, {"index": 3}]},
            "battery": {"batteries": [{"index": 2}]},
        }
        inverter = _make(hass, model=MODEL_SINGLE_PHASE, state=state)
        migrations = inverter.positional_migrations()
        assert migrations["pv_mppts_0_power"] == "mppt_1_power"
        assert migrations["pv_mppts_1_current"] == "mppt_2_current"
        # The model says two trackers: the phantom third one is not migrated.
        assert "pv_mppts_2_power" not in migrations
        assert migrations["battery_batteries_0_stateOfCharge"] == (
            "battery_2_raw_state_of_charge"
        )
        assert migrations["battery_batteries_0_moduleCount"] == "battery_2_modules"

    @pytest.mark.covers("FC-MIG-02")
    def test_obsolete_patterns_need_known_hardware(self, hass: HomeAssistant) -> None:
        assert _make(hass).obsolete_entity_patterns() == []
        assert _make(hass, model=MODEL_SINGLE_PHASE).obsolete_entity_patterns() == []
        known = _make(hass, model=MODEL_SINGLE_PHASE, state={})
        patterns = known.obsolete_entity_patterns()
        assert any(p.match("grid_phases_L2_power") for p in patterns)
        assert any(p.match("model_threePhase") for p in patterns)
        assert not any(p.match("model_maxAcPower") for p in patterns)
        three = _make(hass, model=MODEL_THREE_PHASE_DUAL, state={})
        assert not any(p.match("grid_phases_L2_power") for p in three.obsolete_entity_patterns())

    def test_curated_entity_types(self, hass: HomeAssistant) -> None:
        inverter = _make(hass, model=MODEL_SINGLE_PHASE)
        inverter.update({"newField": 1})
        curated = inverter.curated_entity_types
        assert "pv_power" in curated
        assert "mppt_1_power" in curated
        assert "api_connection" in curated
        assert "newField" not in curated


_JSON_VALUES = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=6),
    lambda children: st.lists(children, max_size=3)
    | st.dictionaries(st.text(max_size=8), children, max_size=4),
    max_leaves=20,
)
_PAYLOADS = st.dictionaries(
    st.sampled_from(
        ["pv", "battery", "grid", "load", "system", "online", "status", "operatingMode", "model", "extra"]
    ),
    _JSON_VALUES,
    max_size=6,
)


@pytest.mark.covers("FC-VAL-01", "FC-VAL-02")
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(first=_PAYLOADS, second=_PAYLOADS)
def test_random_readings_never_raise_or_publish_invalid_values(
    hass: HomeAssistant, first: dict[str, Any], second: dict[str, Any]
) -> None:
    inverter = _make(hass, model=MODEL_SINGLE_PHASE, state={"battery": {"batteries": [{"index": 1}]}})
    for payload in (first, second):
        inverter.update(payload)
        for sensor in inverter.sensor_entities.values():
            value = sensor.native_value
            if sensor._numeric:
                assert value is None or (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                ), (sensor.entity_id, value)
                if sensor.state_class == "total_increasing":
                    assert value is None or value >= 0
            else:
                assert not isinstance(value, (dict, list))
        for binary_sensor in inverter.binary_sensor_entities.values():
            assert binary_sensor.is_on in (True, False, None)


class TestRobustness:
    """Pathological payloads and repeated state changes."""

    def test_deep_nesting_is_cut_off_and_reported_once(
        self, inverter: HybridInverter, caplog: pytest.LogCaptureFixture
    ) -> None:
        payload: dict[str, Any] = {"leaf": 1.0}
        for _ in range(hi._MAX_DEPTH + 3):
            payload = {"deeper": payload}
        caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
        inverter.update({"top": payload})
        inverter.update({"top": payload})
        warnings = [r for r in caplog.records if "levels deep" in r.getMessage()]
        assert len(warnings) == 1
        assert not any(path.endswith(".leaf") for path in inverter.sensor_entities)

    @pytest.mark.covers("FC-ENT-06")
    def test_a_value_that_is_not_json_creates_nothing(self, inverter: HybridInverter) -> None:
        before = set(inverter.sensor_entities) | set(inverter.binary_sensor_entities)
        inverter.update({"odd": object()})
        assert set(inverter.sensor_entities) | set(inverter.binary_sensor_entities) == before

    def test_state_changes_are_idempotent(
        self, hass: HomeAssistant, inverter: HybridInverter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        writes = _attach(inverter.sensor_entities["pv.power"], hass, monkeypatch)
        inverter.set_missing(True)
        inverter.set_missing(True)
        assert inverter.missing
        assert len(writes) == 1
        inverter.set_missing(False)
        inverter.shut_down()
        inverter.shut_down()
        assert len(writes) == 3
        assert not inverter.sensor_entities["pv.power"].available

    @pytest.mark.parametrize(("levels", "kept"), [(hi._MAX_DEPTH - 2, True), (hi._MAX_DEPTH - 1, False)])
    def test_the_depth_limit_is_exact(self, inverter: HybridInverter, levels: int, kept: bool) -> None:
        payload: dict[str, Any] = {"leaf": 1.0}
        for _ in range(levels):
            payload = {"d": payload}
        inverter.update({"top": payload})
        assert any(path.endswith(".leaf") for path in inverter.sensor_entities) is kept

    @pytest.mark.parametrize(("levels", "kept"), [(hi._MAX_DEPTH - 1, True), (hi._MAX_DEPTH, False)])
    def test_nested_lists_count_towards_the_depth_limit(
        self, inverter: HybridInverter, levels: int, kept: bool
    ) -> None:
        payload: Any = 1.0
        for _ in range(levels):
            payload = [payload]
        inverter.update({"top": payload})
        assert any(path.startswith("top.") for path in inverter.sensor_entities) is kept

    def test_text_fields_keep_their_case(self, inverter: HybridInverter) -> None:
        inverter.update({"firmwareVersion": "V1.2-RC", "status": "OnGrid"})
        assert inverter.sensor_entities["firmwareVersion"].native_value == "V1.2-RC"
        assert inverter.sensor_entities["status"].native_value == "ongrid"

    def test_a_private_key_does_not_hide_the_keys_after_it(self, inverter: HybridInverter) -> None:
        inverter.update({"extra": {"_private": 1, "after": 2}})
        assert "extra.after" in inverter.sensor_entities
        assert "extra._private" not in inverter.sensor_entities

    def test_short_lists_are_not_reported_as_truncated(
        self, inverter: HybridInverter, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
        inverter.update({"strings": [{"power": 1.0}]})
        assert not [r for r in caplog.records if "truncated list" in r.getMessage()]
        long_list = [{"power": float(i)} for i in range(_MAX_LIST_EXPANSION + 1)]
        inverter.update({"strings": long_list})
        inverter.update({"strings": long_list})
        assert len([r for r in caplog.records if "truncated list" in r.getMessage()]) == 1

    @pytest.mark.covers("FC-AVAIL-01")
    def test_the_failure_threshold_restarts_after_a_success(self, inverter: HybridInverter) -> None:
        for _ in range(UNAVAILABLE_AFTER_FAILED_POLLS - 1):
            inverter.record_poll_failure()
        inverter.record_poll_success()
        for _ in range(UNAVAILABLE_AFTER_FAILED_POLLS - 1):
            inverter.record_poll_failure()
        assert inverter.available is True
        inverter.record_poll_failure()
        assert inverter.available is False
        inverter.record_poll_success()
        inverter.mark_unavailable()
        assert inverter.available is False

    def test_a_new_inverter_has_not_been_read_yet(self, inverter: HybridInverter) -> None:
        assert inverter.api_connected is None
        assert inverter.last_success_at is None
        assert inverter.offline is False
        assert inverter.missing is False


class TestEntityContracts:
    """What each entity exposes to Home Assistant."""

    @pytest.mark.covers("FC-ENT-10")
    def test_the_api_connection_entity(self, hass: HomeAssistant) -> None:
        inverter = _make(hass, model=MODEL_SINGLE_PHASE)
        entity = inverter.binary_sensor_entities[hi.API_CONNECTION_PATH]
        assert entity.unique_id == "abc123_api_connection"
        assert entity.entity_id == "binary_sensor.abc123_api_connection"
        assert entity.translation_key == "api_connection"
        assert entity.device_class == "connectivity"
        assert entity.entity_category is EntityCategory.DIAGNOSTIC
        assert entity.icon == "mdi:cloud-sync-outline"
        assert entity.device_info is inverter.device_info
        assert entity.auto_discovered is False
        assert "api_connection" in inverter.curated_entity_types

    @pytest.mark.covers("FC-ENT-05")
    def test_rating_entities(self, hass: HomeAssistant) -> None:
        inverter = _make(hass, model=MODEL_SINGLE_PHASE)
        for field, key, icon in (
            ("maxAcPower", "rated_ac_power", "mdi:transmission-tower"),
            ("maxDcPower", "rated_dc_power", "mdi:battery-charging-high"),
        ):
            entity = inverter.sensor_entities[f"model.{field}"]
            assert entity.unique_id == f"abc123_model_{field}"
            assert entity.translation_key == key
            assert entity.native_unit_of_measurement == "kW"
            assert entity.device_class == "power"
            assert entity.state_class is None
            assert entity.suggested_display_precision == 1
            assert entity.icon == icon
            assert entity.entity_category is EntityCategory.DIAGNOSTIC

    @pytest.mark.covers("FC-ENT-04", "FC-ENT-05")
    def test_entities_found_later_go_to_the_sensor_platform(self, hass: HomeAssistant) -> None:
        inverter = _make(hass, model={"threePhase": False}, state={})
        inverter.update({"battery": {"batteries": [{"index": 4, "power": 1.0}]}, "model": {"maxAcPower": 6}})
        platforms = [call.args[0] for call in inverter.controller.add_entities.call_args_list]
        assert platforms == ["sensor", "sensor"]
        battery_call, rating_call = inverter.controller.add_entities.call_args_list
        assert {entity.entity_type for entity in battery_call.args[1]} == {
            "battery_4_raw_state_of_charge", "battery_4_power", "battery_4_modules"
        }
        assert [entity.entity_type for entity in rating_call.args[1]] == ["model_maxAcPower"]

    @pytest.mark.covers("FC-VAL-06")
    def test_an_out_of_range_tracker_does_not_hide_the_others(
        self, hass: HomeAssistant
    ) -> None:
        inverter = _make(hass, model=MODEL_SINGLE_PHASE)
        inverter.update({"pv": {"mppts": [{"index": 3, "power": 9.0}, {"index": 1, "power": 1.0}]}})
        assert inverter.sensor_entities["pv.mppts.1.power"].native_value == 1.0
        unknown = _make(hass)
        unknown.update({"pv": {"mppts": [{"index": 1, "power": 1.0}]}})
        assert unknown.ignored_list_items == {
            "pv.mppts:out_of_range:1": "MPPT index 1 cannot be placed: the model is unknown"
        }
        unknown.update({"battery": {"batteries": [{"index": "two"}]}})
        assert "battery.batteries:no_index:'two'" in unknown.ignored_list_items

    @pytest.mark.covers("FC-ENT-02")
    def test_single_phase_machines_drop_both_phase_blocks(self, hass: HomeAssistant) -> None:
        inverter = _make(hass, model=MODEL_SINGLE_PHASE)
        inverter.update({"system": {"phases": {"L1": {"power": 1.0}}}, "grid": {"phases": {"L1": {"power": 1.0}}}})
        assert not any(".phases." in path for path in inverter.sensor_entities)

    @pytest.mark.covers("FC-VAL-01")
    @pytest.mark.parametrize(
        ("path", "meta"),
        [
            ("unit_only", {"entity_type": "u", "unit": "%"}),
            ("class_only", {"entity_type": "c", "device_class": SensorDeviceClass.POWER}),
            ("state_class_only", {"entity_type": "s", "state_class": SensorStateClass.MEASUREMENT}),
            ("decimals_only", {"entity_type": "d", "decimals": 0}),
        ],
    )
    def test_any_numeric_hint_makes_a_sensor_numeric(
        self, hass: HomeAssistant, path: str, meta: dict[str, Any]
    ) -> None:
        inverter = _make(hass)
        sensor = inverter._create_sensor(path, meta)
        sensor.set_native_value("text")
        assert sensor.native_value is None
        sensor.set_native_value("4")
        assert sensor.native_value == 4.0

    def test_a_text_sensor_keeps_text(self, hass: HomeAssistant) -> None:
        sensor = _make(hass)._create_sensor("t", {"entity_type": "t"})
        sensor.set_native_value("Hello")
        assert sensor.native_value == "Hello"

    def test_zero_decimals_is_a_display_precision(self, hass: HomeAssistant) -> None:
        inverter = _make(hass)
        assert inverter.sensor_entities["battery.stateOfCharge"].suggested_display_precision == 0
        assert inverter._create_sensor("n", {"entity_type": "n"}).suggested_display_precision is None

    @pytest.mark.covers("FC-VAL-01", "FC-VAL-02")
    def test_a_bad_value_after_a_good_one_is_still_reported(
        self, inverter: HybridInverter, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
        inverter.update({"online": True, "pv": {"power": 1.0}})
        inverter.update({"online": "sometimes", "pv": {"power": "lots"}})
        messages = [r.getMessage() for r in caplog.records]
        assert any("'sometimes'" in m for m in messages)
        assert any("'lots'" in m for m in messages)
        inverter.update({"online": None, "pv": {"power": None}})
        assert len([m for m in messages if "unusable value" in m]) == 2

    @pytest.mark.covers("FC-VAL-04", "FC-VAL-05")
    def test_each_counter_problem_is_reported_once(
        self, inverter: HybridInverter, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
        for value in (5.0, 4.0, 5.0, 4.0, 5.0, -1.0, 5.0, -2.0):
            inverter.update({"pv": {"energyToday": value}})
        messages = [r.getMessage() for r in caplog.records if "withheld" in r.getMessage()]
        assert len(messages) == 2
        assert "decrease reading of 4.0" in messages[0]
        assert "(last reported 5.0)" in messages[0]
        assert "negative reading of -1.0" in messages[1]

    def test_auto_discovered_entities_carry_their_own_name(self, hass: HomeAssistant) -> None:
        inverter = _make(hass)
        inverter.update({"extra": {"cellTemperature": 21.0, "fanRunning": True}})
        sensor = inverter.sensor_entities["extra.cellTemperature"]
        binary = inverter.binary_sensor_entities["extra.fanRunning"]
        for entity, name in ((sensor, "Extra Cell Temperature"), (binary, "Extra Fan Running")):
            assert entity.name == name
            assert entity.translation_key is None
            assert entity.auto_discovered is True
            assert entity.entity_category is EntityCategory.DIAGNOSTIC

    def test_obsolete_patterns_cover_positional_batteries(self, hass: HomeAssistant) -> None:
        patterns = _make(hass, model=MODEL_SINGLE_PHASE, state={}).obsolete_entity_patterns()
        assert any(p.match("battery_batteries_0_power") for p in patterns)
        assert not any(p.match("battery_1_power") for p in patterns)


class TestCounterGuard:
    """TotalIncreasingGuard on its own."""

    def test_problems_are_named(self) -> None:
        guard = hi.TotalIncreasingGuard()
        assert guard.judge(-1.0) == (None, "negative")
        assert guard.judge(5.0) == (5.0, None)
        assert guard.judge(4.0) == (None, "decrease")

    def test_a_drop_to_exactly_ninety_percent_is_a_reset(self) -> None:
        guard = hi.TotalIncreasingGuard()
        guard.judge(10.0)
        guard.judge(9.0)
        assert guard.judge(9.0) == (9.0, None)
        assert guard.reference == 9.0

    def test_restoring(self) -> None:
        guard = hi.TotalIncreasingGuard()
        guard.restore(-1.0)
        assert guard.reference is None
        guard.restore(0.5)
        assert guard.reference == 0.5
        assert guard.judge(0.4) == (None, "decrease")
        guard.restore(0.2)
        assert guard.reference == 0.5, "a lower restored value never lowers the reference"


class TestOnlineFlag:
    """How the ``online`` field drives the offline state."""

    @pytest.mark.covers("FC-AVAIL-03")
    def test_offline_without_an_online_field_is_not_assumed(self, inverter: HybridInverter) -> None:
        inverter.update({"pv": {"power": 1.0}})
        assert not inverter.offline
        inverter.update({"online": False})
        assert inverter.offline
        inverter.update({"online": "unknown"})
        assert not inverter.offline, "an unparseable online flag does not hide telemetry"
