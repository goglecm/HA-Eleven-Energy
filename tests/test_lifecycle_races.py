"""Overlaps: what happens when lifecycle steps land in the middle of things.

Everything runs on Home Assistant's single event loop, so two pieces of code
interleave only at an ``await``. The cases here are those where a user action
or a lifecycle step - deleting a device, unloading the entry, an inverter
coming back to the site listing, the credential being rejected - lands while
a poll or a command is waiting on the network, or where a timer keeps running
through a period in which its condition could not be observed.

Ledger entries D36 to D38, D40 and D41 (``tests/DEFECTS.md``); they were
introduced by the audit itself, so ``check_red.py`` proves them against
``db7904c``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
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
)
from custom_components.eleven_energy_plus.controller import Controller
from custom_components.eleven_energy_plus.notifications import FaultReporter

from .common import (
    DEVICE_ID,
    api_key_entry,
    device_payload,
    mock_api,
    setup_entry,
    site_summary,
)

PV_POWER = f"sensor.{DEVICE_ID}_pv_power"


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


def _issues(hass: HomeAssistant) -> list[str]:
    return sorted(
        issue_id for domain, issue_id in ir.async_get(hass).issues if domain == DOMAIN
    )


def _notifications(hass: HomeAssistant) -> list[str]:
    notes = persistent_notification._async_get_or_create_notifications(hass)
    return sorted(note for note in notes if note.startswith(f"{DOMAIN}_"))


def _device(hass: HomeAssistant, entry: MockConfigEntry, device_id: str) -> dr.DeviceEntry:
    return next(
        device
        for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
        if (DOMAIN, device_id) in device.identifiers
    )


async def _make_missing(
    controller: Controller, aioclient_mock: AiohttpClientMocker, devices: dict[str, Any], listed: list[str]
) -> None:
    _serve(aioclient_mock, devices=devices, summaries=[site_summary(d) for d in listed])
    await controller.poll_site()
    await controller.poll_site()


# ---------------------------------------------------------------------------
# D36: a cause of unavailability that clears without a fresh reading
# ---------------------------------------------------------------------------


@pytest.mark.covers("FC-AVAIL-01", "FC-DISC-05")
@pytest.mark.parametrize(
    "cause",
    [
        pytest.param("missing", marks=pytest.mark.defect("D36")),
        pytest.param("credential", marks=pytest.mark.guard("D36")),
    ],
)
async def test_an_inverter_that_comes_back_shows_nothing_old_until_read(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
    cause: str,
) -> None:
    _entry, controller = await _loaded(hass, aioclient_mock)
    assert hass.states.get(PV_POWER).state != STATE_UNAVAILABLE

    if cause == "missing":
        await _make_missing(controller, aioclient_mock, {DEVICE_ID: device_payload()}, [])
        aioclient_mock.clear_requests()
        aioclient_mock.get(f"{BASE_URL}site", json={"devices": [site_summary()]})
        aioclient_mock.get(f"{BASE_URL}devices/{DEVICE_ID}", status=500)
        await controller.poll_site()
        assert not controller.devices[DEVICE_ID].missing
    else:
        monkeypatch.setattr(
            "custom_components.eleven_energy_plus.controller.AUTH_RECOVERY_SECONDS", 0
        )
        aioclient_mock.clear_requests()
        aioclient_mock.get(f"{BASE_URL}devices/{DEVICE_ID}", status=401)
        await controller.poll_devices()
        aioclient_mock.clear_requests()
        aioclient_mock.get(f"{BASE_URL}site", json={"devices": [site_summary()]})
        aioclient_mock.get(f"{BASE_URL}devices/{DEVICE_ID}", status=500)
        assert await controller._check_auth_recovered()

    await controller.poll_devices()
    await hass.async_block_till_done()
    assert hass.states.get(PV_POWER).state == STATE_UNAVAILABLE, (
        "a value from before the outage must not come back as current"
    )

    _serve(aioclient_mock)
    await controller.poll_devices()
    await hass.async_block_till_done()
    assert hass.states.get(PV_POWER).state != STATE_UNAVAILABLE


# ---------------------------------------------------------------------------
# D37: reports that arrive after the entry was unloaded
# ---------------------------------------------------------------------------


def _work_mode_failed(reporter: FaultReporter, clock: FakeClock) -> None:
    reporter.report_work_mode_result(DEVICE_ID, "Inverter", "API returned status 400")


def _missing(reporter: FaultReporter, clock: FakeClock) -> None:
    reporter.report_missing(DEVICE_ID, "Inverter", True)


def _offline(reporter: FaultReporter, clock: FakeClock) -> None:
    reporter.report_offline(DEVICE_ID, "Inverter", True)
    clock.now += INVERTER_OFFLINE_AFTER_SECONDS
    reporter.report_offline(DEVICE_ID, "Inverter", True)


def _cloud(reporter: FaultReporter, clock: FakeClock) -> None:
    reporter.report_poll_cycle(False)
    clock.now += CLOUD_UNREACHABLE_AFTER_SECONDS
    reporter.report_poll_cycle(False)


def _credential(reporter: FaultReporter, clock: FakeClock) -> None:
    reporter.report_credential(True)


def _optimiser(reporter: FaultReporter, clock: FakeClock) -> None:
    reporter.record_work_mode(DEVICE_ID, "set_work_mode_pv_export")
    reporter._optimiser_off_since[DEVICE_ID] = dt_util.utcnow() - OPTIMISER_OFF_AFTER
    reporter.check_optimiser({DEVICE_ID: "Inverter"})


_AFTER_UNLOAD: dict[str, Callable[[FaultReporter, FakeClock], None]] = {
    "work_mode_failed": _work_mode_failed,
    "inverter_missing": _missing,
    "inverter_offline": _offline,
    "cloud_unreachable": _cloud,
    "credential_rejected": _credential,
    "optimiser_off": _optimiser,
}


@pytest.mark.defect("D37")
@pytest.mark.covers("FC-NOTIF-10")
@pytest.mark.parametrize("report", list(_AFTER_UNLOAD))
async def test_nothing_is_shown_once_the_reporter_is_unloaded(
    hass: HomeAssistant, clock: FakeClock, report: str
) -> None:
    entry = api_key_entry()
    entry.add_to_hass(hass)
    reporter = FaultReporter(hass, entry)
    await reporter.async_load()
    await reporter.async_unload()

    _AFTER_UNLOAD[report](reporter, clock)
    assert _issues(hass) == [], "nothing is left monitoring to clear it"
    assert _notifications(hass) == []
    assert reporter.active_issues == []


@pytest.mark.defect("D37")
@pytest.mark.covers("FC-NOTIF-05", "FC-NOTIF-10")
async def test_an_unloaded_reporter_stores_nothing(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: Any
) -> None:
    entry = api_key_entry()
    entry.add_to_hass(hass)
    reporter = FaultReporter(hass, entry)
    await reporter.async_load()
    await reporter.async_unload()
    stored = dict(hass_storage[f"{DOMAIN}.{entry.entry_id}"])
    reporter.record_work_mode(DEVICE_ID, "set_work_mode_pv_export")
    freezer.tick(timedelta(seconds=5))
    await hass.async_block_till_done()
    assert reporter.optimiser_off_since == {}, (
        "the entry's next reporter owns the stored timers; a write from this "
        "one could overwrite what that one saved"
    )
    assert hass_storage[f"{DOMAIN}.{entry.entry_id}"] == stored


def _gated_post(controller: Controller, status: int) -> tuple[asyncio.Event, asyncio.Event]:
    """Make the controller's POSTs wait for ``release``; ``started`` is set on entry."""
    started = asyncio.Event()
    release = asyncio.Event()
    real = controller._request

    async def request(
        method: str, url_suffix: str, *, json: dict[str, Any] | None = None
    ) -> tuple[int, Any, str | None]:
        if method != "POST":
            return await real(method, url_suffix, json=json)
        started.set()
        await release.wait()
        return status, None, None

    patch.object(controller, "_request", request).start()
    return started, release


