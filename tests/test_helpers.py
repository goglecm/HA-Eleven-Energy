"""Pure-Python helper tests (no HomeAssistant runtime needed)."""

from __future__ import annotations

import math

import pytest
import voluptuous as vol

from custom_components.eleven_energy_plus.const import (
    MAX_POWER_KW,
    MAX_TARGET_MINUTES,
)
from custom_components.eleven_energy_plus.controller import (
    _as_list,
    _clamp_minutes,
    _clamp_percent,
    _clamp_power_kw,
    _coerce_bool,
    api_device_label,
    model_block,
)
from custom_components.eleven_energy_plus.hybrid_inverter import (
    _humanise_path,
    _infer_meta,
    _item_index,
    coerce_finite_number,
    indexed_items,
    parse_bool,
)
from custom_components.eleven_energy_plus.services import (
    BOOLEAN_VALUE,
    MINUTES_VALUE,
    PERCENT_VALUE,
    POWER_KW_VALUE,
)


class TestClampPercent:
    """``_clamp_percent`` is the defensive gate for every percentage field."""

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

    @pytest.mark.parametrize(
        "value",
        [None, "nan-string", object(), [1, 2], "nan", "inf", "-inf", math.nan, math.inf, True, False],
    )
    def test_returns_none_for_unusable(self, value) -> None:
        """NaN must never clamp to 100 % and a boolean is not a percentage."""
        assert _clamp_percent(value) is None


class TestClampPowerKw:
    """``_clamp_power_kw`` guards against unrealistic kW values."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, 0.0),
            (3.5, 3.5),
            (MAX_POWER_KW, MAX_POWER_KW),
            (100, MAX_POWER_KW),
            (-1, 0.0),
        ],
    )
    def test_clamps_into_range(self, value, expected) -> None:
        assert _clamp_power_kw(value) == expected

    @pytest.mark.parametrize("value", ["abc", None, "nan", "inf", math.nan, True])
    def test_returns_none_for_unusable(self, value) -> None:
        """Regression: "nan" kW used to become full power (50 kW)."""
        assert _clamp_power_kw(value) is None


class TestClampMinutes:
    """``_clamp_minutes`` returns whole minutes within the API range, or None."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1, 1),
            (120, 120),
            (0, 1),
            (-5, 1),
            ("60", 60),
            (60.7, 60),
            (99999, MAX_TARGET_MINUTES),
        ],
    )
    def test_clamps_into_range(self, value, expected) -> None:
        assert _clamp_minutes(value) == expected

    @pytest.mark.parametrize("value", [None, "two", "inf", math.inf, math.nan, True])
    def test_returns_none_for_unusable(self, value) -> None:
        """Regression: "inf" used to raise OverflowError out of set_work_mode."""
        assert _clamp_minutes(value) is None


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


class TestAsList:
    """Target values arrive as a string or a list."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, []),
            ("", []),
            ("a", ["a"]),
            (["a", "b"], ["a", "b"]),
            (["a", "", None], ["a"]),
            (("x",), ["x"]),
        ],
    )
    def test_normalises(self, value, expected) -> None:
        assert _as_list(value) == expected


class TestServiceValidators:
    """The action schemas reject bad input instead of clamping it."""

    @pytest.mark.parametrize("value", [0, 50, 100, "75", 12.5])
    def test_percent_accepts(self, value) -> None:
        assert PERCENT_VALUE(value) == float(value)

    @pytest.mark.parametrize(
        "value", [-1, 100.5, 150, "nan", "inf", math.nan, True, False, "abc", None, [50]]
    )
    def test_percent_rejects(self, value) -> None:
        with pytest.raises(vol.Invalid):
            PERCENT_VALUE(value)

    @pytest.mark.parametrize("value", [0, 3.5, MAX_POWER_KW, "20"])
    def test_power_accepts(self, value) -> None:
        assert POWER_KW_VALUE(value) == float(value)

    @pytest.mark.parametrize("value", [-0.1, MAX_POWER_KW + 0.1, "nan", "-inf", True])
    def test_power_rejects(self, value) -> None:
        with pytest.raises(vol.Invalid):
            POWER_KW_VALUE(value)

    @pytest.mark.parametrize(
        ("value", "expected"), [(1, 1), ("60", 60), ("60.0", 60), (1440, 1440)]
    )
    def test_minutes_accepts(self, value, expected) -> None:
        assert MINUTES_VALUE(value) == expected

    @pytest.mark.parametrize("value", [0, 1441, 60.5, "inf", "nan", True, "soon"])
    def test_minutes_rejects(self, value) -> None:
        with pytest.raises(vol.Invalid):
            MINUTES_VALUE(value)

    @pytest.mark.parametrize(
        ("value", "expected"), [(True, True), ("on", True), ("false", False), (0, False)]
    )
    def test_boolean_accepts(self, value, expected) -> None:
        assert BOOLEAN_VALUE(value) is expected

    @pytest.mark.parametrize("value", ["maybe", "2", None])
    def test_boolean_rejects(self, value) -> None:
        with pytest.raises(vol.Invalid):
            BOOLEAN_VALUE(value)


class TestValueParsing:
    """Entity-side parsing of API values."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(1, 1), (1.5, 1.5), ("2.25", 2.25), (" 3 ", 3.0), (0, 0)],
    )
    def test_coerce_finite_number_accepts(self, value, expected) -> None:
        assert coerce_finite_number(value) == expected

    @pytest.mark.parametrize(
        "value", [True, False, "N/A", "", math.nan, math.inf, "nan", None, {}, []]
    )
    def test_coerce_finite_number_rejects(self, value) -> None:
        assert coerce_finite_number(value) is None

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (True, True),
            (False, False),
            ("true", True),
            ("False", False),
            (" ON ", True),
            ("no", False),
            (1, True),
            (0, False),
            (1.0, True),
            (2, None),
            ("maybe", None),
            ("", None),
            (None, None),
            ([], None),
        ],
    )
    def test_parse_bool(self, value, expected) -> None:
        """Regression: ``bool("false")`` used to turn the Online sensor on."""
        assert parse_bool(value) is expected


