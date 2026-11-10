"""Tests for notifications.py: repair issues and persistent notifications.

Durations are measured on the monotonic clock; the tests replace the module's
clock with :class:`FakeClock` (never the event loop's) and move it by hand.
The optimiser timer uses UTC wall-clock time, moved with ``freezer``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
from typing import Any
from unittest.mock import patch

import pytest

from homeassistant.components import persistent_notification
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.eleven_energy_plus import (
    async_remove_config_entry_device,
    notifications,
)
from custom_components.eleven_energy_plus.const import (
    BASE_URL,
    CLOUD_UNREACHABLE_AFTER_SECONDS,
    DOMAIN,
    INVERTER_OFFLINE_AFTER_SECONDS,
    OPTIMISER_OFF_AFTER,
    UNAVAILABLE_AFTER_FAILED_POLLS,
)
from custom_components.eleven_energy_plus.controller import Controller
from custom_components.eleven_energy_plus.notifications import FaultReporter

from .common import (
    DEVICE_ID,
    MODEL_SINGLE_PHASE,
    api_key_entry,
    device_payload,
    mock_api,
    setup_entry,
    site_summary,
)

POST_URL = f"{BASE_URL}devices/{DEVICE_ID}/operatingMode"
API_CONNECTION = f"binary_sensor.{DEVICE_ID}_api_connection"
_T0 = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    """Stands in for the ``time`` module inside notifications.py."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(notifications, "time", fake)
    return fake


def _issue(hass: HomeAssistant, issue_id: str) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


def _notification(hass: HomeAssistant, name: str) -> dict[str, Any] | None:
    notes = persistent_notification._async_get_or_create_notifications(hass)
    return notes.get(f"{DOMAIN}_{name}")


async def _loaded(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, **kwargs: Any
) -> tuple[MockConfigEntry, Controller]:
    mock_api(aioclient_mock, **kwargs)
    entry = api_key_entry()
    assert await setup_entry(hass, entry)
    return entry, entry.runtime_data


def _serve(aioclient_mock: AiohttpClientMocker, **kwargs: Any) -> None:
    aioclient_mock.clear_requests()
    mock_api(aioclient_mock, **kwargs)


def _serve_errors(aioclient_mock: AiohttpClientMocker, status: int = 500) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE_URL}site", status=status)
    aioclient_mock.get(f"{BASE_URL}devices/{DEVICE_ID}", status=status)


# ---------------------------------------------------------------------------
# cloud_unreachable
# ---------------------------------------------------------------------------


@pytest.mark.covers("FC-NOTIF-01", "FC-NOTIF-08")
async def test_cloud_unreachable_after_the_threshold_then_cleared(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: FakeClock
) -> None:
    _entry, controller = await _loaded(hass, aioclient_mock)
    _serve_errors(aioclient_mock)
    await controller._poll_cycle()
    clock.now += CLOUD_UNREACHABLE_AFTER_SECONDS - 1
    await controller._poll_cycle()
    assert _issue(hass, "cloud_unreachable") is None

    clock.now += 1
    await controller._poll_cycle()
    issue = _issue(hass, "cloud_unreachable")
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_placeholders == {"minutes": "15"}
    note = _notification(hass, "cloud_unreachable")
    assert note is not None
    assert note["title"] == "Eleven Energy cloud unreachable"
    assert "15 minutes" in note["message"]

    _serve(aioclient_mock)
    await controller._poll_cycle()
    assert _issue(hass, "cloud_unreachable") is None
    assert _notification(hass, "cloud_unreachable") is None


@pytest.mark.covers("FC-NOTIF-01")
async def test_a_reachable_site_with_failing_devices_is_not_an_outage(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: FakeClock
) -> None:
    _entry, controller = await _loaded(hass, aioclient_mock)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE_URL}site", json={"devices": [site_summary()]})
    aioclient_mock.get(f"{BASE_URL}devices/{DEVICE_ID}", status=500)
    await controller._poll_cycle()
    clock.now += CLOUD_UNREACHABLE_AFTER_SECONDS + 1
    await controller._poll_cycle()
    assert _issue(hass, "cloud_unreachable") is None


