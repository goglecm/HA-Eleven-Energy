"""Tests for the :mod:`custom_components.eleven_energy_plus.controller` module."""

from __future__ import annotations

import asyncio

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.eleven_energy_plus.const import BASE_URL, DOMAIN
from custom_components.eleven_energy_plus.controller import Controller, PostResult


@pytest.fixture
def entry() -> MockConfigEntry:
    """Return a stub config entry suitable for instantiating a Controller."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={"token": "test-token"},
        options={},
        title="Eleven Energy",
    )


@pytest.fixture
def controller(hass: HomeAssistant, entry: MockConfigEntry) -> Controller:
    """Build a fresh Controller bound to a stub ConfigEntry."""
    entry.add_to_hass(hass)
    return Controller("test-token", hass, entry)


@pytest.fixture
def instant_backoff(monkeypatch: pytest.MonkeyPatch):
    """Replace ``asyncio.wait_for`` so retry backoff is instantaneous.

    The replacement closes the passed coroutine before raising so we don't get
    a ``RuntimeWarning: coroutine was never awaited`` from the test runner.
    """

    async def _instant_timeout(coro, timeout):  # type: ignore[no-redef]
        coro.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(
        "custom_components.eleven_energy_plus.controller.asyncio.wait_for",
        _instant_timeout,
    )
    return _instant_timeout


class TestSendReliablePost:
    """The retry loop must be bounded and cancellable."""

    async def test_success_returns_post_result(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        aioclient_mock.post(
            f"{BASE_URL}devices/abc/operatingMode",
            json={"ok": True},
            status=200,
        )
        result = await controller.send_reliable_post(
            "devices/abc/operatingMode", {"workMode": "selfConsumption"}
        )
        assert isinstance(result, PostResult)
        assert result.status == 200
        assert result.body == {"ok": True}
        assert result.attempts == 1

    async def test_retry_cap(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
        instant_backoff,
    ) -> None:
        """Five attempts max - never more."""
        for _ in range(10):
            aioclient_mock.post(
                f"{BASE_URL}devices/abc/operatingMode", status=503
            )

        result = await controller.send_reliable_post(
            "devices/abc/operatingMode", {"workMode": "reset"}
        )

        assert result.status == 503
        assert result.attempts == 5

    async def test_termination_aborts_retries(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        aioclient_mock.post(
            f"{BASE_URL}devices/abc/operatingMode", status=503
        )

        # Simulate termination during the backoff: when ``wait_for`` is
        # entered we flip ``_terminated`` and return cleanly (no exception)
        # so the loop re-evaluates and exits at the top.
        async def _wait_and_terminate(coro, timeout):
            coro.close()
            controller._terminated = True

        monkeypatch.setattr(
            "custom_components.eleven_energy_plus.controller.asyncio.wait_for",
            _wait_and_terminate,
        )

        result = await controller.send_reliable_post(
            "devices/abc/operatingMode", {"workMode": "reset"}
        )

        # We made one attempt, then the wake-event handler tripped
        # ``_terminated`` so no second attempt is made.
        assert result.attempts == 1


class TestSetWorkMode:
    """The structured response surface must be stable for automations."""

    async def test_unknown_service_returns_error(
        self, controller: Controller
    ) -> None:
        result = await controller.set_work_mode("not_a_service", {})
        assert result["success"] is False
        assert result["error"] == "unknown service not_a_service"
        assert result["status"] is None
        assert result["device_id"] is None

    async def test_no_device_returns_error(
        self, controller: Controller
    ) -> None:
        result = await controller.set_work_mode(
            "set_work_mode_self_consumption", {}
        )
        assert result["success"] is False
        assert result["error"] == "no Eleven Energy Plus device available"

    async def test_missing_required_field_returns_error(
        self, controller: Controller
    ) -> None:
        # Inject a fake device so we get past the device check.
        controller.devices["abc"] = _make_fake_inverter("abc")
        result = await controller.set_work_mode(
            "set_work_mode_force_charge", {}
        )
        assert result["success"] is False
        assert "missing required field" in result["error"]
        assert "target_power" in result["error"]
        assert "target_percent" in result["error"]

    async def test_success_returns_full_response(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        controller.devices["abc"] = _make_fake_inverter("abc")
        aioclient_mock.post(
            f"{BASE_URL}devices/abc/operatingMode",
            status=200,
            json={"accepted": True},
        )
        result = await controller.set_work_mode(
            "set_work_mode_force_charge",
            {"target_power": 3.5, "target_percent": 80},
        )
        assert result["success"] is True
        assert result["status"] == 200
        assert result["device_id"] == "abc"
        assert result["work_mode"] == "forceCharge"
        assert result["params"] == {"target_power": 3.5, "target_percent": 80}
        assert result["attempts"] == 1
        assert result["error"] is None

    async def test_clamps_out_of_range_inputs(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        controller.devices["abc"] = _make_fake_inverter("abc")
        aioclient_mock.post(
            f"{BASE_URL}devices/abc/operatingMode", status=200
        )
        result = await controller.set_work_mode(
            "set_work_mode_self_consumption", {"percent_to_battery": 250}
        )
        assert result["success"] is True
        assert result["params"]["percent_to_battery"] == 100.0

    async def test_grid_export_carries_extra_flags(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        controller.devices["abc"] = _make_fake_inverter("abc")
        aioclient_mock.post(
            f"{BASE_URL}devices/abc/operatingMode", status=200
        )
        result = await controller.set_work_mode(
            "set_work_mode_grid_export",
            {
                "target_power": 5,
                "target_percent": 20,
                "include_excess_solar": "true",
                "overdrive": False,
            },
        )
        assert result["success"] is True
        assert result["params"]["include_excess_solar"] is True
        assert result["params"]["overdrive"] is False
        # Inspect the actual POST that landed on the mock.
        last_post = aioclient_mock.mock_calls[-1]
        # ``last_post`` is (method, url, json_body, headers).
        body = last_post[2]
        assert body["workMode"] == "gridExport"
        assert body["targetSoc"] == 20.0
        assert body["rate"] == 5.0
        assert body["addAverageExcess"] is True
        assert body["overdrive"] is False

    async def test_api_failure_returns_error(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
        instant_backoff,
    ) -> None:
        controller.devices["abc"] = _make_fake_inverter("abc")
        # All five attempts return 500.
        for _ in range(10):
            aioclient_mock.post(
                f"{BASE_URL}devices/abc/operatingMode", status=500
            )
        result = await controller.set_work_mode(
            "set_work_mode_self_consumption", {}
        )
        assert result["success"] is False
        assert result["status"] == 500
        assert result["attempts"] == 5
        assert "API returned status 500" in result["error"]


class TestPollSite:
    """Site polling drives device discovery."""

    async def test_returns_false_on_5xx(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", status=503)
        assert await controller.poll_site() is False
        assert controller.devices == {}

    async def test_discovers_hybrid_inverter(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        aioclient_mock.get(
            f"{BASE_URL}site",
            json={
                "devices": [
                    {
                        "deviceId": "abc",
                        "type": "hybridinverter",
                        "name": "Inverter A",
                        "serialNumber": "SN-A",
                    },
                ]
            },
        )
        assert await controller.poll_site() is True
        assert "abc" in controller.devices

    async def test_null_name_and_serial_are_coerced(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        aioclient_mock.get(
            f"{BASE_URL}site",
            json={
                "devices": [
                    {
                        "deviceId": "x",
                        "type": "hybridinverter",
                        "name": None,
                        "serialNumber": None,
                    }
                ]
            },
        )
        assert await controller.poll_site() is True
        assert "x" in controller.devices
        info = controller.devices["x"].device_info
        # When the API returns ``null`` for ``name``, the controller falls
        # back to the generic ``"Inverter"`` placeholder so the device card
        # reads "Eleven Energy Plus Inverter" rather than the brand-doubled
        # "Eleven Energy Plus Eleven Energy".
        assert info["name"] == "Eleven Energy Plus Inverter"
        assert info["serial_number"] == ""

    async def test_non_hybrid_devices_skipped(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        aioclient_mock.get(
            f"{BASE_URL}site",
            json={
                "devices": [
                    {"deviceId": "x", "type": "thermostat"},
                    {"deviceId": "y", "type": "hybridinverter"},
                ]
            },
        )
        assert await controller.poll_site() is True
        assert set(controller.devices) == {"y"}


class TestInitialise:
    """The initial poll governs whether HA marks the entry ready or retries."""

    async def test_raises_not_ready_on_initial_failure(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", status=500)
        with pytest.raises(ConfigEntryNotReady):
            await controller.initialise()

    async def test_raises_not_ready_when_no_devices(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", json={"devices": []})
        with pytest.raises(ConfigEntryNotReady):
            await controller.initialise()


class TestSetWorkModeAllVariants:
    """Smoke-test the remaining work-mode services so every match arm is hit."""

    async def test_pv_export(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        controller.devices["abc"] = _make_fake_inverter("abc")
        aioclient_mock.post(
            f"{BASE_URL}devices/abc/operatingMode", status=200
        )
        result = await controller.set_work_mode("set_work_mode_pv_export", {})
        assert result["success"] is True
        assert result["work_mode"] == "pvExportPriority"

    async def test_idle_battery_with_flags(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        controller.devices["abc"] = _make_fake_inverter("abc")
        aioclient_mock.post(
            f"{BASE_URL}devices/abc/operatingMode", status=200
        )
        result = await controller.set_work_mode(
            "set_work_mode_idle_battery",
            {"allow_charging": "true", "allow_discharging": False},
        )
        assert result["success"] is True
        assert result["work_mode"] == "idleBattery"
        assert result["params"] == {
            "allow_charging": True,
            "allow_discharging": False,
        }
        body = aioclient_mock.mock_calls[-1][2]
        assert body["allowCharge"] is True
        assert body["allowDischarge"] is False

    async def test_target_soc_with_optional_kwargs(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        controller.devices["abc"] = _make_fake_inverter("abc")
        aioclient_mock.post(
            f"{BASE_URL}devices/abc/operatingMode", status=200
        )
        result = await controller.set_work_mode(
            "set_work_mode_target_soc",
            {
                "target_soc": 60,
                "target_minutes": 90,
                "max_charge_power": 4,
                "max_discharge_power": 4,
            },
        )
        assert result["success"] is True
        assert result["work_mode"] == "targetSoc"
        body = aioclient_mock.mock_calls[-1][2]
        assert body["targetSoc"] == 60.0
        assert body["targetMinutes"] == 90
        assert body["maxChargeRate"] == 4.0
        assert body["maxDischargeRate"] == 4.0

    async def test_reset(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        controller.devices["abc"] = _make_fake_inverter("abc")
        aioclient_mock.post(
            f"{BASE_URL}devices/abc/operatingMode", status=200
        )
        result = await controller.set_work_mode("set_work_mode_reset", {})
        assert result["success"] is True
        assert result["work_mode"] == "reset"


class TestPollDevices:
    """The per-device poll loop must isolate failures between devices."""

    async def test_polls_device_and_updates_entity(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        from custom_components.eleven_energy_plus.hybrid_inverter import HybridInverter

        controller.devices["abc"] = HybridInverter(
            controller.hass, controller, "abc", "Inverter", "SN"
        )
        aioclient_mock.get(
            f"{BASE_URL}devices/abc",
            json={"pv": {"power": 4.2}, "battery": {"stateOfCharge": 65}},
        )

        await controller.poll_devices()

        inverter = controller.devices["abc"]
        assert inverter.sensor_entities["pv.power"].native_value == 4.2
        assert (
            inverter.sensor_entities["battery.stateOfCharge"].native_value == 65
        )

    async def test_one_device_failure_does_not_break_others(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        from custom_components.eleven_energy_plus.hybrid_inverter import HybridInverter

        controller.devices["bad"] = HybridInverter(
            controller.hass, controller, "bad", "Bad", "SN-1"
        )
        controller.devices["good"] = HybridInverter(
            controller.hass, controller, "good", "Good", "SN-2"
        )

        aioclient_mock.get(f"{BASE_URL}devices/bad", status=500)
        aioclient_mock.get(
            f"{BASE_URL}devices/good", json={"pv": {"power": 7.5}}
        )

        await controller.poll_devices()

        assert (
            controller.devices["good"].sensor_entities["pv.power"].native_value
            == 7.5
        )


class TestTerminate:
    """Termination must reliably stop the background polling task."""

    async def test_terminate_without_task_is_noop(
        self, controller: Controller
    ) -> None:
        await controller.terminate()
        assert controller.poller_task is None
        assert controller._terminated is True

    async def test_terminate_cancels_running_task(
        self, controller: Controller
    ) -> None:
        async def long_running() -> None:
            await asyncio.sleep(60)

        task = asyncio.create_task(long_running())
        controller.poller_task = task

        await controller.terminate()

        assert controller.poller_task is None
        assert task.cancelled()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_inverter(device_id: str):
    """Return a minimal stand-in for HybridInverter for set_work_mode tests."""

    class _Fake:
        type = "hybridinverter"

        def __init__(self) -> None:
            self.device_id = device_id

    return _Fake()
