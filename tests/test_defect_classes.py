"""Defect-class sweeps: every place a class of defect could occur.

Each ledger defect (``tests/DEFECTS.md``) has a direct test and a sweep over
every other place in the integration where the same class of defect could
appear. Cases where the baseline code was already correct are tagged
``guard(...)`` and kept as guard tests; cases that caught the defect are
tagged ``defect(...)`` and must fail on the baseline (``tests/tools/check_red.py``).

These tests deliberately drive the integration through surfaces that exist
on both the baseline and the fixed code (entity values, entry setup, service
calls, flows, ``Controller`` / ``HybridInverter`` methods), so a red run on
the baseline fails for the defect's own reason.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime, timedelta
import email.utils
import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import voluptuous as vol

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_entry_oauth2_flow, device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)
from yarl import URL

from custom_components.eleven_energy_plus import hybrid_inverter as hi_module
from custom_components.eleven_energy_plus.const import (
    BASE_URL,
    CONF_DEVICE_LABEL,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DOMAIN,
    OAUTH_TOKEN_URL,
    UNAVAILABLE_AFTER_FAILED_POLLS,
)
from custom_components.eleven_energy_plus.controller import Controller
from custom_components.eleven_energy_plus.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.eleven_energy_plus.hybrid_inverter import HybridInverter

from .common import (
    DEVICE_ID,
    MODEL_SINGLE_PHASE,
    MODEL_THREE_PHASE_DUAL,
    api_key_entry,
    device_payload,
    mock_api,
    restore_sensor_value,
    setup_entry,
    site_summary,
)

HI_LOGGER = "custom_components.eleven_energy_plus.hybrid_inverter"
CONTROLLER_LOGGER = "custom_components.eleven_energy_plus.controller"
POST_URL = f"{BASE_URL}devices/{DEVICE_ID}/operatingMode"


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def _full_reading(model: dict[str, Any]) -> dict[str, Any]:
    """A reading that sets every curated field of a machine described by ``model``."""
    reading = device_payload(
        model=model,
        mppts=[
            {"index": i, "power": 0.5 * i, "voltage": 300.0 + i, "current": 1.5 * i}
            for i in range(1, 4)
        ],
        batteries=[
            {"index": 1, "stateOfCharge": 81, "power": -0.3, "moduleCount": 4},
            {"index": 2, "stateOfCharge": 79, "power": -0.2, "moduleCount": 4},
        ],
    )
    reading["pv"].update({"power": 1.5, "energyToday": 3.2})
    reading["load"].update({"power": 1.2, "energyToday": 4.0})
    reading["battery"].update(
        {"stateOfCharge": 80, "power": -0.5, "energyInToday": 1.0, "energyOutToday": 2.0}
    )
    reading["grid"].update({"power": 0.2, "energyInToday": 1.1, "energyOutToday": 2.2})
    reading["system"] = {"power": 1.0, "voltage": 230.0}
    if model.get("threePhase"):
        for group in ("grid", "system"):
            reading[group]["phases"] = {
                phase: {"power": 0.1, "voltage": 231.0, "current": 0.5}
                for phase in ("L1", "L2", "L3")
            }
    return reading


def _inverter(
    hass: HomeAssistant, model: dict[str, Any], reading: dict[str, Any]
) -> HybridInverter:
    controller = MagicMock()
    controller.add_entities = MagicMock()
    return HybridInverter(
        hass, controller, "dev", "Inverter", "SN", model=model, state=copy.deepcopy(reading)
    )


def _value(inverter: HybridInverter, path: str) -> Any:
    if path in inverter.binary_sensor_entities:
        return inverter.binary_sensor_entities[path].is_on
    return inverter.sensor_entities[path].native_value


def _get_parent(reading: dict[str, Any], path: str) -> tuple[Any, str]:
    """Return ``(container, key)`` for a dotted path (indexed lists by own index)."""
    parts = path.split(".")
    node: Any = reading
    idx = 0
    while idx < len(parts) - 1:
        part = parts[idx]
        if part in ("mppts", "batteries") and isinstance(node.get(part), list):
            wanted = int(parts[idx + 1])
            node = next(item for item in node[part] if item.get("index") == wanted)
            idx += 2
            continue
        node = node[part]
        idx += 1
    return node, parts[-1]


def _mutate(reading: dict[str, Any], path: str, how: str) -> dict[str, Any]:
    reading = copy.deepcopy(reading)
    parent, key = _get_parent(reading, path)
    if how == "null":
        parent[key] = None
    elif how == "missing":
        del parent[key]
    elif how == "parent_missing":
        parts = path.split(".")
        if parts[0] in ("pv", "battery") and parts[1] in ("mppts", "batteries"):
            items = reading[parts[0]][parts[1]]
            reading[parts[0]][parts[1]] = [
                item for item in items if item.get("index") != int(parts[2])
            ]
        else:
            del reading[parts[0]]
    return reading


# ---------------------------------------------------------------------------
# Class: stale values (D11)
# ---------------------------------------------------------------------------

_SINGLE_TOP_LEVEL = list(hi_module._PREDEFINED_DEFS)
_SINGLE_INDEXED = [
    f"pv.mppts.{i}.{field}" for i in (1, 2) for field in ("power", "voltage", "current")
] + [
    f"battery.batteries.{i}.{field}"
    for i in (1, 2)
    for field in ("stateOfCharge", "power", "moduleCount")
]
_THREE_PHASE = [
    f"{group}.phases.{phase}.{field}"
    for group in ("grid", "system")
    for phase in ("L1", "L2", "L3")
    for field in ("power", "voltage", "current")
]


def _stale_cases() -> list[Any]:
    cases = []
    for path in _SINGLE_TOP_LEVEL:
        for how in ("null", "missing", "parent_missing"):
            if how == "parent_missing" and "." not in path:
                continue
            cases.append(
                pytest.param(
                    MODEL_SINGLE_PHASE, path, how,
                    id=f"single-{path}-{how}",
                    marks=[pytest.mark.defect("D11"), pytest.mark.covers("FC-VAL-03")],
                )
            )
    for path in _SINGLE_INDEXED:
        for how in ("null", "missing", "parent_missing"):
            # The baseline already wrote None for an explicit null list field.
            mark = pytest.mark.guard("D11") if how == "null" else pytest.mark.defect("D11")
            cases.append(
                pytest.param(
                    MODEL_SINGLE_PHASE, path, how,
                    id=f"single-{path}-{how}",
                    marks=[mark, pytest.mark.covers("FC-VAL-03")],
                )
            )
    cases.extend(
        pytest.param(
            MODEL_THREE_PHASE_DUAL, path, how,
            id=f"three-{path}-{how}",
            marks=[pytest.mark.defect("D11"), pytest.mark.covers("FC-VAL-03")],
        )
        for path in _THREE_PHASE
        for how in ("null", "missing")
    )
    return cases


@pytest.mark.parametrize(("model", "path", "how"), _stale_cases())
def test_absent_or_null_field_reads_unknown(
    hass: HomeAssistant, model: dict[str, Any], path: str, how: str
) -> None:
    """A field that is null or missing must never keep its previous value."""
    reading = _full_reading(model)
    inverter = _inverter(hass, model, reading)
    inverter.update(copy.deepcopy(reading))
    assert _value(inverter, path) is not None, "precondition: the field has a value"
    inverter.update(_mutate(reading, path, how))
    assert _value(inverter, path) is None


@pytest.mark.defect("D11")
@pytest.mark.covers("FC-VAL-03", "FC-ENT-06")
@pytest.mark.parametrize("how", ["null", "missing"])
def test_auto_discovered_field_reads_unknown_when_absent(
    hass: HomeAssistant, how: str
) -> None:
    inverter = _inverter(hass, MODEL_SINGLE_PHASE, {})
    inverter.update({"extra": {"temperature": 31.5, "flag": True}})
    assert inverter.sensor_entities["extra.temperature"].native_value == 31.5
    assert inverter.binary_sensor_entities["extra.flag"].is_on is True
    if how == "null":
        inverter.update({"extra": {"temperature": None, "flag": None}})
    else:
        inverter.update({"extra": {}})
    assert inverter.sensor_entities["extra.temperature"].native_value is None
    assert inverter.binary_sensor_entities["extra.flag"].is_on is None


@pytest.mark.guard("D11")
@pytest.mark.covers("FC-ENT-05")
def test_static_ratings_survive_readings_without_model(hass: HomeAssistant) -> None:
    """Nameplate ratings are static: a reading without the model block keeps them."""
    inverter = _inverter(hass, MODEL_SINGLE_PHASE, {})
    reading = _full_reading(MODEL_SINGLE_PHASE)
    del reading["model"]
    inverter.update(reading)
    assert inverter.sensor_entities["model.maxAcPower"].native_value == 12.0
    assert inverter.sensor_entities["model.maxDcPower"].native_value == 10.0


# ---------------------------------------------------------------------------
# Class: offline inverter (D12) and availability causes (X-AVAIL)
# ---------------------------------------------------------------------------

_OFFLINE_EXEMPT = {"online", "status", "firmwareVersion", "model.maxAcPower", "model.maxDcPower"}


def _offline_cases() -> list[Any]:
    reading_paths = [
        *hi_module._PREDEFINED_DEFS,
        "pv.mppts.1.power",
        "battery.batteries.1.stateOfCharge",
        "battery.batteries.1.moduleCount",
        "model.maxAcPower",
    ]
    cases = []
    for path in reading_paths:
        exempt = path in _OFFLINE_EXEMPT
        mark = pytest.mark.guard("D12") if exempt else pytest.mark.defect("D12")
        cases.append(
            pytest.param(
                path, not exempt, id=path, marks=[mark, pytest.mark.covers("FC-AVAIL-03")]
            )
        )
    return cases


@pytest.mark.parametrize(("path", "hidden"), _offline_cases())
def test_offline_inverter_hides_stale_telemetry(
    hass: HomeAssistant, path: str, hidden: bool
) -> None:
    reading = _full_reading(MODEL_SINGLE_PHASE)
    inverter = _inverter(hass, MODEL_SINGLE_PHASE, reading)
    inverter.update(copy.deepcopy(reading))
    entity = inverter.sensor_entities.get(path) or inverter.binary_sensor_entities[path]
    assert entity.available

    offline = copy.deepcopy(reading)
    offline["online"] = False
    inverter.update(offline)
    assert entity.available is (not hidden)

    inverter.update(copy.deepcopy(reading))
    assert entity.available, "telemetry recovers as soon as the inverter is online"


def _entity_id_for(kind: str) -> str:
    return {
        "sensor": "sensor.abc_pv_power",
        "binary_sensor": "binary_sensor.abc_system_online",
        "number": "number.abc_poll_interval",
    }[kind]


async def _cause(
    cause: str,
    hass: HomeAssistant,
    entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    controller = entry.runtime_data
    if cause == "poll_failures":
        aioclient_mock.clear_requests()
        aioclient_mock.get(f"{BASE_URL}devices/{DEVICE_ID}", status=500)
        for _ in range(UNAVAILABLE_AFTER_FAILED_POLLS):
            await controller.poll_devices()
    elif cause == "auth":
        aioclient_mock.clear_requests()
        aioclient_mock.get(f"{BASE_URL}devices/{DEVICE_ID}", status=401)
        await controller.poll_devices()
    elif cause == "offline":
        aioclient_mock.clear_requests()
        offline = device_payload(model=MODEL_SINGLE_PHASE)
        offline["online"] = False
        aioclient_mock.get(f"{BASE_URL}devices/{DEVICE_ID}", json=offline)
        await controller.poll_devices()
    elif cause == "missing":
        aioclient_mock.clear_requests()
        aioclient_mock.get(f"{BASE_URL}site", json={"devices": []})
        aioclient_mock.get(
            f"{BASE_URL}devices/{DEVICE_ID}", json=device_payload(model=MODEL_SINGLE_PHASE)
        )
        await controller.poll_site()
        await controller.poll_site()
        await controller.poll_devices()
    elif cause == "shutdown":
        await controller.terminate()
    await hass.async_block_till_done()


# (entity kind, cause) -> (expected available, ledger mark). The mark records
# what the baseline did: "defect" where it showed a stale value as available,
# "guard" where it was already right. The API Connection kind is new and is
# swept in test_notifications.py.
_AVAIL_TABLE: dict[tuple[str, str], tuple[bool, str, str]] = {
    ("sensor", "poll_failures"): (False, "guard", "D02"),
    ("sensor", "auth"): (False, "guard", "D01"),
    ("sensor", "offline"): (False, "defect", "D12"),
    ("sensor", "missing"): (False, "defect", "D04"),
    ("sensor", "shutdown"): (False, "defect", "D10"),
    ("binary_sensor", "poll_failures"): (False, "guard", "D02"),
    ("binary_sensor", "auth"): (False, "guard", "D01"),
    ("binary_sensor", "offline"): (True, "guard", "D12"),
    ("binary_sensor", "missing"): (False, "defect", "D04"),
    ("binary_sensor", "shutdown"): (False, "defect", "D10"),
    ("number", "poll_failures"): (True, "guard", "D02"),
    ("number", "auth"): (True, "guard", "D01"),
    ("number", "offline"): (True, "guard", "D12"),
    ("number", "missing"): (True, "guard", "D04"),
    ("number", "shutdown"): (False, "defect", "D10"),
}


def _avail_cases() -> list[Any]:
    return [
        pytest.param(
            kind, cause, expected,
            id=f"{kind}-{cause}",
            marks=[
                getattr(pytest.mark, mark)(defect),
                pytest.mark.covers(
                    f"X-AVAIL-{kind}-{cause}",
                    *(("FC-AVAIL-05",) if kind == "number" and expected else ()),
                ),
            ],
        )
        for (kind, cause), (expected, mark, defect) in _AVAIL_TABLE.items()
    ]


@pytest.mark.parametrize(("kind", "cause", "available"), _avail_cases())
async def test_availability_per_entity_kind_and_cause(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    kind: str,
    cause: str,
    available: bool,
) -> None:
    mock_api(
        aioclient_mock,
        devices={DEVICE_ID: device_payload(model=MODEL_SINGLE_PHASE)},
        summaries=[site_summary(model=MODEL_SINGLE_PHASE)],
    )
    entry = api_key_entry()
    assert await setup_entry(hass, entry)
    entity_id = _entity_id_for(kind)
    assert hass.states.get(entity_id).state != STATE_UNAVAILABLE

    await _cause(cause, hass, entry, aioclient_mock)
    state = hass.states.get(entity_id)
    assert (state.state != STATE_UNAVAILABLE) is available


# ---------------------------------------------------------------------------
# Class: total_increasing counters (D13)
# ---------------------------------------------------------------------------

_COUNTER_PATHS = [
    path
    for path, meta in hi_module._PREDEFINED_DEFS.items()
    if str(meta.get("state_class")) == "total_increasing"
] + ["pv.lifetimeEnergyTotal", "battery.energyTodayExtra"]

_COUNTER_SEQUENCES = {
    "one_off_dip": ([5.0, 0.0, 5.1], [5.0, None, 5.1]),
    "small_dip": ([5.0, 4.9, 5.1], [5.0, None, 5.1]),
    "negative": ([5.0, -1.0, 5.0], [5.0, None, 5.0]),
    "midnight_reset": ([5.0, 0.0, 0.1], [5.0, None, 0.1]),
    "persistent_small_correction": ([5.0, 4.9, 4.9], [5.0, None, 5.0]),
    "reset_then_glitch": ([5.0, 0.0, 0.1, 0.0, 0.2], [5.0, None, 0.1, None, 0.2]),
}


def _set_path(reading: dict[str, Any], path: str, value: float) -> None:
    node = reading
    *parents, leaf = path.split(".")
    for part in parents:
        node = node.setdefault(part, {})
    node[leaf] = value


@pytest.mark.defect("D13")
@pytest.mark.covers("FC-VAL-04", "FC-VAL-05")
@pytest.mark.parametrize("path", _COUNTER_PATHS)
@pytest.mark.parametrize("sequence", list(_COUNTER_SEQUENCES))
def test_total_increasing_counters_never_report_a_false_reset(
    hass: HomeAssistant, path: str, sequence: str
) -> None:
    readings, expected = _COUNTER_SEQUENCES[sequence]
    inverter = _inverter(hass, {}, {})
    seen = []
    for value in readings:
        reading: dict[str, Any] = {}
        _set_path(reading, path, value)
        inverter.update(reading)
        entity = inverter.sensor_entities[path]
        assert str(entity.state_class) == "total_increasing"
        seen.append(entity.native_value)
    assert seen == expected


_RESTART_CASES = {
    # name: (value before the restart, readings after it, expected states, mark)
    "dip_after_restart": (5.0, [0.0], [STATE_UNKNOWN], "defect"),
    "reset_after_restart": (5.0, [0.0, 0.1], [STATE_UNKNOWN, "0.1"], "defect"),
    "rise_after_restart": (5.0, [5.2], ["5.2"], "guard"),
}


@pytest.mark.covers("FC-VAL-05")
@pytest.mark.parametrize(
    ("restored", "readings", "expected"),
    [
        pytest.param(
            restored, readings, expected, id=name, marks=getattr(pytest.mark, mark)("D33")
        )
        for name, (restored, readings, expected, mark) in _RESTART_CASES.items()
    ],
)
async def test_counter_is_judged_against_its_value_before_a_restart(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    restored: float,
    readings: list[float],
    expected: list[str],
) -> None:
    """The first reading after a restart must not be a false meter reset."""
    entity_id = "sensor.abc_pv_energy_today"
    entry = api_key_entry()
    entry.add_to_hass(hass)
    restore_sensor_value(hass, entry, entity_id, restored, "kWh")
    first = device_payload()
    first["pv"]["energyToday"] = readings[0]
    mock_api(aioclient_mock, devices={DEVICE_ID: first})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    seen = [hass.states.get(entity_id).state]
    for value in readings[1:]:
        reading = device_payload()
        reading["pv"]["energyToday"] = value
        aioclient_mock.clear_requests()
        mock_api(aioclient_mock, devices={DEVICE_ID: reading})
        await entry.runtime_data.poll_devices()
        await hass.async_block_till_done()
        seen.append(hass.states.get(entity_id).state)
    assert seen == expected


# ---------------------------------------------------------------------------
# Class: index mapping (D14)
# ---------------------------------------------------------------------------

_INDEX_CASES = {
    # name: (list path, items, expected warning substring)
    "mppt_above_count": ("pv.mppts", [{"index": 3, "power": 1.0}], "index 3"),
    "mppt_zero_based": ("pv.mppts", [{"index": 0, "power": 1.0}], "index 0"),
    "mppt_duplicate": ("pv.mppts", [{"index": 1, "power": 1.0}, {"index": 1, "power": 2.0}], "duplicate"),
    "mppt_no_index": ("pv.mppts", [{"power": 1.0}], "no usable index"),
    "battery_non_integer": ("battery.batteries", [{"index": "x", "power": 1.0}], "no usable index"),
    "battery_duplicate": ("battery.batteries", [{"index": 1, "power": 1.0}, {"index": 1, "power": 2.0}], "duplicate"),
}


@pytest.mark.defect("D14")
@pytest.mark.covers("FC-VAL-06")
@pytest.mark.parametrize("case", list(_INDEX_CASES))
def test_unexpected_indices_are_reported(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, case: str
) -> None:
    list_path, items, needle = _INDEX_CASES[case]
    inverter = _inverter(hass, MODEL_SINGLE_PHASE, {"battery": {"batteries": [{"index": 1}]}})
    reading: dict[str, Any] = {}
    group, key = list_path.split(".")
    reading[group] = {key: items}
    caplog.set_level(logging.WARNING, logger=HI_LOGGER)
    inverter.update(reading)
    inverter.update(copy.deepcopy(reading))
    warnings = [r.getMessage() for r in caplog.records if needle in r.getMessage()]
    assert len(warnings) == 1, "reported exactly once, not silently and not every poll"


@pytest.mark.guard("D14")
@pytest.mark.covers("FC-VAL-06", "FC-ENT-04")
def test_battery_index_gaps_create_matching_entities(hass: HomeAssistant) -> None:
    state = {"battery": {"batteries": [{"index": 1}, {"index": 3}]}}
    inverter = _inverter(hass, MODEL_SINGLE_PHASE, state)
    assert inverter.battery_indices == [1, 3]


# ---------------------------------------------------------------------------
# Class: boolean coercion (D06)
# ---------------------------------------------------------------------------

_BOOL_FIELDS = {
    "set_work_mode_idle_battery": ("allow_charging", "allow_discharging"),
    "set_work_mode_grid_export": ("include_excess_solar", "overdrive"),
}
_AMBIGUOUS = ["maybe", 2, "2", "", [], "yesno"]


def _controller_with_fake_inverter(hass: HomeAssistant) -> Controller:
    entry = MockConfigEntry(domain=DOMAIN, data={"token": "t"})
    entry.add_to_hass(hass)
    controller = Controller("t", hass, entry)

    class _Fake:
        type = "hybridinverter"
        device_id = DEVICE_ID
        display_name = "Eleven Energy Plus Inverter"
        missing = False

        def mark_unavailable(self, *args: Any, **kwargs: Any) -> None:
            pass

    controller.devices[DEVICE_ID] = _Fake()  # type: ignore[assignment]
    return controller


def _base_data(service: str) -> dict[str, Any]:
    return {
        "set_work_mode_force_charge": {"target_power": 3, "target_percent": 50},
        "set_work_mode_grid_export": {"target_power": 3, "target_percent": 50},
        "set_work_mode_target_soc": {"target_soc": 50, "target_minutes": 60},
    }.get(service, {})


@pytest.mark.defect("D06")
@pytest.mark.covers("FC-SVC-02")
@pytest.mark.parametrize(
    ("service", "field"),
    [(service, field) for service, fields in _BOOL_FIELDS.items() for field in fields],
)
@pytest.mark.parametrize("value", _AMBIGUOUS, ids=repr)
async def test_controller_refuses_ambiguous_booleans(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    service: str,
    field: str,
    value: Any,
) -> None:
    controller = _controller_with_fake_inverter(hass)
    aioclient_mock.post(POST_URL, status=200)
    result = await controller.set_work_mode(service, {**_base_data(service), field: value})
    assert result["success"] is False
    assert field in (result["error"] or "")
    assert aioclient_mock.call_count == 0, "nothing may be sent for an ambiguous value"


def _schema_bool_cases() -> list[Any]:
    # Home Assistant's cv.boolean accepts any number (2 -> True); every other
    # ambiguous value was already refused by the baseline schema.
    return [
        pytest.param(
            value, id=repr(value),
            marks=pytest.mark.defect("D06") if value == 2 else pytest.mark.guard("D06"),
        )
        for value in _AMBIGUOUS
    ]


@pytest.mark.covers("FC-SVC-02")
@pytest.mark.parametrize(
    ("service", "field"),
    [(service, field) for service, fields in _BOOL_FIELDS.items() for field in fields],
)
@pytest.mark.parametrize("value", _schema_bool_cases())
async def test_action_schema_refuses_ambiguous_booleans(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    service: str,
    field: str,
    value: Any,
) -> None:
    mock_api(aioclient_mock)
    aioclient_mock.post(POST_URL, status=200)
    assert await setup_entry(hass, api_key_entry())
    with pytest.raises((vol.Invalid, ServiceValidationError)):
        await hass.services.async_call(
            DOMAIN, service, {**_base_data(service), field: value},
            blocking=True, return_response=True,
        )


# ---------------------------------------------------------------------------
# Class: number coercion (D07, D30 and the X-NUM cells)
# ---------------------------------------------------------------------------

_SERVICE_FOR_FIELD = {
    "percent_to_battery": "set_work_mode_self_consumption",
    "target_power": "set_work_mode_force_charge",
    "target_percent": "set_work_mode_force_charge",
    "target_soc": "set_work_mode_target_soc",
    "target_minutes": "set_work_mode_target_soc",
    "max_charge_power": "set_work_mode_target_soc",
    "max_discharge_power": "set_work_mode_target_soc",
}
_INVALID_NUMBERS = {
    "nan": "nan",
    "inf": "inf",
    "bool": True,
    "text": "fast",
    "out_of_range": 10_000,
}


@pytest.mark.guard("D07")
@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        pytest.param(
            field, invalid, id=f"{field}-{invalid}",
            marks=pytest.mark.covers(f"X-NUM-{field}-{invalid}", "FC-SVC-01"),
        )
        for field in _SERVICE_FOR_FIELD
        for invalid in _INVALID_NUMBERS
    ],
)
async def test_every_numeric_action_field_rejects_invalid_numbers(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, field: str, invalid: str
) -> None:
    mock_api(aioclient_mock)
    aioclient_mock.post(POST_URL, status=200)
    assert await setup_entry(hass, api_key_entry())
    service = _SERVICE_FOR_FIELD[field]
    data = {**_base_data(service), field: _INVALID_NUMBERS[invalid]}
    posts = sum(1 for call in aioclient_mock.mock_calls if call[0] == "POST")
    with pytest.raises((vol.Invalid, ServiceValidationError)):
        await hass.services.async_call(DOMAIN, service, data, blocking=True, return_response=True)
    assert sum(1 for call in aioclient_mock.mock_calls if call[0] == "POST") == posts


_POLL_INTERVAL_CASES = {
    # name: (stored value, expected interval, warning expected, ledger mark)
    "bool": (True, DEFAULT_POLL_INTERVAL_SECONDS, True, "defect"),
    "nan": (float("nan"), DEFAULT_POLL_INTERVAL_SECONDS, True, "defect"),
    "inf": (float("inf"), DEFAULT_POLL_INTERVAL_SECONDS, True, "defect"),
    "text": ("fast", DEFAULT_POLL_INTERVAL_SECONDS, True, "defect"),
    "out_of_range": (10_000, 300, True, "defect"),
    "fraction": ("30.5", DEFAULT_POLL_INTERVAL_SECONDS, True, "defect"),
    "negative": (-5, 5, True, "defect"),
    "integral_float": (45.0, 45, False, "guard"),
    "numeric_text": ("45", 45, False, "guard"),
    "missing": (None, DEFAULT_POLL_INTERVAL_SECONDS, False, "guard"),
}


def _poll_interval_cases() -> list[Any]:
    cases = []
    for name, (raw, expected, warns, kind) in _POLL_INTERVAL_CASES.items():
        marks = [getattr(pytest.mark, kind)("D07"), pytest.mark.covers("FC-OPT-06")]
        if name in _INVALID_NUMBERS:
            marks.append(pytest.mark.covers(f"X-NUM-poll_interval_option-{name}"))
        cases.append(pytest.param(raw, expected, warns, id=name, marks=marks))
    return cases


@pytest.mark.parametrize(("raw", "expected", "warns"), _poll_interval_cases())
def test_stored_poll_interval_is_parsed_strictly(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    raw: Any,
    expected: int,
    warns: bool,
) -> None:
    options = {} if raw is None else {CONF_POLL_INTERVAL: raw}
    entry = MockConfigEntry(domain=DOMAIN, data={"token": "t"}, options=options)
    entry.add_to_hass(hass)
    caplog.set_level(logging.WARNING, logger=CONTROLLER_LOGGER)
    controller = Controller("t", hass, entry)
    assert controller.poll_interval == expected
    assert controller.poll_interval == expected
    warnings = [r for r in caplog.records if "interval" in r.getMessage().lower()]
    assert len(warnings) == (1 if warns else 0), "reported once, never silently"


@pytest.mark.defect("D30")
@pytest.mark.covers("FC-OPT-05")
@pytest.mark.parametrize("value", [float("nan")], ids=["nan"])
async def test_number_entity_rejects_non_finite_values(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, value: float
) -> None:
    mock_api(aioclient_mock)
    entry = api_key_entry()
    assert await setup_entry(hass, entry)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": "number.abc_poll_interval", "value": value},
            blocking=True,
        )
    assert entry.options[CONF_POLL_INTERVAL] == 30


# ---------------------------------------------------------------------------
# Class: silent failures (D02, D04, D10) and lifecycle (D03)
# ---------------------------------------------------------------------------


@pytest.mark.defect("D02")
@pytest.mark.covers("FC-AVAIL-02")
async def test_reading_that_crashes_processing_counts_as_failure(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_api(aioclient_mock)
    entry = api_key_entry()
    assert await setup_entry(hass, entry)
    controller = entry.runtime_data

    def _boom(self: HybridInverter, payload: Any) -> None:
        raise RuntimeError("processing bug")

    caplog.set_level(logging.WARNING)
    with patch.object(HybridInverter, "update", _boom):
        for _ in range(UNAVAILABLE_AFTER_FAILED_POLLS):
            await controller.poll_devices()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.abc_pv_power").state == STATE_UNAVAILABLE
    assert any("processing bug" in r.getMessage() or r.exc_info for r in caplog.records)


@pytest.mark.defect("D04")
@pytest.mark.covers("FC-DISC-05")
async def test_inverter_missing_from_site_goes_unavailable_and_recovers(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    devices = {"a": device_payload("a"), "b": device_payload("b")}
    mock_api(aioclient_mock, devices=devices)
    entry = api_key_entry()
    assert await setup_entry(hass, entry)
    controller = entry.runtime_data

    aioclient_mock.clear_requests()
    mock_api(aioclient_mock, devices=devices, summaries=[site_summary("a")])
    for _ in range(2):
        await controller.poll_site()
        await controller.poll_devices()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.b_pv_power").state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.a_pv_power").state != STATE_UNAVAILABLE

    aioclient_mock.clear_requests()
    mock_api(aioclient_mock, devices=devices)
    await controller.poll_site()
    await controller.poll_devices()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.b_pv_power").state != STATE_UNAVAILABLE


@pytest.mark.guard("D04")
@pytest.mark.covers("FC-DISC-05")
async def test_one_site_listing_without_an_inverter_is_not_enough(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    devices = {"a": device_payload("a"), "b": device_payload("b")}
    mock_api(aioclient_mock, devices=devices)
    entry = api_key_entry()
    assert await setup_entry(hass, entry)
    aioclient_mock.clear_requests()
    mock_api(aioclient_mock, devices=devices, summaries=[site_summary("a")])
    await entry.runtime_data.poll_site()
    await entry.runtime_data.poll_devices()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.b_pv_power").state != STATE_UNAVAILABLE


@pytest.mark.defect("D10")
@pytest.mark.covers("FC-AVAIL-04")
async def test_failed_unload_leaves_no_stale_values(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_api(aioclient_mock)
    entry = api_key_entry()
    assert await setup_entry(hass, entry)
    with patch.object(
        hass.config_entries, "async_unload_platforms", return_value=False
    ):
        assert not await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.FAILED_UNLOAD
    for entity_id in ("sensor.abc_pv_power", "binary_sensor.abc_system_online"):
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE


@pytest.mark.defect("D03")
@pytest.mark.covers("FC-DISC-04")
async def test_inverter_added_at_runtime_gets_every_platform(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    mock_api(aioclient_mock, devices={"a": device_payload("a")})
    entry = api_key_entry()
    assert await setup_entry(hass, entry)

    aioclient_mock.clear_requests()
    mock_api(aioclient_mock, devices={"a": device_payload("a"), "b": device_payload("b")})
    await entry.runtime_data.poll_site()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.b_pv_power") is not None
    assert hass.states.get("binary_sensor.b_system_online") is not None
    assert hass.states.get("number.b_poll_interval") is not None


# ---------------------------------------------------------------------------
# Defect class "date and time" (D05)
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)


def _http_date(when: datetime) -> str:
    return email.utils.format_datetime(when, usegmt=True)


_RETRY_AFTER_CASES = {
    # name: (header, expected first wait, ledger mark)
    "http_date_future": (_http_date(_NOW + timedelta(seconds=7)), 7.0, "defect"),
    "http_date_far_future": (_http_date(_NOW + timedelta(hours=2)), 30.0, "defect"),
    "http_date_past": (_http_date(_NOW - timedelta(seconds=60)), 1.0, "guard"),
    "malformed": ("soon-ish", 1.0, "guard"),
    "seconds": ("7", 7.0, "guard"),
    "seconds_capped": ("500", 30.0, "guard"),
    "negative_seconds": ("-5", 1.0, "guard"),
}


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        pytest.param(
            header, expected, id=name,
            marks=[getattr(pytest.mark, mark)("D05"), pytest.mark.covers("FC-CMD-02", "FC-TIME-04")],
        )
        for name, (header, expected, mark) in _RETRY_AFTER_CASES.items()
    ],
)
async def test_post_honours_retry_after(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: Any,
    instant_backoff: list[float],
    header: str,
    expected: float,
) -> None:
    freezer.move_to(_NOW)
    controller = _controller_with_fake_inverter(hass)
    aioclient_mock.post(POST_URL, status=429, headers={"Retry-After": header})
    await controller.send_reliable_post(f"devices/{DEVICE_ID}/operatingMode", {})
    assert instant_backoff[0] == pytest.approx(expected, abs=1.0)


@pytest.mark.defect("D05")
@pytest.mark.covers("FC-POLL-02")
@pytest.mark.parametrize(
    "header", ["120", _http_date(_NOW + timedelta(seconds=120))], ids=["seconds", "http_date"]
)
async def test_rate_limited_poll_defers_the_next_poll(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: Any,
    monkeypatch: pytest.MonkeyPatch,
    header: str,
) -> None:
    freezer.move_to(_NOW)
    entry = MockConfigEntry(domain=DOMAIN, data={"token": "t"}, options={CONF_POLL_INTERVAL: 5})
    entry.add_to_hass(hass)
    controller = Controller("t", hass, entry)
    controller.devices[DEVICE_ID] = HybridInverter(hass, controller, DEVICE_ID, "Inv", "SN")
    aioclient_mock.get(f"{BASE_URL}devices/{DEVICE_ID}", status=429, headers={"Retry-After": header})
    aioclient_mock.get(f"{BASE_URL}site", json={"devices": [site_summary()]})
    timeouts: list[float] = []
    real_wait_for = asyncio.wait_for

    async def _recording_wait_for(awaitable: Any, timeout: float | None = None) -> Any:  # noqa: ASYNC109 - stands in for asyncio.wait_for
        if controller._shutdown.is_set():
            return await real_wait_for(awaitable, timeout)
        timeouts.append(timeout)
        if len(timeouts) >= 2:
            controller._shutdown.set()
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(
        "custom_components.eleven_energy_plus.controller.asyncio.wait_for", _recording_wait_for
    )
    for platform in ("sensor", "binary_sensor", "number"):
        controller.complete_platform_setup(platform, lambda entities: None)
    for _ in range(50):
        await asyncio.sleep(0)
        if controller._shutdown.is_set():
            break
    await controller.terminate()
    assert timeouts, "the poller waited at least once"
    assert timeouts[0] >= 119, timeouts


# ---------------------------------------------------------------------------
# Class: credential recovery (D01)
# ---------------------------------------------------------------------------


@pytest.mark.defect("D01")
@pytest.mark.covers("FC-AUTH-08")
async def test_transient_credential_rejection_recovers(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "custom_components.eleven_energy_plus.controller.AUTH_RECOVERY_SECONDS",
        0,
        raising=False,
    )
    mock_api(aioclient_mock)
    entry = api_key_entry(options={CONF_POLL_INTERVAL: 5})
    assert await setup_entry(hass, entry)
    controller = entry.runtime_data

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE_URL}site", status=401)
    aioclient_mock.get(f"{BASE_URL}devices/{DEVICE_ID}", status=401)
    await controller.poll_devices()
    await hass.async_block_till_done()
    assert controller.auth_failed
    assert hass.states.get("sensor.abc_pv_power").state == STATE_UNAVAILABLE

    aioclient_mock.clear_requests()
    mock_api(aioclient_mock)
    for _ in range(20):
        controller._poll_now.set()
        for _ in range(20):
            await asyncio.sleep(0)
        await hass.async_block_till_done()
        if not controller.auth_failed and (
            hass.states.get("sensor.abc_pv_power").state != STATE_UNAVAILABLE
        ):
            break
    assert not controller.auth_failed
    assert hass.states.get("sensor.abc_pv_power").state != STATE_UNAVAILABLE
    assert not [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"]["source"] == SOURCE_REAUTH
    ], "the reauthentication prompt is withdrawn once the credential works again"


# ---------------------------------------------------------------------------
# Class: error classification (D08, D09, D16) - X-SVC cells
# ---------------------------------------------------------------------------

_SERVICES = [
    "set_work_mode_self_consumption",
    "set_work_mode_force_charge",
    "set_work_mode_grid_export",
    "set_work_mode_pv_export",
    "set_work_mode_idle_battery",
    "set_work_mode_target_soc",
    "set_work_mode_reset",
]

# (response mode, outcome) -> expected: "ok", "dict_error", "raise_validation"
# or the translation key of the HomeAssistantError to raise.
_EXPECTED = {
    ("response", "success"): "ok",
    ("no_response", "success"): "ok",
    ("response", "validation"): "raise_validation",
    ("no_response", "validation"): "raise_validation",
    ("response", "target"): "dict_error",
    ("no_response", "target"): "invalid_request",
    ("response", "auth"): "dict_error",
    ("no_response", "auth"): "credential_rejected",
    ("response", "api"): "dict_error",
    ("no_response", "api"): "work_mode_failed",
    ("response", "superseded"): "dict_error",
    ("no_response", "superseded"): "work_mode_superseded",
    ("response", "shutdown"): "dict_error",
    ("no_response", "shutdown"): "work_mode_aborted",
    ("response", "internal"): "dict_error",
    ("no_response", "internal"): "internal_error",
    ("response", "not_loaded"): "dict_error",
    ("no_response", "not_loaded"): "not_loaded",
}
# Items each outcome demonstrates on top of FC-SVC-08.
_SVC_ITEMS = {
    "validation": ("FC-SVC-03",),
    "internal": ("FC-SVC-13",),
    "not_loaded": ("FC-SVC-12",),
    "superseded": ("FC-CMD-03",),
    "shutdown": ("FC-CMD-05",),
}
_SVC_DEFECTS = {
    ("no_response", "auth"): "D16",
    ("no_response", "superseded"): "D08",
    ("no_response", "shutdown"): "D16",
    ("response", "internal"): "D09",
    ("no_response", "internal"): "D09",
}


def _svc_cases() -> list[Any]:
    cases = []
    for service in _SERVICES:
        for mode, outcome in _EXPECTED:
            defect = _SVC_DEFECTS.get((mode, outcome))
            mark = pytest.mark.defect(defect) if defect else pytest.mark.guard("D16")
            cases.append(
                pytest.param(
                    service, mode, outcome,
                    id=f"{service}-{mode}-{outcome}",
                    marks=[
                        mark,
                        pytest.mark.covers(
                            f"X-SVC-{service}-{mode}-{outcome}",
                            "FC-SVC-08",
                            *_SVC_ITEMS.get(outcome, ()),
                        ),
                    ],
                )
            )
    return cases


async def _run_outcome(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
    service: str,
    mode: str,
    outcome: str,
) -> Any:
    """Arrange ``outcome`` for ``service`` and call it; return the response."""
    response = mode == "response"
    data = dict(_base_data(service))

    if outcome == "not_loaded":
        assert await async_setup_component(hass, DOMAIN, {})
        return await hass.services.async_call(
            DOMAIN, service, data, blocking=True, return_response=response
        )

    mock_api(aioclient_mock)
    entry = api_key_entry()
    assert await setup_entry(hass, entry)
    controller = entry.runtime_data

    async def _no_wait(delay: float) -> None:
        return None

    monkeypatch.setattr(controller, "_wait_backoff", _no_wait)

    if outcome == "success":
        aioclient_mock.post(POST_URL, status=200)
    elif outcome == "validation":
        data["bogus"] = 1
    elif outcome == "target":
        data["device_id"] = "not-a-device"
    elif outcome == "auth":
        aioclient_mock.post(POST_URL, status=401)
    elif outcome == "api":
        aioclient_mock.post(POST_URL, status=500)
    elif outcome == "shutdown":
        aioclient_mock.post(POST_URL, status=503)

        async def _shut_down(delay: float) -> None:
            controller._shutdown.set()

        monkeypatch.setattr(controller, "_wait_backoff", _shut_down)
    elif outcome == "internal":

        def _bug(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("integration bug")

        monkeypatch.setattr(controller, "_sanitise_work_mode", _bug)
    elif outcome == "superseded":
        release = asyncio.Event()
        real_request = controller._request

        async def _request(method: str, suffix: str, *, json: Any = None) -> Any:
            if json and json.get("workMode") == "reset" and not release.is_set():
                await release.wait()
                return 200, {}, None
            if method == "POST":
                return 200, {}, None
            return await real_request(method, suffix, json=json)

        monkeypatch.setattr(controller, "_request", _request)
        blocker = asyncio.create_task(
            hass.services.async_call(DOMAIN, "set_work_mode_reset", {}, blocking=True)
        )
        for _ in range(20):
            await asyncio.sleep(0)
        under_test = asyncio.create_task(
            hass.services.async_call(
                DOMAIN, service, data, blocking=True, return_response=response
            )
        )
        for _ in range(20):
            await asyncio.sleep(0)
        newer = asyncio.create_task(
            hass.services.async_call(DOMAIN, "set_work_mode_pv_export", {}, blocking=True)
        )
        for _ in range(20):
            await asyncio.sleep(0)
        release.set()
        await blocker
        await newer
        return await under_test

    return await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=response
    )


@pytest.mark.parametrize(("service", "mode", "outcome"), _svc_cases())
async def test_every_action_outcome_is_classified(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
    service: str,
    mode: str,
    outcome: str,
) -> None:
    expected = _EXPECTED[(mode, outcome)]
    if expected == "raise_validation":
        with pytest.raises((vol.Invalid, ServiceValidationError)):
            await _run_outcome(hass, aioclient_mock, monkeypatch, service, mode, outcome)
        return
    if expected in ("ok", "dict_error"):
        response = await _run_outcome(
            hass, aioclient_mock, monkeypatch, service, mode, outcome
        )
        if mode == "no_response":
            assert response is None
            return
        assert response["success"] is (expected == "ok")
        if expected == "dict_error":
            assert response["error"]
        return
    with pytest.raises(HomeAssistantError) as err:
        await _run_outcome(hass, aioclient_mock, monkeypatch, service, mode, outcome)
    is_validation = isinstance(err.value, ServiceValidationError)
    assert is_validation is (expected == "invalid_request")
    assert err.value.translation_key == expected


# ---------------------------------------------------------------------------
# Class: credential entry points (D18, D19, D20) - X-CRED cells
# ---------------------------------------------------------------------------

_TOKEN_RESPONSE = {
    "access_token": "access-new",
    "refresh_token": "refresh-new",
    "expires_in": 28800,
    "token_type": "Bearer",
}
_EXISTING_DEVICE = "abc"


def _site_for(outcome: str) -> dict[str, Any]:
    if outcome == "no_inverters":
        return {"json": {"devices": [{"deviceId": "t1", "type": "thermostat"}]}}
    if outcome == "invalid_auth":
        return {"status": 401}
    if outcome == "cannot_connect":
        return {"status": 500}
    if outcome == "different_site":
        return {"json": {"devices": [site_summary("zzz")]}}
    return {"json": {"devices": [site_summary(_EXISTING_DEVICE)]}}


_CRED_DEFECTS = {
    ("token", "user", "no_inverters"): "D18",
    ("token", "reauth", "no_inverters"): "D18",
    ("token", "reconfigure", "no_inverters"): "D18",
    ("token", "reauth", "different_site"): "D19",
    ("token", "reconfigure", "different_site"): "D19",
    ("signin", "user", "invalid_auth"): "D20",
    ("signin", "user", "cannot_connect"): "D20",
    ("signin", "user", "no_inverters"): "D20",
    ("signin", "reauth", "invalid_auth"): "D20",
    ("signin", "reauth", "cannot_connect"): "D20",
    ("signin", "reauth", "no_inverters"): "D20",
    ("signin", "reauth", "different_site"): "D19",
    ("signin", "reconfigure", "invalid_auth"): "D20",
    ("signin", "reconfigure", "cannot_connect"): "D20",
    ("signin", "reconfigure", "no_inverters"): "D20",
    ("signin", "reconfigure", "different_site"): "D19",
}


def _cred_cases() -> list[Any]:
    cases = []
    for kind in ("token", "signin"):
        for source in ("user", "reauth", "reconfigure"):
            for outcome in ("valid", "invalid_auth", "cannot_connect", "no_inverters", "different_site"):
                if source == "user" and outcome == "different_site":
                    continue
                defect = _CRED_DEFECTS.get((kind, source, outcome))
                mark = pytest.mark.defect(defect) if defect else pytest.mark.guard("D20")
                covers = [f"X-CRED-{kind}-{source}-{outcome}"]
                if outcome == "no_inverters":
                    covers.append("FC-FLOW-12")
                if outcome == "different_site":
                    covers.append("FC-FLOW-09")
                cases.append(
                    pytest.param(
                        kind, source, outcome,
                        id=f"{kind}-{source}-{outcome}",
                        marks=[mark, pytest.mark.covers(*covers)],
                    )
                )
    return cases


@pytest.fixture
def _flow_env(hass: HomeAssistant) -> Any:
    with (
        patch("custom_components.eleven_energy_plus.async_setup_entry", return_value=True),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        yield reload


async def _sign_in(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, result: dict[str, Any]
) -> dict[str, Any]:
    if result["type"] is FlowResultType.MENU:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "pick_implementation"}
        )
    elif result["type"] is FlowResultType.FORM:  # reauth confirmation
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.EXTERNAL_STEP, result
    state = config_entry_oauth2_flow._decode_jwt(hass, URL(result["url"]).query["state"])
    aioclient_mock.post(OAUTH_TOKEN_URL, json=_TOKEN_RESPONSE)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"state": state, "code": "code"}
    )
    return await hass.config_entries.flow.async_configure(result["flow_id"])


@pytest.mark.parametrize(("kind", "source", "outcome"), _cred_cases())
@pytest.mark.usefixtures("current_request_with_host")
async def test_every_credential_entry_point_validates(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    _flow_env: Any,
    kind: str,
    source: str,
    outcome: str,
) -> None:
    aioclient_mock.get(f"{BASE_URL}site", **_site_for(outcome))

    entry: MockConfigEntry | None = None
    if source != "user":
        data = (
            {"token": "old"}
            if kind == "token"
            else {"auth_implementation": DOMAIN, "token": {**_TOKEN_RESPONSE, "expires_at": 9e9}}
        )
        entry = MockConfigEntry(domain=DOMAIN, data=data, options={CONF_POLL_INTERVAL: 30})
        entry.add_to_hass(hass)
        dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, _EXISTING_DEVICE)}
        )

    if source == "user":
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    elif source == "reauth":
        result = await entry.start_reauth_flow(hass)
    else:
        result = await entry.start_reconfigure_flow(hass)

    if kind == "token":
        if result["type"] is FlowResultType.MENU:
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"next_step_id": "token"}
            )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"token": "new-token"}
        )
    else:
        result = await _sign_in(hass, aioclient_mock, result)

    if outcome == "valid":
        if source == "user":
            assert result["type"] is FlowResultType.CREATE_ENTRY
        else:
            assert result["type"] is FlowResultType.ABORT
            assert result["reason"] == f"{source}_successful"
            _flow_env.assert_called_once()
        return

    if kind == "token":
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": outcome}
    else:
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == outcome
    if entry is not None:
        _flow_env.assert_not_called()
        assert entry.data.get("token") in ("old",) or kind == "signin"


# ---------------------------------------------------------------------------
# Class: option validation (D21)
# ---------------------------------------------------------------------------

_LABEL_CASES = {
    "too_long": ("x" * 65, False, "defect"),
    "control_character": ("bad\x00label", False, "defect"),
    "newline": ("two\nlines", False, "defect"),
    "max_length": ("y" * 64, True, "guard"),
    "blank": ("", True, "guard"),
    "unicode": ("Méditerranée 12", True, "guard"),
}


@pytest.mark.parametrize(
    ("label", "accepted"),
    [
        pytest.param(
            label, accepted, id=name,
            marks=[getattr(pytest.mark, mark)("D21"), pytest.mark.covers("FC-OPT-04")],
        )
        for name, (label, accepted, mark) in _LABEL_CASES.items()
    ],
)
async def test_device_label_override_is_validated(
    hass: HomeAssistant, label: str, accepted: bool
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"token": "t"}, options={CONF_POLL_INTERVAL: 30})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_POLL_INTERVAL: 30, CONF_DEVICE_LABEL: label}
    )
    if accepted:
        assert result["type"] is FlowResultType.CREATE_ENTRY
    else:
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {CONF_DEVICE_LABEL: "label_invalid"}


# The interval selector lets NaN and fractions through; the baseline replaced
# NaN with the default and truncated fractions, both without telling the user.
_OPTION_INTERVAL_CASES = {
    "nan": (float("nan"), None, "defect"),
    "fraction": (45.5, None, "defect"),
    "integral_float": (45.0, 45, "guard"),
    "integer": (45, 45, "guard"),
}


@pytest.mark.parametrize(
    ("value", "saved"),
    [
        pytest.param(
            value, saved, id=name,
            marks=[getattr(pytest.mark, mark)("D32"), pytest.mark.covers("FC-OPT-02")],
        )
        for name, (value, saved, mark) in _OPTION_INTERVAL_CASES.items()
    ],
)
async def test_options_poll_interval_is_never_silently_replaced(
    hass: HomeAssistant, value: float, saved: int | None
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"token": "t"}, options={CONF_POLL_INTERVAL: 30})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_POLL_INTERVAL: value}
    )
    if saved is None:
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {CONF_POLL_INTERVAL: "poll_interval_invalid"}
        assert entry.options[CONF_POLL_INTERVAL] == 30
    else:
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_POLL_INTERVAL] == saved


@pytest.mark.defect("D31")
@pytest.mark.covers("FC-DIAG-01")
async def test_diagnostics_report_a_failed_version_lookup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_api(aioclient_mock)
    entry = api_key_entry()
    assert await setup_entry(hass, entry)
    with patch(
        "custom_components.eleven_energy_plus.diagnostics.async_get_integration",
        side_effect=RuntimeError("loader broke"),
    ):
        dump = await async_get_config_entry_diagnostics(hass, entry)
    assert dump["integration"]["version"] == "unknown"
    assert any(
        "version" in record.getMessage().lower() and record.levelno >= logging.WARNING
        for record in caplog.records
    ), "the failed lookup was swallowed without a log line"


# ---------------------------------------------------------------------------
# Defect class "privacy" (D23)
# ---------------------------------------------------------------------------

_PII = {
    "address": "1 Solar Street",
    "postcode": "AB1 2CD",
    "postCode": "AB1 2CD",
    "latitude": 51.5,
    "longitude": -0.12,
    "email": "owner@example.com",
    "phone": "+44 20 7946 0000",
}


@pytest.mark.defect("D23")
@pytest.mark.covers("FC-DIAG-02")
@pytest.mark.parametrize("field", list(_PII))
async def test_diagnostics_redact_personal_fields(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, field: str
) -> None:
    summary = site_summary()
    site = {"devices": [summary], "site": {field: _PII[field]}, field: _PII[field]}
    reading = device_payload()
    reading["location"] = {field: _PII[field]}
    aioclient_mock.get(f"{BASE_URL}site", json=site)
    aioclient_mock.get(f"{BASE_URL}devices/{DEVICE_ID}", json=reading)
    entry = api_key_entry()
    assert await setup_entry(hass, entry)
    dump = await async_get_config_entry_diagnostics(hass, entry)
    assert str(_PII[field]) not in repr(dump)