@pytest.mark.covers("FC-TIME-01")
async def test_a_wall_clock_jump_does_not_count_as_outage_time(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    clock: FakeClock,
    freezer: Any,
) -> None:
    _entry, controller = await _loaded(hass, aioclient_mock)
    _serve_errors(aioclient_mock)
    await controller._poll_cycle()
    freezer.tick(timedelta(days=2))
    await controller._poll_cycle()
    assert _issue(hass, "cloud_unreachable") is None


@pytest.mark.covers("FC-NOTIF-01")
async def test_being_rate_limited_is_not_an_outage(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: FakeClock
) -> None:
    _entry, controller = await _loaded(hass, aioclient_mock)
    _serve_errors(aioclient_mock, status=429)
    await controller._poll_cycle()
    clock.now += CLOUD_UNREACHABLE_AFTER_SECONDS + 1
    controller._polls_deferred_until = 0
    await controller._poll_cycle()
    assert _issue(hass, "cloud_unreachable") is None


# ---------------------------------------------------------------------------
# inverter_offline and inverter_missing
# ---------------------------------------------------------------------------


@pytest.mark.covers("FC-NOTIF-02")
async def test_inverter_offline_after_the_threshold_then_cleared(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: FakeClock
) -> None:
    _entry, controller = await _loaded(hass, aioclient_mock)
    offline = device_payload()
    offline["online"] = False
    _serve(aioclient_mock, devices={DEVICE_ID: offline})
    await controller.poll_devices()
    clock.now += INVERTER_OFFLINE_AFTER_SECONDS - 1
    await controller.poll_devices()
    assert _issue(hass, f"inverter_offline_{DEVICE_ID}") is None

    clock.now += 1
    await controller.poll_devices()
    issue = _issue(hass, f"inverter_offline_{DEVICE_ID}")
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_placeholders["name"] == "Eleven Energy Plus North Sea 6 (6kW)"
    assert _notification(hass, f"inverter_offline_{DEVICE_ID}") is not None

    _serve(aioclient_mock)
    await controller.poll_devices()
    assert _issue(hass, f"inverter_offline_{DEVICE_ID}") is None
    assert _notification(hass, f"inverter_offline_{DEVICE_ID}") is None


@pytest.mark.covers("FC-NOTIF-03", "FC-DISC-05")
async def test_inverter_missing_is_raised_and_cleared_with_the_listing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    devices = {"a": device_payload("a"), "b": device_payload("b")}
    _entry, controller = await _loaded(hass, aioclient_mock, devices=devices)
    _serve(aioclient_mock, devices=devices, summaries=[site_summary("a")])
    await controller.poll_site()
    assert _issue(hass, "inverter_missing_b") is None
    await controller.poll_site()
    assert _issue(hass, "inverter_missing_b") is not None
    assert _notification(hass, "inverter_missing_b") is not None

    _serve(aioclient_mock, devices=devices)
    await controller.poll_site()
    assert _issue(hass, "inverter_missing_b") is None
    assert _notification(hass, "inverter_missing_b") is None


@pytest.mark.covers("FC-DISC-05", "FC-NOTIF-03")
async def test_a_departed_inverter_can_be_deleted_and_a_listed_one_cannot(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    devices = {"a": device_payload("a"), "b": device_payload("b")}
    entry, controller = await _loaded(hass, aioclient_mock, devices=devices)
    device_b = next(
        device
        for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
        if (DOMAIN, "b") in device.identifiers
    )
    assert not await async_remove_config_entry_device(hass, entry, device_b)

    _serve(aioclient_mock, devices=devices, summaries=[site_summary("a")])
    await controller.poll_site()
    await controller.poll_site()
    assert await async_remove_config_entry_device(hass, entry, device_b)
    assert "b" not in controller.devices
    assert _issue(hass, "inverter_missing_b") is None


async def test_any_device_may_be_deleted_while_the_entry_is_not_loaded(
    hass: HomeAssistant,
) -> None:
    entry = api_key_entry()
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "a")}
    )
    assert await async_remove_config_entry_device(hass, entry, device)


# ---------------------------------------------------------------------------
# work_mode_failed and optimiser_off
# ---------------------------------------------------------------------------