@pytest.mark.defect("D37")
@pytest.mark.covers("FC-NOTIF-10", "FC-LIFE-05")
async def test_a_command_still_running_at_unload_leaves_nothing_behind(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry, controller = await _loaded(hass, aioclient_mock)
    started, release = _gated_post(controller, status=400)
    command = hass.async_create_task(controller.set_work_mode("set_work_mode_pv_export", {}))
    await started.wait()

    assert await hass.config_entries.async_unload(entry.entry_id)
    release.set()
    result = await command
    patch.stopall()
    assert not result["success"]
    assert _issues(hass) == []
    assert _notifications(hass) == []


@pytest.mark.defect("D37")
@pytest.mark.covers("FC-NOTIF-04", "FC-NOTIF-05", "FC-LIFE-04")
@pytest.mark.parametrize("status", [400, 200], ids=["fails", "succeeds"])
async def test_a_command_finishing_after_a_reload_reports_to_the_new_controller(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int
) -> None:
    entry, old = await _loaded(hass, aioclient_mock)
    started, release = _gated_post(old, status=status)
    command = hass.async_create_task(old.set_work_mode("set_work_mode_pv_export", {}))
    await started.wait()

    assert await hass.config_entries.async_reload(entry.entry_id)
    new = entry.runtime_data
    assert new is not old
    release.set()
    await command
    patch.stopall()

    if status == 200:
        assert DEVICE_ID in new.faults.optimiser_off_since, (
            "the work mode was set, so the running controller must time it"
        )
        return
    assert f"work_mode_failed_{DEVICE_ID}" in new.faults.active_issues
    aioclient_mock.post(f"{BASE_URL}devices/{DEVICE_ID}/operatingMode", status=200)
    assert (await new.set_work_mode("set_work_mode_pv_export", {}))["success"]
    assert _issues(hass) == [], "the next success clears it, as for any failure"


# ---------------------------------------------------------------------------
# D38: results that arrive for an inverter deleted in the meantime
# ---------------------------------------------------------------------------


@pytest.mark.covers("FC-DISC-05", "FC-NOTIF-04")
@pytest.mark.parametrize(
    "status",
    [
        pytest.param(400, marks=pytest.mark.defect("D38"), id="fails"),
        # A success wakes the poller, whose next cycle drops the timer.
        pytest.param(200, marks=pytest.mark.guard("D38"), id="succeeds"),
    ],
)
async def test_a_command_to_an_inverter_deleted_meanwhile_reports_nothing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int
) -> None:
    devices = {"a": device_payload("a"), "b": device_payload("b")}
    entry, controller = await _loaded(hass, aioclient_mock, devices=devices)
    await _make_missing(controller, aioclient_mock, devices, ["a"])
    device_b = _device(hass, entry, "b")
    started, release = _gated_post(controller, status=status)
    command = hass.async_create_task(
        controller.set_work_mode("set_work_mode_pv_export", {"device_id": device_b.id})
    )
    await started.wait()

    assert await async_remove_config_entry_device(hass, entry, device_b)
    release.set()
    await command
    patch.stopall()
    assert "work_mode_failed_b" not in _issues(hass)
    assert f"{DOMAIN}_work_mode_failed_b" not in _notifications(hass)
    assert "b" not in controller.faults.optimiser_off_since


