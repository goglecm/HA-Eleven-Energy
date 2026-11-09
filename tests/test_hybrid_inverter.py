"""Tests for the :mod:`custom_components.eleven_energy_plus.hybrid_inverter` module."""

from __future__ import annotations

import logging
import math
from typing import Any
from unittest.mock import MagicMock

import pytest

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

    def test_predefined_sensors_created(self, inverter: HybridInverter) -> None:
        assert "pv.power" in inverter.sensor_entities
        assert "battery.stateOfCharge" in inverter.sensor_entities
        assert "operatingMode.workMode" in inverter.sensor_entities
        assert "firmwareVersion" in inverter.sensor_entities

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

    def test_entity_ids_match_platform_domain(self, inverter: HybridInverter) -> None:
        """Entity ids must never be minted in the wrong domain (stops in 2027.5)."""
        for entity in inverter.sensor_entities.values():
            assert entity.entity_id.startswith("sensor.")
        for entity in inverter.binary_sensor_entities.values():
            assert entity.entity_id.startswith("binary_sensor.")

    def test_binary_sensor_starts_unknown(self, inverter: HybridInverter) -> None:
        """Before the first reading, Online must not claim "off" (offline)."""
        assert inverter.binary_sensor_entities["online"].is_on is None

    def test_no_model_entities_without_model(self, inverter: HybridInverter) -> None:
        assert inverter.mppt_count == 0
        assert not inverter.three_phase
        assert not any(
            path.startswith(("pv.mppts.", "battery.batteries.", "model.", "grid.phases."))
            for path in inverter.sensor_entities
        )


class TestWalkLeafRouting:
    """The walker pushes values into the right pre-existing entities."""

    def test_known_numeric_path_updates_sensor(
        self, inverter: HybridInverter
    ) -> None:
        inverter.update({"pv": {"power": 1.23}})
        assert inverter.sensor_entities["pv.power"].native_value == 1.23

    def test_known_string_lowercase_path_lowercased(
        self, inverter: HybridInverter
    ) -> None:
        inverter.update({"status": "OnGrid"})
        assert inverter.sensor_entities["status"].native_value == "ongrid"

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
    def test_numeric_sensor_rejects_unusable_values(
        self, inverter: HybridInverter, bad: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        sensor = inverter.sensor_entities["pv.power"]
        sensor.set_native_value(2.0)
        caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
        sensor.set_native_value(bad)
        assert sensor.native_value is None
        assert any("unusable value" in r.message for r in caplog.records)

    def test_numeric_sensor_accepts_numeric_strings(
        self, inverter: HybridInverter
    ) -> None:
        sensor = inverter.sensor_entities["pv.power"]
        sensor.set_native_value("2.5")
        assert sensor.native_value == 2.5

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

    def test_list_expands_to_indexed_paths(
        self, inverter: HybridInverter
    ) -> None:
        inverter.update({"pvStrings": [{"power": 1.1}, {"power": 2.2}]})
        assert inverter.sensor_entities["pvStrings.0.power"].native_value == 1.1
        assert inverter.sensor_entities["pvStrings.1.power"].native_value == 2.2

    def test_list_truncates_beyond_max(
        self, inverter: HybridInverter, caplog: pytest.LogCaptureFixture
    ) -> None:
        oversized = [{"v": i} for i in range(_MAX_LIST_EXPANSION + 5)]
        caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
        inverter.update({"items": oversized})

        assert f"items.{_MAX_LIST_EXPANSION - 1}.v" in inverter.sensor_entities
        assert f"items.{_MAX_LIST_EXPANSION}.v" not in inverter.sensor_entities
        assert any("truncated list items" in r.message for r in caplog.records)

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
        assert "newField" not in curated