@pytest.mark.covers("FC-NOTIF-04")
@pytest.mark.parametrize(
    ("arrange", "issue"),
    [
        ("api_error", True),
        ("credential_rejected", True),
        ("internal_error", True),
        ("invalid_request", False),
    ],
)
async def test_work_mode_failed_for_api_auth_and_internal_failures_only(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    instant_backoff: list[float],
    arrange: str,
    issue: bool,
) -> None:
    _entry, controller = await _loaded(hass, aioclient_mock)
    data: dict[str, Any] = {}
    if arrange == "api_error":
        aioclient_mock.post(POST_URL, status=500)
    elif arrange == "credential_rejected":
        aioclient_mock.post(POST_URL, status=401)
    elif arrange == "internal_error":
        aioclient_mock.post(POST_URL, status=200)
        patcher = patch.object(controller, "_sanitise_work_mode", side_effect=KeyError("bug"))
        patcher.start()
    else:
        data = {"percent_to_battery": 250}
    result = await controller.set_work_mode("set_work_mode_self_consumption", data)
    assert result["error_kind"] == arrange
    raised = _issue(hass, f"work_mode_failed_{DEVICE_ID}")
    assert (raised is not None) is issue
    if issue:
        assert raised is not None
        assert raised.severity is ir.IssueSeverity.ERROR
        assert raised.translation_placeholders["error"] == result["error"]
    if arrange == "internal_error":
        patch.stopall()


@pytest.mark.covers("FC-NOTIF-04", "FC-NOTIF-08")
async def test_work_mode_failed_is_updated_not_renotified_and_cleared_on_success(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, instant_backoff: list[float]
) -> None:
    _entry, controller = await _loaded(hass, aioclient_mock)
    aioclient_mock.post(POST_URL, status=500)
    await controller.set_work_mode("set_work_mode_self_consumption", {})
    issue_id = f"work_mode_failed_{DEVICE_ID}"
    assert _notification(hass, issue_id) is not None
    persistent_notification.async_dismiss(hass, f"{DOMAIN}_{issue_id}")

    aioclient_mock.clear_requests()
    aioclient_mock.post(POST_URL, status=400)
    await controller.set_work_mode("set_work_mode_self_consumption", {})
    issue = _issue(hass, issue_id)
    assert issue is not None
    assert issue.translation_placeholders["error"] == "API returned status 400"
    assert _notification(hass, issue_id) is None, "shown once, when the fault appeared"

    aioclient_mock.clear_requests()
    aioclient_mock.post(POST_URL, status=200)
    await controller.set_work_mode("set_work_mode_self_consumption", {})
    assert _issue(hass, issue_id) is None


@pytest.mark.covers("FC-NOTIF-05")
async def test_optimiser_off_after_24_hours_and_cleared_by_reset(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: Any
) -> None:
    freezer.move_to(_T0)
    _entry, controller = await _loaded(hass, aioclient_mock)
    aioclient_mock.post(POST_URL, status=200)
    await controller.set_work_mode("set_work_mode_pv_export", {})
    freezer.move_to(_T0 + timedelta(hours=1))
    await controller.set_work_mode("set_work_mode_self_consumption", {})
    names = {DEVICE_ID: "Inverter"}

    freezer.move_to(_T0 + OPTIMISER_OFF_AFTER - timedelta(seconds=1))
    controller.faults.check_optimiser(names)
    assert _issue(hass, f"optimiser_off_{DEVICE_ID}") is None

    freezer.move_to(_T0 + OPTIMISER_OFF_AFTER)
    controller.faults.check_optimiser(names)
    issue = _issue(hass, f"optimiser_off_{DEVICE_ID}")
    assert issue is not None, "measured from the first mode set, not the latest"
    assert issue.translation_placeholders == {"name": "Inverter", "hours": "24"}

    await controller.set_work_mode("set_work_mode_reset", {})
    assert _issue(hass, f"optimiser_off_{DEVICE_ID}") is None
    assert controller.faults.optimiser_off_since == {}