@pytest.mark.defect("D38")
@pytest.mark.covers("FC-DISC-05")
async def test_a_reading_for_an_inverter_deleted_meanwhile_is_dropped(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Latent today: only an inverter missing from the listing can be deleted,
    # and those are not polled. The poll must not depend on that rule.
    monkeypatch.setattr(notifications, "INVERTER_OFFLINE_AFTER_SECONDS", 0)
    devices = {"a": device_payload("a"), "b": device_payload("b")}
    _entry, controller = await _loaded(hass, aioclient_mock, devices=devices)
    offline = device_payload("a")
    offline["online"] = False
    fresh = device_payload("b")
    fresh["pv"]["power"] = 4.2
    _serve(aioclient_mock, devices={"a": offline, "b": fresh})
    real_fetch = controller._fetch_device

    async def fetch_then_forget(device_id: str) -> dict[str, Any]:
        payload = await real_fetch(device_id)
        if device_id == "a":
            controller.forget_device(device_id)
        return payload

    monkeypatch.setattr(controller, "_fetch_device", fetch_then_forget)
    await controller.poll_devices()
    assert "a" not in controller.last_device_payloads
    assert _issues(hass) == []
    assert controller.last_device_payloads["b"]["pv"]["power"] == 4.2, (
        "the inverters after the deleted one are still read"
    )


# ---------------------------------------------------------------------------
# D40: outage timers running through time the condition was not observed
# ---------------------------------------------------------------------------


@pytest.mark.defect("D40")
@pytest.mark.covers("FC-NOTIF-01", "FC-AUTH-06")
async def test_a_rejected_credential_proves_the_cloud_was_reachable(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: FakeClock
) -> None:
    _entry, controller = await _loaded(hass, aioclient_mock)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE_URL}site", status=500)
    aioclient_mock.get(f"{BASE_URL}devices/{DEVICE_ID}", status=500)
    await controller._poll_cycle()

    clock.now += CLOUD_UNREACHABLE_AFTER_SECONDS - 60
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE_URL}devices/{DEVICE_ID}", status=401)
    await controller.poll_devices()
    assert controller.auth_failed

    clock.now += 120
    controller._recover_auth()
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE_URL}site", status=500)
    aioclient_mock.get(f"{BASE_URL}devices/{DEVICE_ID}", status=500)
    await controller._poll_cycle()
    assert "cloud_unreachable" not in _issues(hass), (
        "the API answered two minutes ago; that is not 15 minutes unreachable"
    )


