"""Pure-Python helper tests for the controller, inverter and action schemas."""

from __future__ import annotations

import math

import pytest
import voluptuous as vol

from homeassistant.const import EntityCategory

from custom_components.eleven_energy_plus.const import (
    MAX_POWER_KW,
    MAX_TARGET_MINUTES,
)
from custom_components.eleven_energy_plus.controller import (
    _as_list,
    _valid_minutes,
    _valid_percent,
    _valid_power_kw,
    api_device_label,
    listed_inverter_ids,
    model_block,
)
from custom_components.eleven_energy_plus.hybrid_inverter import (
    _MAX_LIST_EXPANSION,
    IgnoredItem,
    _humanise_path,
    _infer_meta,
    indexed_items,
    item_index,
    scan_indexed_items,
)
from custom_components.eleven_energy_plus.services import (
    BOOLEAN_VALUE,
    MINUTES_VALUE,
    PERCENT_VALUE,
    POWER_KW_VALUE,
)

_UNUSABLE = [None, "nan-string", object(), [1, 2], "nan", "inf", "-inf", math.nan, math.inf, True, False]


class TestValidPercent:
    """``_valid_percent`` is the controller's gate for every percentage field."""

    @pytest.mark.parametrize(
        ("value", "expected"), [(0, 0.0), (50, 50.0), (100, 100.0), ("75", 75.0), (75.5, 75.5)]
    )
    def test_accepts_in_range(self, value, expected) -> None:
        assert _valid_percent(value) == expected

    @pytest.mark.parametrize("value", [-5, -0.01, 100.01, 150, *_UNUSABLE])
    def test_refuses_rather_than_clamps(self, value) -> None:
        """Out of range is refused, never clamped; NaN never becomes 100 %."""
        assert _valid_percent(value) is None


class TestValidPowerKw:
    """``_valid_power_kw`` refuses unrealistic kW values."""

    @pytest.mark.parametrize("value", [0, 3.5, MAX_POWER_KW, "20"])
    def test_accepts_in_range(self, value) -> None:
        assert _valid_power_kw(value) == float(value)

    @pytest.mark.parametrize("value", [100, MAX_POWER_KW + 0.1, -1, "abc", *_UNUSABLE])
    def test_refuses_rather_than_clamps(self, value) -> None:
        """Regression: "nan" kW used to become full power (50 kW)."""
        assert _valid_power_kw(value) is None


class TestValidMinutes:
    """``_valid_minutes`` returns whole minutes within the API range, or None."""

    @pytest.mark.parametrize(
        ("value", "expected"), [(1, 1), (120, 120), ("60", 60), (60.0, 60), (MAX_TARGET_MINUTES, MAX_TARGET_MINUTES)]
    )
    def test_accepts_whole_minutes_in_range(self, value, expected) -> None:
        assert _valid_minutes(value) == expected

    @pytest.mark.parametrize("value", [0, -5, 60.7, "60.5", 99999, "two", *_UNUSABLE])
    def test_refuses_fractions_and_out_of_range(self, value) -> None:
        """Regression: 60.7 used to be truncated to 60 and "inf" to raise."""
        assert _valid_minutes(value) is None


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
            (7, ["7"]),
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
        ("value", "expected"),
        [(True, True), ("on", True), ("false", False), (0, False), (1, True), ("YES", True)],
    )
    def test_boolean_accepts(self, value, expected) -> None:
        assert BOOLEAN_VALUE(value) is expected

    @pytest.mark.parametrize("value", ["maybe", "2", None, 2, 0.5, -1, [], "", "yesno"])
    def test_boolean_rejects(self, value) -> None:
        """Regression: ``cv.boolean`` accepted any number (2 -> True)."""
        with pytest.raises(vol.Invalid):
            BOOLEAN_VALUE(value)

    @pytest.mark.parametrize(
        ("validator", "value", "message"),
        [
            (PERCENT_VALUE, "nan", "expected a finite number, got 'nan'"),
            (MINUTES_VALUE, 60.5, "expected a whole number of minutes, got 60.5"),
            (BOOLEAN_VALUE, 2, "expected true or false, got 2"),
        ],
    )
    def test_errors_name_the_refused_value(self, validator, value, message) -> None:
        with pytest.raises(vol.Invalid) as err:
            validator(value)
        assert message in str(err.value)


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

    @pytest.mark.parametrize(
        ("item", "expected"),
        [
            ({"index": 0}, 0),
            ({"index": 3}, 3),
            ({}, None),
            ({"index": True}, None),
            ({"index": "7"}, None),
            ({"index": 1.0}, None),
            ({"index": -1}, None),
            ({"index": None}, None),
        ],
    )
    def test_item_index_never_falls_back_to_position(self, item, expected) -> None:
        """A positional fallback would shift readings between trackers."""
        assert item_index(item) == expected

    def test_tolerates_shapes(self) -> None:
        assert list(indexed_items(None, "pv.mppts")) == []
        assert list(indexed_items({"pv": []}, "pv.mppts")) == []
        assert scan_indexed_items({"pv": {"mppts": "x"}}, "pv.mppts") == ([], [])

    def test_reports_every_ignored_item(self) -> None:
        payload = {
            "pv": {
                "mppts": [
                    {"index": 1},
                    "x",
                    {"power": 1},
                    {"index": 1, "power": 2},
                    {"index": 2},
                ]
            }
        }
        usable, ignored = scan_indexed_items(payload, "pv.mppts")
        assert [(position, index) for position, index, _ in usable] == [(0, 1), (4, 2)]
        assert [item.key for item in ignored] == ["not_object:str", "no_index:None", "duplicate:1"]
        assert ignored[0].reason == "item 1 is a str, not an object"
        assert ignored[1].reason == "item 2 has no usable index (None)"
        assert ignored[2].reason == "duplicate index 1; only the first such item is used"
        _usable, ignored = scan_indexed_items({"pv": {"mppts": [{"index": "x"}]}}, "pv.mppts")
        assert ignored == [IgnoredItem("no_index:'x'", "item 0 has no usable index ('x')")]

    def test_reports_truncation(self) -> None:
        payload = {"pv": {"mppts": [{"index": i} for i in range(_MAX_LIST_EXPANSION + 3)]}}
        usable, ignored = scan_indexed_items(payload, "pv.mppts")
        assert len(usable) == _MAX_LIST_EXPANSION
        assert ignored == [
            IgnoredItem("truncated", f"more than {_MAX_LIST_EXPANSION} items; the rest are ignored")
        ]