@pytest.mark.covers("FC-NOTIF-06")
async def test_optimiser_timer_survives_a_restart(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
    freezer: Any,
) -> None:
    freezer.move_to(_T0)
    entry, controller = await _loaded(hass, aioclient_mock)
    aioclient_mock.post(POST_URL, status=200)
    await controller.set_work_mode("set_work_mode_idle_battery", {})
    assert await hass.config_entries.async_unload(entry.entry_id)
    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]
    assert stored == {"optimiser_off_since": {DEVICE_ID: _T0.isoformat()}}

    freezer.move_to(_T0 + OPTIMISER_OFF_AFTER)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    controller = entry.runtime_data
    assert controller.faults.optimiser_off_since == {DEVICE_ID: _T0.isoformat()}
    await controller._poll_cycle()
    assert _issue(hass, f"optimiser_off_{DEVICE_ID}") is not None


@pytest.mark.covers("FC-NOTIF-07", "FC-TIME-01")
async def test_a_clock_moving_back_restarts_the_optimiser_timer(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    freezer.move_to(_T0)
    _entry, controller = await _loaded(hass, aioclient_mock)
    aioclient_mock.post(POST_URL, status=200)
    await controller.set_work_mode("set_work_mode_pv_export", {})
    names = {DEVICE_ID: "Inverter"}

    restarted_at = _T0 - timedelta(hours=3)
    freezer.move_to(restarted_at)
    controller.faults.check_optimiser(names)
    assert "clock moved back" in caplog.text
    assert controller.faults.optimiser_off_since == {DEVICE_ID: restarted_at.isoformat()}

    freezer.move_to(restarted_at + OPTIMISER_OFF_AFTER - timedelta(seconds=1))
    controller.faults.check_optimiser(names)
    assert _issue(hass, f"optimiser_off_{DEVICE_ID}") is None
    freezer.move_to(restarted_at + OPTIMISER_OFF_AFTER)
    controller.faults.check_optimiser(names)
    assert _issue(hass, f"optimiser_off_{DEVICE_ID}") is not None


@pytest.mark.covers("FC-TIME-03")
async def test_optimiser_timer_counts_real_hours_across_dst(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: Any
) -> None:
    """Clocks go back in the UK on 25 October 2026; the timer still counts 24 real hours."""
    await hass.config.async_set_time_zone("Europe/London")
    start = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)
    freezer.move_to(start)
    _entry, controller = await _loaded(hass, aioclient_mock)
    aioclient_mock.post(POST_URL, status=200)
    await controller.set_work_mode("set_work_mode_pv_export", {})
    names = {DEVICE_ID: "Inverter"}
    # Same local wall-clock time a day later is only 23 real hours after the start.
    freezer.move_to(start + timedelta(hours=23))
    controller.faults.check_optimiser(names)
    assert _issue(hass, f"optimiser_off_{DEVICE_ID}") is None
    freezer.move_to(start + timedelta(hours=24))
    controller.faults.check_optimiser(names)
    assert _issue(hass, f"optimiser_off_{DEVICE_ID}") is not None


async def test_timers_of_inverters_no_longer_known_are_dropped(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: Any
) -> None:
    freezer.move_to(_T0)
    _entry, controller = await _loaded(hass, aioclient_mock)
    aioclient_mock.post(POST_URL, status=200)
    await controller.set_work_mode("set_work_mode_pv_export", {})
    freezer.move_to(_T0 + OPTIMISER_OFF_AFTER)
    controller.faults.check_optimiser({DEVICE_ID: "Inverter"})
    assert _issue(hass, f"optimiser_off_{DEVICE_ID}") is not None
    controller.faults.check_optimiser({})
    assert controller.faults.optimiser_off_since == {}
    assert _issue(hass, f"optimiser_off_{DEVICE_ID}") is None


@pytest.mark.parametrize(
    "stored",
    [
        {"optimiser_off_since": {DEVICE_ID: "not a time"}},
        {"optimiser_off_since": {DEVICE_ID: "2026-11-09T12:00:00"}},
        {"optimiser_off_since": {DEVICE_ID: 12}},
    ],
    ids=["unparseable", "naive", "not_text"],
)
async def test_unreadable_stored_timers_are_discarded_and_reported(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    stored: dict[str, Any],
) -> None:
    entry = api_key_entry()
    entry.add_to_hass(hass)
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {"version": 1, "minor_version": 1, "key": f"{DOMAIN}.{entry.entry_id}", "data": stored}
    reporter = FaultReporter(hass, entry)
    await reporter.async_load()
    assert reporter.optimiser_off_since == {}
    assert "unreadable optimiser timer" in caplog.text


