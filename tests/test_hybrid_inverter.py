"""Tests for the :mod:`custom_components.eleven_energy.hybrid_inverter` module."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from homeassistant.core import HomeAssistant

from custom_components.eleven_energy.hybrid_inverter import (
    _MAX_LIST_EXPANSION,
    HybridInverter,
    InverterSensorEntity,
)


@pytest.fixture
def inverter(hass: HomeAssistant) -> HybridInverter:
    """Return a HybridInverter wired to a mock controller (no side effects)."""
    controller = MagicMock()
    controller.add_entities = MagicMock()
    return HybridInverter(
        hass=hass,
        controller=controller,
        device_id="abc123",
        device_name="Hybrid",
        device_serial_number="SN-1",
    )


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
        assert inverter.device_info["name"] == "Eleven Energy Hybrid"
        assert inverter.device_info["serial_number"] == "SN-1"


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

    def test_known_binary_path_updates_binary_sensor(
        self, inverter: HybridInverter
    ) -> None:
        inverter.update({"online": True})
        assert inverter.binary_sensor_entities["online"].is_on is True

    def test_firmware_version_is_now_exposed(
        self, inverter: HybridInverter
    ) -> None:
        inverter.update({"firmwareVersion": "v1.2.3"})
        assert inverter.sensor_entities["firmwareVersion"].native_value == "v1.2.3"


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


class TestLeafIsolation:
    """A single bad leaf must not poison sibling entities."""

    def test_failing_leaf_does_not_block_siblings(
        self, inverter: HybridInverter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force ``set_native_value`` to raise for the ``pv.power`` entity only.
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

        caplog.set_level(
            logging.WARNING, logger="custom_components.eleven_energy.hybrid_inverter"
        )
        inverter.update({"pv": {"power": 5}})

        assert any(
            "failed handling leaf pv.power" in record.message
            for record in caplog.records
        )


class TestListWalking:
    """Lists are expanded with index-based paths and capped at the safety limit."""

    def test_list_expands_to_indexed_paths(
        self, inverter: HybridInverter
    ) -> None:
        inverter.update(
            {
                "pvStrings": [
                    {"power": 1.1},
                    {"power": 2.2},
                ]
            }
        )
        assert "pvStrings.0.power" in inverter.sensor_entities
        assert "pvStrings.1.power" in inverter.sensor_entities
        assert inverter.sensor_entities["pvStrings.0.power"].native_value == 1.1
        assert inverter.sensor_entities["pvStrings.1.power"].native_value == 2.2

    def test_list_truncates_beyond_max(
        self, inverter: HybridInverter, caplog: pytest.LogCaptureFixture
    ) -> None:
        oversized = [{"v": i} for i in range(_MAX_LIST_EXPANSION + 5)]
        caplog.set_level(
            logging.WARNING, logger="custom_components.eleven_energy.hybrid_inverter"
        )
        inverter.update({"items": oversized})

        last_kept = f"items.{_MAX_LIST_EXPANSION - 1}.v"
        first_dropped = f"items.{_MAX_LIST_EXPANSION}.v"

        assert last_kept in inverter.sensor_entities
        assert first_dropped not in inverter.sensor_entities
        assert any("truncated list items" in r.message for r in caplog.records)


class TestDeduplication:
    """Pushing the same value twice should short-circuit before state-write."""

    def test_equal_value_skips_write(
        self,
        hass: HomeAssistant,
        inverter: HybridInverter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Pretend the entity is attached so that ``async_write_ha_state`` runs.
        sensor = inverter.sensor_entities["pv.power"]
        sensor.hass = hass
        sensor.platform = MagicMock()
        sensor.entity_id = "sensor.test_pv_power"

        writes: list[float | None] = []
        monkeypatch.setattr(
            InverterSensorEntity,
            "async_write_ha_state",
            lambda self: writes.append(self._attr_native_value),
        )

        sensor.set_native_value(1.0)
        sensor.set_native_value(1.0)
        sensor.set_native_value(2.0)
        sensor.set_native_value(2.0)

        assert writes == [1.0, 2.0]