class TestLabelSelection:
    """The device label comes from the model block, not the provisioning name."""

    def test_model_block_name_wins_over_name(self) -> None:
        summary = {"name": "North Sea 6 (6kW)", "model": {"name": "Mediterranean Sea 12"}}
        assert api_device_label(summary, None) == "Mediterranean Sea 12"

    def test_model_block_from_reading_when_listing_lacks_it(self) -> None:
        summary = {"name": "North Sea 6 (6kW)"}
        state = {"model": {"name": "Mediterranean Sea 12"}}
        assert api_device_label(summary, state) == "Mediterranean Sea 12"

    def test_name_used_without_model_block(self) -> None:
        assert api_device_label({"name": "Real Name"}, None) == "Real Name"

    @pytest.mark.parametrize(
        ("summary", "expected"),
        [
            ({"name": None, "model": "String Model"}, "String Model"),
            ({"name": "", "productName": "Product"}, "Product"),
            ({"name": "  ", "inverterModel": "Inverter Model"}, "Inverter Model"),
            ({"name": None}, "Inverter"),
            ({"model": {"name": "  "}, "name": "Fallback"}, "Fallback"),
            ({}, "Inverter"),
        ],
    )
    def test_fallbacks(self, summary, expected) -> None:
        assert api_device_label(summary, None) == expected

    def test_model_block_ignores_non_dicts(self) -> None:
        assert model_block({"model": "String"}, {"model": ["x"]}) == {}
        assert model_block({"model": {"a": 1}}, None) == {"a": 1}


class TestIndexedItems:
    """List items are keyed by their own ``index``."""

    def test_uses_own_index(self) -> None:
        payload = {"pv": {"mppts": [{"index": 2, "power": 1}, {"index": 1, "power": 2}]}}
        assert [(p, i) for p, i, _ in indexed_items(payload, "pv.mppts")] == [
            (0, 2),
            (1, 1),
        ]

    def test_falls_back_to_position(self) -> None:
        assert _item_index({}, 0) == 1
        assert _item_index({"index": True}, 4) == 5
        assert _item_index({"index": "7"}, 1) == 2
        assert _item_index({"index": 0}, 3) == 0

    def test_tolerates_shapes(self) -> None:
        assert list(indexed_items(None, "pv.mppts")) == []
        assert list(indexed_items({"pv": []}, "pv.mppts")) == []
        assert list(indexed_items({"pv": {"mppts": ["x", 1]}}, "pv.mppts")) == []


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

    def test_returns_none_for_unsupported_type(self) -> None:
        assert _infer_meta("anything", object()) is None

    def test_bool_becomes_binary_sensor(self) -> None:
        meta = _infer_meta("anyFault", True)
        assert meta is not None
        assert meta["kind"] == "binary_sensor"
        assert meta["device_class"].value == "problem"

    def test_bool_online_maps_to_connectivity(self) -> None:
        meta = _infer_meta("inverterOnline", False)
        assert meta is not None
        assert meta["device_class"].value == "connectivity"

    def test_bool_running_maps_to_running(self) -> None:
        meta = _infer_meta("pumpRunning", True)
        assert meta is not None
        assert meta["device_class"].value == "running"

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