@pytest.mark.covers("FC-NOTIF-06")
async def test_one_unreadable_timer_does_not_lose_the_others(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    entry = api_key_entry()
    entry.add_to_hass(hass)
    key = f"{DOMAIN}.{entry.entry_id}"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"optimiser_off_since": {"bad": "never", "good": _T0.isoformat()}},
    }
    reporter = FaultReporter(hass, entry)
    await reporter.async_load()
    assert reporter.optimiser_off_since == {"good": _T0.isoformat()}


@pytest.mark.parametrize("stored", [None, ["x"], {"optimiser_off_since": ["x"]}], ids=["none", "list", "timers_list"])
async def test_missing_or_odd_storage_starts_empty(
    hass: HomeAssistant, hass_storage: dict[str, Any], stored: Any
) -> None:
    entry = api_key_entry()
    entry.add_to_hass(hass)
    if stored is not None:
        hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {"version": 1, "minor_version": 1, "key": f"{DOMAIN}.{entry.entry_id}", "data": stored}
    reporter = FaultReporter(hass, entry)
    await reporter.async_load()
    assert reporter.optimiser_off_since == {}


# ---------------------------------------------------------------------------
# Rejected credential
# ---------------------------------------------------------------------------


@pytest.mark.covers("FC-NOTIF-09", "FC-AUTH-06")
async def test_rejected_credential_notification_is_dismissed_on_recovery(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("custom_components.eleven_energy_plus.controller.AUTH_RECOVERY_SECONDS", 0)
    _entry, controller = await _loaded(hass, aioclient_mock)
    _serve_errors(aioclient_mock, status=401)
    await controller.poll_devices()
    await controller.poll_devices()
    note = _notification(hass, "credential_rejected")
    assert note is not None
    assert note["title"] == "Eleven Energy Plus needs you to reauthenticate"

    _serve(aioclient_mock)
    await controller._poll_cycle()
    assert not controller.auth_failed
    assert _notification(hass, "credential_rejected") is None


@pytest.mark.covers("FC-AUTH-07")
async def test_a_refused_sign_in_refresh_is_not_retested(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("custom_components.eleven_energy_plus.controller.AUTH_RECOVERY_SECONDS", 0)
    _entry, controller = await _loaded(hass, aioclient_mock)
    controller._handle_auth_failure(None)
    _serve(aioclient_mock)
    assert not await controller._check_auth_recovered()
    assert controller.auth_failed
    assert aioclient_mock.call_count == 0


@pytest.mark.covers("FC-AUTH-08", "FC-TIME-01")
async def test_the_credential_is_retested_exactly_when_due_and_polling_resumes_at_once(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.eleven_energy_plus import controller as controller_module

    controller_clock = FakeClock()
    monkeypatch.setattr(controller_module, "time", controller_clock)
    _entry, controller = await _loaded(hass, aioclient_mock)
    _serve_errors(aioclient_mock, status=401)
    await controller.poll_devices()
    _serve(aioclient_mock)

    controller_clock.now += controller_module.AUTH_RECOVERY_SECONDS - 0.001
    await controller._poll_cycle()
    assert controller.auth_failed, "not due a millisecond early"
    assert aioclient_mock.call_count == 0

    controller_clock.now += 0.001
    await controller._poll_cycle()
    assert not controller.auth_failed
    await hass.async_block_till_done()
    assert hass.states.get(f"sensor.{DEVICE_ID}_pv_power").state != STATE_UNAVAILABLE, (
        "the cycle that sees the credential work again also polls"
    )


@pytest.mark.covers("FC-AUTH-08")
async def test_recovery_is_retested_only_when_due_and_survives_errors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    _entry, controller = await _loaded(hass, aioclient_mock)
    _serve_errors(aioclient_mock, status=401)
    await controller.poll_devices()
    calls = aioclient_mock.call_count
    assert not await controller._check_auth_recovered(), "not due yet"
    assert aioclient_mock.call_count == calls

    monkeypatch.setattr("custom_components.eleven_energy_plus.controller.AUTH_RECOVERY_SECONDS", 0)
    assert not await controller._check_auth_recovered(), "still rejected"
    _serve_errors(aioclient_mock, status=500)
    assert not await controller._check_auth_recovered(), "unreachable is not recovery"
    assert controller.auth_failed


# ---------------------------------------------------------------------------
# Unload and removal
# ---------------------------------------------------------------------------


@pytest.mark.covers("FC-NOTIF-10", "FC-LIFE-05")
async def test_unload_withdraws_everything_and_removal_deletes_stored_state(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
    clock: FakeClock,
) -> None:
    entry, controller = await _loaded(hass, aioclient_mock)
    aioclient_mock.post(POST_URL, status=200)
    await controller.set_work_mode("set_work_mode_pv_export", {})
    controller.faults.report_missing(DEVICE_ID, "Inverter", True)
    controller.faults.report_credential(True)
    assert _issue(hass, f"inverter_missing_{DEVICE_ID}") is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert _issue(hass, f"inverter_missing_{DEVICE_ID}") is None
    assert _notification(hass, f"inverter_missing_{DEVICE_ID}") is None
    assert _notification(hass, "credential_rejected") is None
    assert f"{DOMAIN}.{entry.entry_id}" in hass_storage

    ir.async_create_issue(
        hass, DOMAIN, "left_over", is_fixable=False,
        severity=ir.IssueSeverity.WARNING, translation_key="cloud_unreachable",
    )
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert f"{DOMAIN}.{entry.entry_id}" not in hass_storage
    assert _issue(hass, "left_over") is None


# ---------------------------------------------------------------------------
# Texts
# ---------------------------------------------------------------------------


async def test_texts_are_loaded_in_the_configured_language(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[Any] = []
    real = notifications.async_get_translations

    async def _spy(hass_: HomeAssistant, language: str, *args: Any) -> dict[str, str]:
        asked.append(language)
        return await real(hass_, language, *args)

    monkeypatch.setattr(notifications, "async_get_translations", _spy)
    await hass.config.async_update(language="de")
    entry = api_key_entry()
    entry.add_to_hass(hass)
    reporter = FaultReporter(hass, entry)
    await reporter.async_load()
    assert asked == ["de"]
    reporter.raise_issue("x", "cloud_unreachable", ir.IssueSeverity.ERROR, {"minutes": "15"})
    note = _notification(hass, "x")
    assert note is not None
    assert note["title"] == "Eleven Energy cloud unreachable", "English is the fallback"


async def test_texts_fall_back_and_leave_unknown_placeholders(hass: HomeAssistant) -> None:
    entry = api_key_entry()
    entry.add_to_hass(hass)
    reporter = FaultReporter(hass, entry)
    await reporter.async_load()
    reporter.raise_issue("x", "not_a_key", ir.IssueSeverity.WARNING, {})
    note = _notification(hass, "x")
    assert note is not None
    assert note["title"] == "not_a_key"
    reporter.raise_issue("y", "inverter_missing", ir.IssueSeverity.WARNING, {})
    note = _notification(hass, "y")
    assert note is not None
    assert note["title"] == "{name} is no longer on the site"


async def test_clearing_an_issue_that_is_not_raised_does_nothing(hass: HomeAssistant) -> None:
    entry = api_key_entry()
    entry.add_to_hass(hass)
    reporter = FaultReporter(hass, entry)
    reporter.clear_issue("never_raised")
    reporter.report_credential(False)
    reporter.forget_device("never_known")
    assert reporter.active_issues == []


@pytest.mark.covers("FC-NOTIF-09")
async def test_the_credential_notification_is_shown_once(hass: HomeAssistant) -> None:
    entry = api_key_entry()
    entry.add_to_hass(hass)
    reporter = FaultReporter(hass, entry)
    reporter.report_credential(True)
    persistent_notification.async_dismiss(hass, f"{DOMAIN}_credential_rejected")
    reporter.report_credential(True)
    assert _notification(hass, "credential_rejected") is None, "not shown again while rejected"


async def test_forgetting_an_inverter_drops_its_timer_and_issues(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: Any
) -> None:
    freezer.move_to(_T0)
    entry = api_key_entry()
    entry.add_to_hass(hass)
    reporter = FaultReporter(hass, entry)
    reporter.record_work_mode(DEVICE_ID, "set_work_mode_pv_export")
    reporter.report_missing(DEVICE_ID, "Inverter", True)
    reporter.forget_device(DEVICE_ID)
    assert reporter.optimiser_off_since == {}
    assert reporter.active_issues == []
    async_fire_time_changed(hass, _T0 + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"] == {"optimiser_off_since": {}}


@pytest.mark.covers("FC-NOTIF-10")
async def test_removal_leaves_other_integrations_issues_alone(hass: HomeAssistant) -> None:
    entry = api_key_entry()
    entry.add_to_hass(hass)
    ir.async_create_issue(
        hass, "other_domain", "theirs", is_fixable=False,
        severity=ir.IssueSeverity.WARNING, translation_key="x",
    )
    await FaultReporter.async_remove(hass, entry)
    assert ir.async_get(hass).async_get_issue("other_domain", "theirs") is not None


# ---------------------------------------------------------------------------
# The API Connection sensor (FC-ENT-10, X-AVAIL-api_connection-*)
# ---------------------------------------------------------------------------


async def _cause(cause: str, controller: Controller, aioclient_mock: AiohttpClientMocker) -> None:
    if cause == "poll_failures":
        _serve_errors(aioclient_mock)
        for _ in range(UNAVAILABLE_AFTER_FAILED_POLLS):
            await controller.poll_devices()
    elif cause == "auth":
        _serve_errors(aioclient_mock, status=401)
        await controller.poll_devices()
    elif cause == "offline":
        offline = device_payload(model=MODEL_SINGLE_PHASE)
        offline["online"] = False
        _serve(aioclient_mock, devices={DEVICE_ID: offline})
        await controller.poll_devices()
    elif cause == "missing":
        _serve(aioclient_mock, summaries=[])
        await controller.poll_site()
        await controller.poll_site()
    else:
        await controller.terminate()


_API_CONNECTION_EXPECTED = {
    "poll_failures": STATE_OFF,
    "auth": STATE_OFF,
    "offline": STATE_ON,
    "missing": STATE_OFF,
    "shutdown": STATE_UNAVAILABLE,
}


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        pytest.param(
            cause, expected, id=cause,
            marks=pytest.mark.covers(f"X-AVAIL-api_connection-{cause}", "FC-ENT-10"),
        )
        for cause, expected in _API_CONNECTION_EXPECTED.items()
    ],
)
async def test_api_connection_explains_the_outage(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, cause: str, expected: str
) -> None:
    """It stays available (until shutdown) and says whether polling works."""
    _entry, controller = await _loaded(hass, aioclient_mock)
    assert hass.states.get(API_CONNECTION).state == STATE_ON
    await _cause(cause, controller, aioclient_mock)
    await hass.async_block_till_done()
    assert hass.states.get(API_CONNECTION).state == expected


@pytest.mark.covers("FC-ENT-10")
async def test_api_connection_goes_off_on_the_first_failure_and_recovers(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _entry, controller = await _loaded(hass, aioclient_mock)
    _serve_errors(aioclient_mock)
    await controller.poll_devices()
    await hass.async_block_till_done()
    assert hass.states.get(API_CONNECTION).state == STATE_OFF
    assert hass.states.get(f"sensor.{DEVICE_ID}_pv_power").state != STATE_UNAVAILABLE
    _serve(aioclient_mock)
    await controller.poll_devices()
    await hass.async_block_till_done()
    assert hass.states.get(API_CONNECTION).state == STATE_ON


@pytest.mark.covers("FC-ENT-10")
async def test_api_connection_entity_is_diagnostic_connectivity(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _loaded(hass, aioclient_mock)
    state = hass.states.get(API_CONNECTION)
    assert state.attributes["device_class"] == "connectivity"
    assert state.attributes["friendly_name"].endswith("API Connection")


def test_log_levels_cover_every_severity() -> None:
    assert set(notifications._SEVERITY_LOG_LEVEL) >= {
        ir.IssueSeverity.ERROR,
        ir.IssueSeverity.WARNING,
    }
    assert notifications._SEVERITY_LOG_LEVEL[ir.IssueSeverity.ERROR] == logging.ERROR
