"""Pure-Python helper tests (no HomeAssistant runtime needed)."""

from __future__ import annotations

import pytest

from custom_components.eleven_energy_plus.controller import (
    _clamp_minutes,
    _clamp_percent,
    _clamp_power_kw,
    _coerce_bool,
)
from custom_components.eleven_energy_plus.hybrid_inverter import (
    _humanise_path,
    _infer_meta,
)


class TestClampPercent:
    """``_clamp_percent`` is the gate for every percentage field."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, 0.0),
            (50, 50.0),
            (100, 100.0),
            (-5, 0.0),
            (150, 100.0),
            ("75", 75.0),
            (75.5, 75.5),
        ],
    )
    def test_clamps_into_range(self, value, expected) -> None:
        assert _clamp_percent(value) == expected

    @pytest.mark.parametrize("value", [None, "nan-string", object(), [1, 2]])
    def test_returns_none_for_uncoercible(self, value) -> None:
        assert _clamp_percent(value) is None


class TestClampPowerKw:
    """``_clamp_power_kw`` guards against the user posting unrealistic kW values."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, 0.0),
            (3.5, 3.5),
            (50, 50.0),
            (100, 50.0),
            (-1, 0.0),
        ],
    )
    def test_clamps_into_range(self, value, expected) -> None:
        assert _clamp_power_kw(value) == expected

    def test_returns_none_for_uncoercible(self) -> None:
        assert _clamp_power_kw("abc") is None
        assert _clamp_power_kw(None) is None


class TestClampMinutes:
    """``_clamp_minutes`` must always return a positive integer or None."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1, 1),
            (120, 120),
            (0, 1),
            (-5, 1),
            ("60", 60),
            (60.7, 60),
        ],
    )
    def test_clamps_to_positive_int(self, value, expected) -> None:
        assert _clamp_minutes(value) == expected

    def test_returns_none_for_uncoercible(self) -> None:
        assert _clamp_minutes(None) is None
        assert _clamp_minutes("two") is None


class TestCoerceBool:
    """``_coerce_bool`` handles the various truthy strings YAML emits."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (True, True),
            (False, False),
            ("true", True),
            ("True", True),
            ("YES", True),
            ("on", True),
            ("1", True),
            ("false", False),
            ("no", False),
            ("off", False),
            ("0", False),
            ("anything-else", False),
            (1, True),
            (0, False),
        ],
    )
    def test_handles_truthy_strings(self, value, expected) -> None:
        assert _coerce_bool(value) is expected


class TestHumanisePath:
    """``_humanise_path`` powers the auto-discovered entity ``name`` attribute."""

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("pv.power", "PV Power"),
            ("battery.stateOfCharge", "Battery State Of Charge"),
            ("operatingMode.workMode", "Operating Mode Work Mode"),
            ("acVoltage", "AC Voltage"),
            ("dcCurrent", "DC Current"),
            ("pvStrings.0.power", "PV Strings 0 Power"),
            ("ev.soc", "EV SOC"),
            ("simple", "Simple"),
        ],
    )
    def test_humanises(self, path, expected) -> None:
        assert _humanise_path(path) == expected


class TestInferMeta:
    """The auto-discovery heuristics drive every dynamic entity's metadata."""

    def test_returns_none_for_none_value(self) -> None:
        assert _infer_meta("anything", None) is None

    def test_bool_becomes_binary_sensor(self) -> None:
        meta = _infer_meta("anyFault", True)
        assert meta is not None
        assert meta["kind"] == "binary_sensor"
        # "fault" should map to PROBLEM device class.
        assert meta["device_class"].value == "problem"

    def test_bool_online_maps_to_connectivity(self) -> None:
        meta = _infer_meta("inverterOnline", False)
        assert meta is not None
        assert meta["device_class"].value == "connectivity"

    def test_today_energy_becomes_total_increasing(self) -> None:
        meta = _infer_meta("pv.energyToday", 1.23)
        assert meta is not None
        assert meta["device_class"].value == "energy"
        assert meta["state_class"].value == "total_increasing"
        assert meta["unit"] == "kWh"

    def test_lifetime_energy_becomes_total_increasing(self) -> None:
        meta = _infer_meta("pv.totalEnergy", 1234.5)
        assert meta is not None
        assert meta["state_class"].value == "total_increasing"
        assert meta["unit"] == "kWh"

    def test_power_field_maps_to_kw_power(self) -> None:
        meta = _infer_meta("foo.bar.somePower", 1.0)
        assert meta is not None
        assert meta["device_class"].value == "power"
        assert meta["unit"] == "kW"

    def test_reactive_power_detected(self) -> None:
        meta = _infer_meta("inverter.reactivePower", 50)
        assert meta is not None
        assert meta["device_class"].value == "reactive_power"

    def test_apparent_power_detected(self) -> None:
        meta = _infer_meta("inverter.apparentPower", 50)
        assert meta is not None
        assert meta["device_class"].value == "apparent_power"

    def test_voltage_maps_to_volts(self) -> None:
        meta = _infer_meta("ac.voltage", 230)
        assert meta is not None
        assert meta["device_class"].value == "voltage"
        assert meta["unit"] == "V"

    def test_current_maps_to_amps(self) -> None:
        meta = _infer_meta("dc.current", 10)
        assert meta is not None
        assert meta["device_class"].value == "current"
        assert meta["unit"] == "A"

    def test_frequency_maps_to_hertz(self) -> None:
        meta = _infer_meta("grid.frequency", 50)
        assert meta is not None
        assert meta["device_class"].value == "frequency"
        assert meta["unit"] == "Hz"

    def test_temperature_maps_to_celsius(self) -> None:
        meta = _infer_meta("inverter.temperature", 35)
        assert meta is not None
        assert meta["device_class"].value == "temperature"
        assert meta["unit"] == "\u00b0C"

    def test_soh_maps_to_percent(self) -> None:
        meta = _infer_meta("battery.soh", 95)
        assert meta is not None
        assert meta["unit"] == "%"

    def test_battery_soc_maps_to_battery_device_class(self) -> None:
        meta = _infer_meta("battery.stateOfCharge", 80)
        assert meta is not None
        assert meta["device_class"].value == "battery"
        assert meta["unit"] == "%"

    def test_charge_rate_maps_to_power(self) -> None:
        meta = _infer_meta("battery.maxChargeRate", 5)
        assert meta is not None
        assert meta["device_class"].value == "power"
        assert meta["unit"] == "kW"

    def test_string_version_gets_chip_icon(self) -> None:
        meta = _infer_meta("hardwareVersion", "1.2.3")
        assert meta is not None
        assert meta["kind"] == "sensor"
        assert meta["icon"] == "mdi:chip"

    def test_string_other_gets_information_icon(self) -> None:
        meta = _infer_meta("status", "ok")
        assert meta is not None
        assert meta["icon"] == "mdi:information-outline"

    def test_unrecognised_number_falls_back_to_numeric_icon(self) -> None:
        meta = _infer_meta("foo.unknown", 42)
        assert meta is not None
        assert meta["icon"] == "mdi:numeric"
        assert meta["unit"] is None