class TestListedInverters:
    """``listed_inverter_ids`` picks the hybrid inverters out of a site listing."""

    def test_filters_and_deduplicates(self) -> None:
        payload = {
            "devices": [
                {"deviceId": "a", "type": "hybridinverter"},
                {"deviceId": "t", "type": "thermostat"},
                {"deviceId": "a", "type": "hybridinverter"},
                {"deviceId": "", "type": "hybridinverter"},
                {"deviceId": 7, "type": "hybridinverter"},
                {"type": "hybridinverter"},
                "not a device",
                {"deviceId": "b", "type": "hybridinverter"},
            ]
        }
        assert listed_inverter_ids(payload) == ["a", "b"]

    def test_empty_listing(self) -> None:
        assert listed_inverter_ids({"devices": []}) == []
        assert listed_inverter_ids({}) == []


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
            ("pv.--", "PV --"),
            ("readingABC", "Reading ABC"),
            ("valueABCD", "Value Abcd"),
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

    @pytest.mark.covers("FC-ENT-06")
    @pytest.mark.parametrize(
        ("path", "value", "unit", "device_class", "state_class", "icon", "decimals"),
        [
            ("pv.energyToday", 1.0, "kWh", "energy", "total_increasing", "mdi:lightning-bolt", 2),
            ("pv.totalEnergy", 1.0, "kWh", "energy", "total_increasing", "mdi:lightning-bolt", 2),
            ("pv.energyLifetime", 1.0, "kWh", "energy", "total_increasing", "mdi:lightning-bolt", 2),
            ("inverter.reactivePower", 1, "var", "reactive_power", "measurement", "mdi:sine-wave", 0),
            ("inverter.qReactive", 1, "var", "reactive_power", "measurement", "mdi:sine-wave", 0),
            ("inverter.apparentPower", 1, "VA", "apparent_power", "measurement", "mdi:flash-outline", 0),
            ("inverter.sApparent", 1, "VA", "apparent_power", "measurement", "mdi:flash-outline", 0),
            ("foo.somePower", 1.0, "kW", "power", "measurement", "mdi:flash", 2),
            ("battery.rate", 1.0, "kW", "power", "measurement", "mdi:battery-arrow-up", 2),
            ("battery.chargeRate", 1.0, "kW", "power", "measurement", "mdi:battery-arrow-up", 2),
            ("battery.dischargeRate", 1.0, "kW", "power", "measurement", "mdi:battery-arrow-up", 2),
            ("battery.maxChargeRate", 1.0, "kW", "power", "measurement", "mdi:battery-arrow-up", 2),
            ("battery.maxDischargeRate", 1.0, "kW", "power", "measurement", "mdi:battery-arrow-up", 2),
            ("ac.voltage", 230, "V", "voltage", "measurement", "mdi:flash", 2),
            ("dc.current", 1, "A", "current", "measurement", "mdi:current-ac", 2),
            ("grid.frequency", 50, "Hz", "frequency", "measurement", "mdi:sine-wave", 2),
            ("inverter.temperature", 30, "\u00b0C", "temperature", "measurement", "mdi:thermometer", 1),
            ("inverter.heatsinkTemp", 30, "\u00b0C", "temperature", "measurement", "mdi:thermometer", 1),
            ("battery.stateOfHealth", 95, "%", None, "measurement", "mdi:heart-pulse", 0),
            ("battery.soh", 95, "%", None, "measurement", "mdi:heart-pulse", 0),
            ("battery.stateOfCharge", 80, "%", "battery", "measurement", "mdi:percent", 0),
            ("ev.soc", 80, "%", "battery", "measurement", "mdi:percent", 0),
            ("battery.targetPc", 80, "%", "battery", "measurement", "mdi:percent", 0),
            ("inverter.loadPc", 40, "%", None, "measurement", "mdi:percent", 0),
            ("inverter.chargePercentage", 40, "%", None, "measurement", "mdi:percent", 0),
            ("foo.unknown", 42, None, None, "measurement", "mdi:numeric", -1),
            ("pv.peakToday", 5, None, None, "measurement", "mdi:numeric", -1),
            ("inverter.totalRuntime", 5, None, None, "measurement", "mdi:numeric", -1),
            ("ev.stateOfCharge", 80, "%", "battery", "measurement", "mdi:percent", 0),
        ],
    )
    def test_every_numeric_heuristic(
        self,
        path: str,
        value: float,
        unit: str | None,
        device_class: str | None,
        state_class: str,
        icon: str,
        decimals: int,
    ) -> None:
        meta = _infer_meta(path, value)
        assert meta is not None
        assert meta["kind"] == "sensor"
        assert meta["unit"] == unit
        assert (meta["device_class"] and meta["device_class"].value) == device_class
        assert meta["state_class"].value == state_class
        assert meta["icon"] == icon
        assert meta["decimals"] == decimals
        assert meta["category"] is EntityCategory.DIAGNOSTIC
        assert meta["entity_type"] == path.replace(".", "_")

    @pytest.mark.covers("FC-ENT-06")
    @pytest.mark.parametrize(
        ("path", "device_class"),
        [
            ("anyFault", "problem"),
            ("inverter.error", "problem"),
            ("gridAlarm", "problem"),
            ("inverterOnline", "connectivity"),
            ("meter.connected", "connectivity"),
            ("pumpRunning", "running"),
            ("exportActive", "running"),
            ("flag", None),
        ],
    )
    def test_every_boolean_heuristic(self, path: str, device_class: str | None) -> None:
        meta = _infer_meta(path, True)
        assert meta is not None
        assert meta["kind"] == "binary_sensor"
        assert (meta["device_class"] and meta["device_class"].value) == device_class
        assert meta["icon"] == "mdi:checkbox-marked-circle-outline"
        assert meta["category"] is EntityCategory.DIAGNOSTIC

    @pytest.mark.covers("FC-ENT-06")
    @pytest.mark.parametrize(
        ("path", "icon"),
        [
            ("hardwareVersion", "mdi:chip"),
            ("battery.model", "mdi:chip"),
            ("firmwareBuild", "mdi:chip"),
            ("serialNo", "mdi:chip"),
            ("statusText", "mdi:information-outline"),
        ],
    )
    def test_every_text_heuristic(self, path: str, icon: str) -> None:
        meta = _infer_meta(path, "x")
        assert meta is not None
        assert meta["kind"] == "sensor"
        assert meta["unit"] is None
        assert meta["device_class"] is None
        assert meta["state_class"] is None
        assert meta["icon"] == icon

    def test_only_the_last_segment_decides_the_kind(self) -> None:
        """A parent block's name must not classify the field inside it."""
        meta = _infer_meta("pv.voltage.count", 3)
        assert meta is not None
        assert meta["unit"] is None
        assert meta["device_class"] is None

    def test_nested_paths_keep_their_identity_and_name(self) -> None:
        meta = _infer_meta("battery.cells.minVoltage", 3.2)
        assert meta is not None
        assert meta["entity_type"] == "battery_cells_minVoltage"
        assert meta["name"] == "Battery Cells Min Voltage"
        assert meta["category"] is EntityCategory.DIAGNOSTIC

    def test_percentage_outside_the_battery_has_no_battery_class(self) -> None:
        meta = _infer_meta("inverter.loadPc", 40)
        assert meta is not None
        assert meta["unit"] == "%"
        assert meta["device_class"] is None

    def test_unrecognised_number_falls_back_to_numeric_icon(self) -> None:
        meta = _infer_meta("foo.unknown", 42)
        assert meta is not None
        assert meta["icon"] == "mdi:numeric"
        assert meta["unit"] is None