@pytest.mark.defect("D40")
@pytest.mark.covers("FC-NOTIF-02", "FC-NOTIF-03")
async def test_an_inverter_back_in_the_listing_restarts_its_offline_timer(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: FakeClock
) -> None:
    _entry, controller = await _loaded(hass, aioclient_mock)
    offline = device_payload()
    offline["online"] = False
    _serve(aioclient_mock, devices={DEVICE_ID: offline})
    await controller.poll_devices()

    clock.now += INVERTER_OFFLINE_AFTER_SECONDS + 1
    await _make_missing(controller, aioclient_mock, {DEVICE_ID: offline}, [])
    assert f"inverter_offline_{DEVICE_ID}" not in _issues(hass), (
        "one issue per inverter: missing replaces offline"
    )

    _serve(aioclient_mock, devices={DEVICE_ID: offline})
    await controller.poll_site()
    await controller.poll_devices()
    assert f"inverter_offline_{DEVICE_ID}" not in _issues(hass), (
        "its online flag was not seen while it was missing"
    )
    clock.now += INVERTER_OFFLINE_AFTER_SECONDS
    await controller.poll_devices()
    assert f"inverter_offline_{DEVICE_ID}" in _issues(hass)


# ---------------------------------------------------------------------------
# D41: a stopped controller treated as live
# ---------------------------------------------------------------------------


@pytest.mark.defect("D41")
@pytest.mark.covers("FC-AVAIL-04", "FC-DISC-05")
async def test_devices_can_be_deleted_once_the_controller_has_stopped(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry, _controller = await _loaded(hass, aioclient_mock)
    with patch.object(hass.config_entries, "async_unload_platforms", return_value=False):
        assert not await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.FAILED_UNLOAD
    assert await async_remove_config_entry_device(hass, entry, _device(hass, entry, DEVICE_ID)), (
        "nothing will poll the site again until the entry loads, as when not loaded"
    )


@pytest.mark.guard("D41")
@pytest.mark.covers("FC-DISC-05")
async def test_a_listed_inverter_cannot_be_deleted_while_running(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry, _controller = await _loaded(hass, aioclient_mock)
    assert not await async_remove_config_entry_device(hass, entry, _device(hass, entry, DEVICE_ID))


@pytest.mark.covers("FC-TIME-01")
async def test_timers_measure_monotonic_time_not_wall_time(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, clock: FakeClock, freezer: Any
) -> None:
    _entry, controller = await _loaded(hass, aioclient_mock)
    offline = device_payload()
    offline["online"] = False
    _serve(aioclient_mock, devices={DEVICE_ID: offline})
    await controller.poll_devices()
    freezer.tick(timedelta(hours=2))
    await controller.poll_devices()
    assert _issues(hass) == []
