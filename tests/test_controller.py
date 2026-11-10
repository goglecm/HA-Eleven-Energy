"""Tests for the :mod:`custom_components.eleven_energy_plus.controller` module."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    OAuth2TokenRequestReauthError,
    OAuth2TokenRequestTransientError,
)
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.eleven_energy_plus.const import (
    BASE_URL,
    CONF_DEVICE_LABEL,
    CONF_POLL_INTERVAL,
    DOMAIN,
    UNAVAILABLE_AFTER_FAILED_POLLS,
)
from custom_components.eleven_energy_plus.controller import (
    Controller,
    ElevenEnergyApiError,
    ElevenEnergyAuthError,
    ElevenEnergyRateLimitedError,
    PostResult,
)
from custom_components.eleven_energy_plus.hybrid_inverter import HybridInverter

from .common import (
    MODEL_SINGLE_PHASE,
    MODEL_THREE_PHASE_DUAL,
    device_field,
    device_payload,
    site_payload,
    site_summary,
)

POST_URL = f"{BASE_URL}devices/abc/operatingMode"


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """Return a stub config entry suitable for instantiating a Controller."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"token": "test-token"},
        options={},
        title="Eleven Energy Plus",
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def controller(hass: HomeAssistant, entry: MockConfigEntry) -> Controller:
    """Build a fresh Controller bound to a stub ConfigEntry."""
    return Controller("test-token", hass, entry)


def _fake_inverter(device_id: str = "abc") -> Any:
    """Return a minimal stand-in for HybridInverter for set_work_mode tests."""

    class _Fake:
        type = "hybridinverter"
        missing = False

        def __init__(self) -> None:
            self.device_id = device_id
            self.display_name = f"Eleven Energy Plus {device_id}"
            self.unavailable_marked = False

        def mark_unavailable(self) -> None:
            self.unavailable_marked = True

    return _Fake()


def _register_device(
    hass: HomeAssistant, entry: MockConfigEntry, device_id: str
) -> dr.DeviceEntry:
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, device_id)},
        name=f"Inverter {device_id}",
    )


def _reauth_flows(hass: HomeAssistant) -> list[dict[str, Any]]:
    return [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"]["source"] == SOURCE_REAUTH
    ]


class TestSendReliablePost:
    """The retry loop must be bounded, selective and cancellable."""

    @pytest.mark.covers("FC-CMD-01")
    async def test_success_returns_post_result(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        aioclient_mock.post(POST_URL, json={"ok": True}, status=200)
        result = await controller.send_reliable_post(
            "devices/abc/operatingMode", {"workMode": "selfConsumption"}
        )
        assert isinstance(result, PostResult)
        assert result.status == 200
        assert result.body == {"ok": True}
        assert result.attempts == 1
        assert result.error is None

    @pytest.mark.covers("FC-CMD-01")
    async def test_retry_cap_on_5xx(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
        instant_backoff: list[float],
    ) -> None:
        aioclient_mock.post(POST_URL, status=503)
        result = await controller.send_reliable_post(
            "devices/abc/operatingMode", {"workMode": "reset"}
        )
        assert result.status == 503
        assert result.attempts == 5
        assert instant_backoff == [1.0, 2.0, 4.0, 8.0]
        assert result.error == "API returned status 503"

    @pytest.mark.parametrize("status", [400, 404, 409, 422])
    @pytest.mark.covers("FC-CMD-01")
    async def test_no_retry_on_client_errors(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
        instant_backoff: list[float],
        status: int,
    ) -> None:
        aioclient_mock.post(POST_URL, status=status)
        result = await controller.send_reliable_post("devices/abc/operatingMode", {})
        assert result.attempts == 1
        assert result.status == status
        assert instant_backoff == []

    @pytest.mark.parametrize("status", [408, 429, 500, 502])
    @pytest.mark.covers("FC-CMD-01")
    async def test_retries_retryable_statuses(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
        instant_backoff: list[float],
        status: int,
    ) -> None:
        aioclient_mock.post(POST_URL, status=status)
        result = await controller.send_reliable_post("devices/abc/operatingMode", {})
        assert result.attempts == 5

    @pytest.mark.parametrize("status", [401, 403])
    @pytest.mark.covers("FC-AUTH-06")
    async def test_auth_failure_stops_and_starts_reauth(
        self,
        hass: HomeAssistant,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
        instant_backoff: list[float],
        status: int,
    ) -> None:
        controller.devices["abc"] = fake = _fake_inverter()
        aioclient_mock.post(POST_URL, status=status)
        result = await controller.send_reliable_post("devices/abc/operatingMode", {})
        await hass.async_block_till_done()
        assert result.attempts == 1
        assert result.status == status
        assert "rejected the credential" in result.error
        assert controller.auth_failed
        assert fake.unavailable_marked
        assert len(_reauth_flows(hass)) == 1

    @pytest.mark.covers("FC-CMD-01")
    async def test_network_error_is_retried_then_succeeds(
        self, controller: Controller, instant_backoff: list[float], monkeypatch
    ) -> None:
        calls: list[int] = []

        async def fake_request(method, suffix, *, json=None):
            calls.append(1)
            if len(calls) < 3:
                raise ElevenEnergyApiError("connection reset")
            return 200, {"ok": True}, None

        monkeypatch.setattr(controller, "_request", fake_request)
        result = await controller.send_reliable_post("devices/abc/operatingMode", {})
        assert result.status == 200
        assert result.attempts == 3

    @pytest.mark.covers("FC-CMD-02")
    async def test_retry_after_is_honoured_and_capped(
        self, controller: Controller, instant_backoff: list[float], monkeypatch
    ) -> None:
        responses = iter([(429, None, "7"), (429, None, "9999"), (200, {}, None)])

        async def fake_request(method, suffix, *, json=None):
            return next(responses)

        monkeypatch.setattr(controller, "_request", fake_request)
        result = await controller.send_reliable_post("devices/abc/operatingMode", {})
        assert result.status == 200
        assert instant_backoff == [7.0, 30.0]

    @pytest.mark.covers("FC-CMD-05")
    async def test_shutdown_aborts_retries(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker, monkeypatch
    ) -> None:
        aioclient_mock.post(POST_URL, status=503)

        async def wait_then_shut_down(delay: float) -> None:
            controller._shutdown.set()

        monkeypatch.setattr(controller, "_wait_backoff", wait_then_shut_down)
        result = await controller.send_reliable_post("devices/abc/operatingMode", {})
        assert result.attempts == 1
        assert "shutting down" in result.error

    @pytest.mark.covers("FC-CMD-03")
    async def test_superseded_aborts_before_next_attempt(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker, instant_backoff
    ) -> None:
        aioclient_mock.post(POST_URL, status=503)
        flag = {"superseded": False}

        def superseded() -> bool:
            was = flag["superseded"]
            flag["superseded"] = True
            return was

        result = await controller.send_reliable_post(
            "devices/abc/operatingMode", {}, superseded=superseded
        )
        assert result.attempts == 1
        assert "superseded" in result.error
        assert result.kind == "superseded"

    @pytest.mark.covers("FC-CMD-05", "FC-POLL-03")
    async def test_wait_backoff_wakes_on_shutdown_only(
        self, controller: Controller
    ) -> None:
        """The poll-now event must never shorten a command's backoff."""
        controller._poll_now.set()
        loop = asyncio.get_running_loop()
        start = loop.time()
        await controller._wait_backoff(0.05)
        assert loop.time() - start >= 0.04

        controller._shutdown.set()
        start = loop.time()
        await controller._wait_backoff(5)
        assert loop.time() - start < 1


class TestSetWorkMode:
    """The structured response surface must be stable for automations."""

    @pytest.mark.covers("FC-SVC-07")
    async def test_unknown_service_returns_error(self, controller: Controller) -> None:
        result = await controller.set_work_mode("not_a_service", {})
        assert result["success"] is False
        assert result["error"] == "unknown service not_a_service"
        assert result["status"] is None
        assert result["device_id"] is None

    async def test_no_device_returns_error(self, controller: Controller) -> None:
        result = await controller.set_work_mode("set_work_mode_self_consumption", {})
        assert result["success"] is False
        assert result["error"] == "no Eleven Energy Plus device available"

    @pytest.mark.covers("FC-SVC-04")
    async def test_missing_required_field_returns_error(
        self, controller: Controller
    ) -> None:
        controller.devices["abc"] = _fake_inverter()
        result = await controller.set_work_mode("set_work_mode_force_charge", {})
        assert result["success"] is False
        assert "missing required field" in result["error"]
        assert "target_power" in result["error"]
        assert "target_percent" in result["error"]

    @pytest.mark.parametrize("bad", ["nan", "inf", True, "fast"])
    async def test_unusable_value_is_refused_not_sent(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
        bad: Any,
    ) -> None:
        """Regression: "nan" kW used to be sent to the inverter as 50 kW."""
        controller.devices["abc"] = _fake_inverter()
        aioclient_mock.post(POST_URL, status=200)
        result = await controller.set_work_mode(
            "set_work_mode_force_charge", {"target_power": bad, "target_percent": 80}
        )
        assert result["success"] is False
        assert result["error"] == "invalid value for field(s): target_power"
        assert aioclient_mock.call_count == 0

    async def test_inf_minutes_never_raises(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        controller.devices["abc"] = _fake_inverter()
        result = await controller.set_work_mode(
            "set_work_mode_target_soc", {"target_soc": 50, "target_minutes": "inf"}
        )
        assert result["success"] is False
        assert "target_minutes" in result["error"]

    @pytest.mark.covers("FC-SVC-06", "FC-CMD-06")
    async def test_success_returns_full_response(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        controller.devices["abc"] = _fake_inverter()
        aioclient_mock.post(POST_URL, status=200, json={"accepted": True})
        result = await controller.set_work_mode(
            "set_work_mode_force_charge",
            {"target_power": 3.5, "target_percent": 80},
        )
        assert result == {
            "success": True,
            "status": 200,
            "device_id": "abc",
            "work_mode": "forceCharge",
            "params": {"target_power": 3.5, "target_percent": 80.0},
            "attempts": 1,
            "error": None,
            "error_kind": None,
        }
        assert controller._poll_now.is_set()
        assert not controller._shutdown.is_set()

    @pytest.mark.covers("FC-SVC-01")
    @pytest.mark.parametrize(
        ("service", "data", "field"),
        [
            ("set_work_mode_self_consumption", {"percent_to_battery": 250}, "percent_to_battery"),
            ("set_work_mode_force_charge", {"target_power": 999, "target_percent": 50}, "target_power"),
            ("set_work_mode_target_soc", {"target_soc": 50, "target_minutes": 60.5}, "target_minutes"),
        ],
    )
    async def test_refuses_out_of_range_inputs_rather_than_clamping(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
        service: str,
        data: dict[str, Any],
        field: str,
    ) -> None:
        """Direct callers bypass the schema; the controller refuses, never clamps."""
        controller.devices["abc"] = _fake_inverter()
        aioclient_mock.post(POST_URL, status=200)
        result = await controller.set_work_mode(service, data)
        assert result["success"] is False
        assert result["error_kind"] == "invalid_request"
        assert field in result["error"]
        assert aioclient_mock.call_count == 0

    async def test_grid_export_carries_extra_flags(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        controller.devices["abc"] = _fake_inverter()
        aioclient_mock.post(POST_URL, status=200)
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
        body = aioclient_mock.mock_calls[-1][2]
        assert body == {
            "workMode": "gridExport",
            "targetSoc": 20.0,
            "rate": 5.0,
            "addAverageExcess": True,
            "overdrive": False,
        }

    async def test_api_failure_returns_error(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
        instant_backoff,
    ) -> None:
        controller.devices["abc"] = _fake_inverter()
        aioclient_mock.post(POST_URL, status=500)
        result = await controller.set_work_mode("set_work_mode_self_consumption", {})
        assert result["success"] is False
        assert result["status"] == 500
        assert result["attempts"] == 5
        assert "API returned status 500" in result["error"]

    async def test_refused_while_credential_rejected(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        controller.devices["abc"] = _fake_inverter()
        controller._auth_failed = True
        result = await controller.set_work_mode("set_work_mode_reset", {})
        assert result["success"] is False
        assert "reauthenticate" in result["error"]
        assert aioclient_mock.call_count == 0


class TestWorkModeOrdering:
    """Commands to one inverter run in order, and the newest one wins."""

    @pytest.mark.covers("FC-CMD-03")
    async def test_newer_command_supersedes_one_in_backoff(
        self, controller: Controller, monkeypatch
    ) -> None:
        controller.devices["abc"] = _fake_inverter()
        sent: list[str] = []
        gate = asyncio.Event()

        async def fake_request(method, suffix, *, json=None):
            sent.append(json["workMode"])
            if len(sent) == 1:
                return 503, None, None
            return 200, {}, None

        async def fake_backoff(delay: float) -> None:
            await gate.wait()

        monkeypatch.setattr(controller, "_request", fake_request)
        monkeypatch.setattr(controller, "_wait_backoff", fake_backoff)

        first = asyncio.create_task(
            controller.set_work_mode(
                "set_work_mode_force_charge", {"target_power": 3, "target_percent": 90}
            )
        )
        for _ in range(10):
            await asyncio.sleep(0)
        assert sent == ["forceCharge"]

        second = asyncio.create_task(controller.set_work_mode("set_work_mode_reset", {}))
        for _ in range(10):
            await asyncio.sleep(0)
        gate.set()

        first_result, second_result = await asyncio.gather(first, second)
        assert first_result["success"] is False
        assert "superseded" in first_result["error"]
        assert second_result["success"] is True
        # The older command was never retried after the newer one was issued.
        assert sent == ["forceCharge", "reset"]

    @pytest.mark.covers("FC-CMD-03")
    async def test_queued_intermediate_command_is_skipped(
        self, controller: Controller, monkeypatch
    ) -> None:
        controller.devices["abc"] = _fake_inverter()
        sent: list[str] = []
        release = asyncio.Event()

        async def fake_request(method, suffix, *, json=None):
            sent.append(json["workMode"])
            if json["workMode"] == "forceCharge":
                await release.wait()
            return 200, {}, None

        monkeypatch.setattr(controller, "_request", fake_request)

        a = asyncio.create_task(
            controller.set_work_mode(
                "set_work_mode_force_charge", {"target_power": 3, "target_percent": 90}
            )
        )
        for _ in range(5):
            await asyncio.sleep(0)
        b = asyncio.create_task(controller.set_work_mode("set_work_mode_pv_export", {}))
        c = asyncio.create_task(controller.set_work_mode("set_work_mode_reset", {}))
        for _ in range(5):
            await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(a, b, c)

        assert results[0]["success"] is True
        assert results[1]["success"] is False and "superseded" in results[1]["error"]
        assert results[2]["success"] is True
        assert sent == ["forceCharge", "reset"]

    @pytest.mark.covers("FC-CMD-04")
    async def test_different_inverters_do_not_block_each_other(
        self, hass: HomeAssistant, entry: MockConfigEntry, controller: Controller, monkeypatch
    ) -> None:
        controller.devices["a"] = _fake_inverter("a")
        controller.devices["b"] = _fake_inverter("b")
        dev_a = _register_device(hass, entry, "a")
        dev_b = _register_device(hass, entry, "b")
        release = asyncio.Event()

        async def fake_request(method, suffix, *, json=None):
            if suffix.startswith("devices/a/"):
                await release.wait()
            return 200, {}, None

        monkeypatch.setattr(controller, "_request", fake_request)
        slow = asyncio.create_task(
            controller.set_work_mode("set_work_mode_reset", {"device_id": dev_a.id})
        )
        for _ in range(5):
            await asyncio.sleep(0)  # the slow command now holds inverter a's lock
        fast = await asyncio.wait_for(
            controller.set_work_mode("set_work_mode_reset", {"device_id": dev_b.id}), 1
        )
        assert fast["success"] is True
        release.set()
        assert (await slow)["success"] is True


class TestTargetResolution:
    """Explicit targets must match exactly one inverter; no guessing."""

    @pytest.mark.covers("FC-SVC-09")
    async def test_device_id_resolves(
        self, hass: HomeAssistant, entry: MockConfigEntry, controller: Controller
    ) -> None:
        controller.devices["a"] = _fake_inverter("a")
        controller.devices["b"] = _fake_inverter("b")
        _register_device(hass, entry, "a")
        dev_b = _register_device(hass, entry, "b")
        assert controller._resolve_target_device({"device_id": dev_b.id}) == ("b", None)
        assert controller._resolve_target_device({"device_id": [dev_b.id]}) == ("b", None)

    @pytest.mark.covers("FC-SVC-09")
    async def test_entity_id_resolves_to_its_device(
        self, hass: HomeAssistant, entry: MockConfigEntry, controller: Controller
    ) -> None:
        controller.devices["a"] = _fake_inverter("a")
        controller.devices["b"] = _fake_inverter("b")
        dev = _register_device(hass, entry, "b")
        er.async_get(hass).async_get_or_create(
            "sensor", DOMAIN, "b_pv_power", device_id=dev.id, config_entry=entry
        )
        entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "b_pv_power")
        assert controller._resolve_target_device({"entity_id": entity_id}) == ("b", None)

    @pytest.mark.covers("FC-SVC-09")
    async def test_area_resolves(
        self, hass: HomeAssistant, entry: MockConfigEntry, controller: Controller
    ) -> None:
        controller.devices["a"] = _fake_inverter("a")
        controller.devices["b"] = _fake_inverter("b")
        area = ar.async_get(hass).async_create("Garage")
        dev = _register_device(hass, entry, "a")
        _register_device(hass, entry, "b")
        dr.async_get(hass).async_update_device(dev.id, area_id=area.id)
        assert controller._resolve_target_device({"area_id": area.id}) == ("a", None)

    @pytest.mark.covers("FC-SVC-10")
    async def test_unmatched_target_is_refused(
        self, hass: HomeAssistant, entry: MockConfigEntry, controller: Controller
    ) -> None:
        controller.devices["a"] = _fake_inverter("a")
        other = MockConfigEntry(domain="other")
        other.add_to_hass(hass)
        foreign = dr.async_get(hass).async_get_or_create(
            config_entry_id=other.entry_id, identifiers={("other", "x")}
        )
        for data in ({"device_id": foreign.id}, {"device_id": "missing"}, {"entity_id": "sensor.nope"}):
            device_id, error = controller._resolve_target_device(data)
            assert device_id is None
            assert error == "the target does not match any Eleven Energy Plus inverter"

    @pytest.mark.covers("FC-SVC-10")
    async def test_target_matching_two_inverters_is_refused(
        self, hass: HomeAssistant, entry: MockConfigEntry, controller: Controller
    ) -> None:
        controller.devices["a"] = _fake_inverter("a")
        controller.devices["b"] = _fake_inverter("b")
        dev_a = _register_device(hass, entry, "a")
        dev_b = _register_device(hass, entry, "b")
        device_id, error = controller._resolve_target_device(
            {"device_id": [dev_a.id, dev_b.id]}
        )
        assert device_id is None
        assert "more than one inverter" in error

    @pytest.mark.covers("FC-SVC-10")
    async def test_untargeted_call_on_multi_inverter_site_is_refused(
        self, controller: Controller
    ) -> None:
        """Regression: it used to silently control "the first" inverter."""
        controller.devices["a"] = _fake_inverter("a")
        controller.devices["b"] = _fake_inverter("b")
        device_id, error = controller._resolve_target_device({})
        assert device_id is None
        assert "choose one in the Inverter field" in error

    @pytest.mark.covers("FC-SVC-09")
    async def test_untargeted_call_on_single_inverter_site(self, controller: Controller) -> None:
        controller.devices["a"] = _fake_inverter("a")
        assert controller._resolve_target_device({}) == ("a", None)

    @pytest.mark.parametrize("key", ["floor_id", "label_id"])
    @pytest.mark.covers("FC-SVC-10")
    async def test_unsupported_target_kinds(self, controller: Controller, key: str) -> None:
        controller.devices["a"] = _fake_inverter("a")
        device_id, error = controller._resolve_target_device({key: ["x"]})
        assert device_id is None
        assert key in error


class TestPollSite:
    """Site polling drives device discovery."""

    @pytest.mark.covers("FC-DISC-06")
    async def test_returns_false_on_5xx(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", status=503)
        assert await controller.poll_site() is False
        assert controller.devices == {}

    @pytest.mark.parametrize(
        "body",
        [["not", "a", "dict"], {"devices": "nope"}, {}, {"devices": None}],
    )
    @pytest.mark.covers("FC-DISC-06")
    async def test_malformed_site_payload_is_not_a_crash(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker, body: Any
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", json=body)
        assert await controller.poll_site() is False

    @pytest.mark.covers("FC-DISC-06", "FC-DISC-01")
    async def test_malformed_device_entries_are_skipped(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        aioclient_mock.get(
            f"{BASE_URL}site",
            json={
                "devices": [
                    "string",
                    None,
                    {"type": "hybridinverter"},
                    {"deviceId": 7, "type": "hybridinverter"},
                    {"deviceId": "", "type": "hybridinverter"},
                    {"deviceId": "ok", "type": "hybridinverter"},
                ]
            },
        )
        aioclient_mock.get(f"{BASE_URL}devices/ok", status=500)
        assert await controller.poll_site() is True
        assert set(controller.devices) == {"ok"}

    @pytest.mark.covers("FC-DISC-01", "FC-DISC-02")
    async def test_discovers_hybrid_inverter_and_reads_it(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", json=site_payload(site_summary()))
        aioclient_mock.get(
            f"{BASE_URL}devices/abc",
            json=device_payload(
                model=MODEL_SINGLE_PHASE,
                batteries=[{"index": 1, "stateOfCharge": 83, "power": -0.2, "moduleCount": 4}],
            ),
        )
        assert await controller.poll_site() is True
        inverter = controller.devices["abc"]
        # The discovery reading is applied straight away.
        assert inverter.sensor_entities["pv.power"].native_value == 1.5
        assert inverter.battery_indices == [1]
        assert inverter.mppt_count == 2
        assert controller.last_device_payloads["abc"]["pv"]["power"] == 1.5

    @pytest.mark.covers("FC-DISC-07")
    async def test_discovery_survives_failed_device_read(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", json=site_payload(site_summary()))
        aioclient_mock.get(f"{BASE_URL}devices/abc", exc=aiohttp.ClientError("down"))
        assert await controller.poll_site() is True
        assert "abc" in controller.devices
        assert controller.devices["abc"].discovery_state is None

    @pytest.mark.covers("FC-DISC-03")
    async def test_model_block_name_is_the_device_label(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        """Regression for the "North Sea 6" label on a Mediterranean Sea 12."""
        aioclient_mock.get(
            f"{BASE_URL}site",
            json=site_payload(site_summary(name="North Sea 6 (6kW)", model=MODEL_SINGLE_PHASE)),
        )
        aioclient_mock.get(f"{BASE_URL}devices/abc", json=device_payload())
        assert await controller.poll_site() is True
        info = controller.devices["abc"].device_info
        assert device_field(info, "name") == "Eleven Energy Plus Mediterranean Sea 12"
        assert device_field(info, "model") == "Mediterranean Sea 12"

    @pytest.mark.covers("FC-DISC-03")
    async def test_model_block_from_reading_when_listing_lacks_it(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", json=site_payload(site_summary()))
        aioclient_mock.get(
            f"{BASE_URL}devices/abc", json=device_payload(model=MODEL_THREE_PHASE_DUAL)
        )
        assert await controller.poll_site() is True
        inverter = controller.devices["abc"]
        assert inverter.device_label == "Atlantic 20"
        assert inverter.three_phase
        assert inverter.mppt_count == 3

    @pytest.mark.covers("FC-DISC-03")
    async def test_name_used_when_no_model_block(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        aioclient_mock.get(
            f"{BASE_URL}site",
            json=site_payload(site_summary(name="Authoritative Name")),
        )
        aioclient_mock.get(f"{BASE_URL}devices/abc", status=500)
        assert await controller.poll_site() is True
        assert controller.devices["abc"].device_label == "Authoritative Name"

    @pytest.mark.covers("FC-DISC-03")
    async def test_null_name_and_serial_are_coerced(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        aioclient_mock.get(
            f"{BASE_URL}site", json=site_payload(site_summary("x", name=None, serial=None))
        )
        aioclient_mock.get(f"{BASE_URL}devices/x", status=500)
        assert await controller.poll_site() is True
        info = controller.devices["x"].device_info
        assert device_field(info, "name") == "Eleven Energy Plus Inverter"
        assert device_field(info, "serial_number") == ""

    @pytest.mark.covers("FC-DISC-03")
    async def test_device_label_override_wins_over_api(
        self, hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
    ) -> None:
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={"token": "test-token"},
            options={CONF_DEVICE_LABEL: "  My Label  "},
        )
        entry.add_to_hass(hass)
        controller = Controller("test-token", hass, entry)
        aioclient_mock.get(
            f"{BASE_URL}site",
            json=site_payload(site_summary(model=MODEL_SINGLE_PHASE)),
        )
        aioclient_mock.get(f"{BASE_URL}devices/abc", json=device_payload())
        assert await controller.poll_site() is True
        info = controller.devices["abc"].device_info
        assert device_field(info, "name") == "Eleven Energy Plus My Label"
        assert device_field(info, "model") == "My Label"

    @pytest.mark.covers("FC-DISC-01")
    async def test_non_hybrid_devices_skipped(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
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
        aioclient_mock.get(f"{BASE_URL}devices/y", json=device_payload("y"))
        assert await controller.poll_site() is True
        assert set(controller.devices) == {"y"}

    @pytest.mark.covers("FC-DISC-01")
    async def test_known_devices_are_not_recreated(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", json=site_payload(site_summary()))
        aioclient_mock.get(f"{BASE_URL}devices/abc", json=device_payload())
        assert await controller.poll_site() is True
        first = controller.devices["abc"]
        assert await controller.poll_site() is True
        assert controller.devices["abc"] is first

    @pytest.mark.covers("FC-DISC-04")
    async def test_new_inverter_registered_without_duplicates(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        """A runtime-discovered inverter's entities are each added exactly once."""
        added: list[Any] = []
        controller.register_platform("sensor", added.extend)
        controller.register_platform("binary_sensor", added.extend)
        aioclient_mock.get(f"{BASE_URL}site", json=site_payload(site_summary()))
        aioclient_mock.get(
            f"{BASE_URL}devices/abc", json=device_payload(extraField=1, extraFlag=True)
        )
        assert await controller.poll_site() is True
        unique_ids = [entity.unique_id for entity in added]
        assert len(unique_ids) == len(set(unique_ids))
        assert "abc_extraField" in unique_ids
        assert "abc_extraFlag" in unique_ids


class TestInitialise:
    """The initial poll governs whether HA marks the entry ready, retries or reauths."""

    @pytest.mark.parametrize("status", [500, 503, 404])
    @pytest.mark.covers("FC-LIFE-02")
    async def test_raises_not_ready_on_initial_failure(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker, status: int
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", status=status)
        with pytest.raises(ConfigEntryNotReady):
            await controller.initialise()

    @pytest.mark.covers("FC-LIFE-02")
    async def test_raises_not_ready_on_network_error(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", exc=TimeoutError())
        with pytest.raises(ConfigEntryNotReady):
            await controller.initialise()

    @pytest.mark.covers("FC-LIFE-02")
    async def test_raises_not_ready_on_malformed_payload(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", json=["unexpected"])
        with pytest.raises(ConfigEntryNotReady):
            await controller.initialise()

    @pytest.mark.covers("FC-LIFE-02")
    async def test_raises_not_ready_when_no_devices(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", json={"devices": []})
        with pytest.raises(ConfigEntryNotReady):
            await controller.initialise()

    @pytest.mark.parametrize("status", [401, 403])
    @pytest.mark.covers("FC-AUTH-05")
    async def test_raises_auth_failed_on_rejected_credential(
        self,
        hass: HomeAssistant,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
        status: int,
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", status=status)
        with pytest.raises(ConfigEntryAuthFailed):
            await controller.initialise()
        await hass.async_block_till_done()
        # Home Assistant starts reauth itself from ConfigEntryAuthFailed during
        # setup; the controller must not start a second flow.
        assert _reauth_flows(hass) == []

    @pytest.mark.covers("FC-AUTH-05")
    async def test_auth_failure_reading_device_during_discovery(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", json=site_payload(site_summary()))
        aioclient_mock.get(f"{BASE_URL}devices/abc", status=401)
        with pytest.raises(ConfigEntryAuthFailed):
            await controller.initialise()


class TestPollDevices:
    """The per-device poll loop isolates failures and drives availability."""

    @pytest.mark.covers("FC-VAL-01")
    async def test_polls_device_and_updates_entity(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
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
        assert inverter.sensor_entities["battery.stateOfCharge"].native_value == 65

    @pytest.mark.covers("FC-AVAIL-01")
    async def test_one_device_failure_does_not_break_others(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        controller.devices["bad"] = HybridInverter(
            controller.hass, controller, "bad", "Bad", "SN-1"
        )
        controller.devices["good"] = HybridInverter(
            controller.hass, controller, "good", "Good", "SN-2"
        )
        aioclient_mock.get(f"{BASE_URL}devices/bad", status=500)
        aioclient_mock.get(f"{BASE_URL}devices/good", json={"pv": {"power": 7.5}})
        await controller.poll_devices()
        assert controller.devices["good"].sensor_entities["pv.power"].native_value == 7.5

    @pytest.mark.covers("FC-AVAIL-01")
    async def test_unavailable_after_repeated_failures_then_recovers(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        inverter = HybridInverter(controller.hass, controller, "abc", "Inverter", "SN")
        controller.devices["abc"] = inverter
        aioclient_mock.get(f"{BASE_URL}devices/abc", status=502)
        for _ in range(UNAVAILABLE_AFTER_FAILED_POLLS):
            assert inverter.available
            await controller.poll_devices()
        assert not inverter.available
        assert not inverter.sensor_entities["pv.power"].available

        aioclient_mock.clear_requests()
        aioclient_mock.get(f"{BASE_URL}devices/abc", json={"pv": {"power": 1.0}})
        await controller.poll_devices()
        assert inverter.available
        assert inverter.sensor_entities["pv.power"].native_value == 1.0

    @pytest.mark.parametrize("body", [["list"], "text", None])
    @pytest.mark.covers("FC-AVAIL-02")
    async def test_non_dict_device_payload_counts_as_failure(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker, body: Any
    ) -> None:
        inverter = HybridInverter(controller.hass, controller, "abc", "Inverter", "SN")
        controller.devices["abc"] = inverter
        aioclient_mock.get(f"{BASE_URL}devices/abc", json=body)
        for _ in range(UNAVAILABLE_AFTER_FAILED_POLLS):
            await controller.poll_devices()
        assert not inverter.available

    @pytest.mark.covers("FC-AUTH-06")
    async def test_rejected_credential_during_poll(
        self,
        hass: HomeAssistant,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
    ) -> None:
        first = HybridInverter(controller.hass, controller, "a", "A", "SN")
        second = HybridInverter(controller.hass, controller, "b", "B", "SN")
        controller.devices.update({"a": first, "b": second})
        aioclient_mock.get(f"{BASE_URL}devices/a", status=401)
        aioclient_mock.get(f"{BASE_URL}devices/b", json={"pv": {"power": 1}})
        await controller.poll_devices()
        await hass.async_block_till_done()
        assert controller.auth_failed
        assert not first.available and not second.available
        # The cycle stops: the second device is not even asked.
        assert aioclient_mock.call_count == 1
        assert len(_reauth_flows(hass)) == 1

        # A second rejection does not start a second reauth flow.
        await controller.poll_devices()
        await hass.async_block_till_done()
        assert len(_reauth_flows(hass)) == 1


class TestOAuthCredential:
    """An account sign-in builds headers from a freshly validated token."""

    @pytest.mark.covers("FC-AUTH-02", "FC-AUTH-04", "FC-TIME-05")
    async def test_headers_use_refreshed_access_token(
        self, hass: HomeAssistant, entry: MockConfigEntry
    ) -> None:
        session = MagicMock()
        session.async_ensure_token_valid = AsyncMock()
        session.token = {"access_token": "fresh-access"}
        controller = Controller(None, hass, entry, session=session)
        headers = await controller._auth_headers()
        assert headers["Authorization"] == "Bearer fresh-access"
        session.async_ensure_token_valid.assert_awaited_once()
        assert controller.uses_account_sign_in

    @pytest.mark.covers("FC-AUTH-07")
    async def test_refused_refresh_is_an_auth_failure(
        self, hass: HomeAssistant, entry: MockConfigEntry
    ) -> None:
        session = MagicMock()
        session.async_ensure_token_valid = AsyncMock(
            side_effect=OAuth2TokenRequestReauthError(
                request_info=MagicMock(), history=(), status=400, domain=DOMAIN
            )
        )
        controller = Controller(None, hass, entry, session=session)
        with pytest.raises(ElevenEnergyAuthError):
            await controller._auth_headers()
        assert controller.auth_failed

    @pytest.mark.covers("FC-AUTH-04")
    async def test_transient_refresh_failure_is_an_api_error(
        self, hass: HomeAssistant, entry: MockConfigEntry
    ) -> None:
        session = MagicMock()
        session.async_ensure_token_valid = AsyncMock(
            side_effect=OAuth2TokenRequestTransientError(
                request_info=MagicMock(), history=(), status=503, domain=DOMAIN
            )
        )
        controller = Controller(None, hass, entry, session=session)
        with pytest.raises(ElevenEnergyApiError):
            await controller._auth_headers()
        assert not controller.auth_failed

    @pytest.mark.covers("FC-AUTH-09")
    async def test_missing_credential_is_an_auth_failure(
        self, hass: HomeAssistant, entry: MockConfigEntry
    ) -> None:
        controller = Controller("", hass, entry)
        with pytest.raises(ElevenEnergyAuthError):
            await controller._auth_headers()


class TestEntryUpdates:
    """Only option changes matter to the update listener."""

    async def test_unchanged_options(self, hass: HomeAssistant, entry, controller) -> None:
        hass.config_entries.async_update_entry(entry, data={"token": "other"})
        assert controller.async_entry_updated() is False

    @pytest.mark.covers("FC-OPT-02")
    async def test_interval_change_notifies_without_reload(
        self, hass: HomeAssistant, entry: MockConfigEntry, controller: Controller
    ) -> None:
        pings: list[bool] = []
        controller.add_option_listener(lambda: pings.append(True))
        hass.config_entries.async_update_entry(entry, options={CONF_POLL_INTERVAL: 45})
        assert controller.async_entry_updated() is False
        assert pings == [True]
        assert controller.poll_interval == 45
        assert controller._poll_now.is_set()

    @pytest.mark.covers("FC-OPT-03")
    async def test_label_change_needs_reload(
        self, hass: HomeAssistant, entry: MockConfigEntry, controller: Controller
    ) -> None:
        hass.config_entries.async_update_entry(entry, options={CONF_DEVICE_LABEL: "New"})
        assert controller.async_entry_updated() is True

    async def test_whitespace_only_label_change_does_not_reload(
        self, hass: HomeAssistant, entry: MockConfigEntry
    ) -> None:
        hass.config_entries.async_update_entry(entry, options={CONF_DEVICE_LABEL: "Same"})
        controller = Controller("t", hass, entry)
        hass.config_entries.async_update_entry(entry, options={CONF_DEVICE_LABEL: "  Same  "})
        assert controller.async_entry_updated() is False

    async def test_listener_exception_is_contained(self, controller: Controller) -> None:
        def boom() -> None:
            raise RuntimeError("listener bug")

        unsub = controller.add_option_listener(boom)
        controller.notify_options_changed()
        unsub()
        unsub()

    @pytest.mark.parametrize(
        ("raw", "expected"), [(None, 15), ("x", 15), (1, 5), (9999, 300), ("60", 60)]
    )
    @pytest.mark.covers("FC-OPT-06")
    async def test_poll_interval_is_clamped(
        self, hass: HomeAssistant, entry: MockConfigEntry, raw: Any, expected: int
    ) -> None:
        options = {} if raw is None else {CONF_POLL_INTERVAL: raw}
        hass.config_entries.async_update_entry(entry, options=options)
        assert Controller("t", hass, entry).poll_interval == expected

    async def test_non_string_label_is_ignored(
        self, hass: HomeAssistant, entry: MockConfigEntry
    ) -> None:
        hass.config_entries.async_update_entry(entry, options={CONF_DEVICE_LABEL: 7})
        assert Controller("t", hass, entry).device_label_override == ""


class TestTerminate:
    """Termination must reliably stop the background polling task."""

    async def test_terminate_without_task_is_noop(self, controller: Controller) -> None:
        await controller.terminate()
        assert controller.poller_task is None
        assert controller._terminated is True
        assert controller._shutdown.is_set()

    @pytest.mark.covers("FC-AVAIL-04")
    async def test_terminate_cancels_running_task(self, controller: Controller) -> None:
        async def long_running() -> None:
            await asyncio.sleep(60)

        task = asyncio.create_task(long_running())
        controller.poller_task = task
        await controller.terminate()
        assert controller.poller_task is None
        assert task.cancelled()

    async def test_poller_not_started_after_termination(
        self, controller: Controller
    ) -> None:
        await controller.terminate()
        for platform in ("sensor", "binary_sensor", "number"):
            controller.complete_platform_setup(platform, lambda entities: None)
        assert controller.poller_task is None

    @pytest.mark.covers("FC-POLL-01", "FC-LIFE-01")
    async def test_poller_runs_and_stops(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        controller.devices["abc"] = HybridInverter(
            controller.hass, controller, "abc", "Inverter", "SN"
        )
        aioclient_mock.get(f"{BASE_URL}site", json=site_payload(site_summary()))
        aioclient_mock.get(f"{BASE_URL}devices/abc", json={"pv": {"power": 2.0}})
        for platform in ("sensor", "binary_sensor", "number"):
            controller.complete_platform_setup(platform, lambda entities: None)
        assert controller.poller_task is not None
        for _ in range(20):
            await asyncio.sleep(0)
        assert controller.devices["abc"].sensor_entities["pv.power"].native_value == 2.0

        # Asking for an early poll runs another cycle (site + device).
        calls_before = aioclient_mock.call_count
        controller._poll_now.set()
        for _ in range(20):
            await asyncio.sleep(0)
        assert aioclient_mock.call_count > calls_before

        await controller.terminate()
        assert controller.poller_task is None

    async def test_a_crashed_poll_task_is_reported_on_termination(
        self, controller: Controller, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def crash() -> None:
            raise RuntimeError("poller bug")

        task = asyncio.create_task(crash())
        await asyncio.sleep(0)
        controller.poller_task = task
        await controller.terminate()
        assert "poll task raised during termination" in caplog.text

    async def test_an_initial_poll_crash_is_contained(
        self, controller: Controller, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def crash(*args: Any, **kwargs: Any) -> bool:
            controller._shutdown.set()
            raise RuntimeError("first poll bug")

        controller.poll_devices = crash  # type: ignore[method-assign]
        controller.start_poller()
        assert controller.poller_task is not None
        await asyncio.wait({controller.poller_task}, timeout=1)
        assert controller.poller_task.done() and not controller.poller_task.cancelled()
        assert "initial device poll failed" in caplog.text

    async def test_a_poll_cycle_crash_is_contained(
        self, controller: Controller, caplog: pytest.LogCaptureFixture
    ) -> None:
        calls = 0

        async def crash() -> None:
            nonlocal calls
            calls += 1
            if calls > 1:
                controller._shutdown.set()
            raise RuntimeError("cycle bug")

        controller._poll_cycle = crash  # type: ignore[method-assign]
        controller.start_poller()
        for _ in range(3):
            controller._poll_now.set()
            for _ in range(10):
                await asyncio.sleep(0)
        assert "poll cycle failed" in caplog.text
        await controller.terminate()


class TestPollCycle:
    """One poll cycle, and the places it stops early."""

    @pytest.fixture
    def inverter_abc(self, controller: Controller) -> HybridInverter:
        device = HybridInverter(controller.hass, controller, "abc", "Inverter", "SN")
        controller.devices["abc"] = device
        return device

    @pytest.mark.usefixtures("inverter_abc")
    async def test_does_nothing_while_rejected_and_not_due(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        controller._handle_auth_failure(401)
        await controller._poll_cycle()
        assert aioclient_mock.call_count == 0

    @pytest.mark.usefixtures("inverter_abc")
    async def test_stops_when_the_site_poll_rejects_the_credential(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", status=401)
        aioclient_mock.get(f"{BASE_URL}devices/abc", json={})
        assert await controller.poll_site() is False
        assert controller.auth_failed
        controller._auth_failed = False
        await controller._poll_cycle()
        assert [str(call[1]) for call in aioclient_mock.mock_calls] == [f"{BASE_URL}site"] * 2

    @pytest.mark.usefixtures("inverter_abc")
    async def test_a_rejection_only_reports_that_the_api_answered(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", json=site_payload(site_summary()))
        aioclient_mock.get(f"{BASE_URL}devices/abc", status=401)
        controller.faults.report_poll_cycle = MagicMock()  # type: ignore[method-assign]
        await controller._poll_cycle()
        controller.faults.report_poll_cycle.assert_called_once_with(True)

    @pytest.mark.usefixtures("inverter_abc")
    async def test_reports_nothing_when_shut_down_mid_cycle(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", json=site_payload(site_summary()))
        real_poll_devices = controller.poll_devices

        async def poll_then_stop(**kwargs: Any) -> bool:
            controller._shutdown.set()
            return await real_poll_devices(**kwargs)

        controller.poll_devices = poll_then_stop  # type: ignore[method-assign]
        controller.faults.report_poll_cycle = MagicMock()  # type: ignore[method-assign]
        await controller._poll_cycle()
        controller.faults.report_poll_cycle.assert_not_called()
        assert all("devices" not in str(call[1]) for call in aioclient_mock.mock_calls)

    async def test_first_payload_debug_log_is_truncated(
        self,
        controller: Controller,
        inverter_abc: HybridInverter,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="custom_components.eleven_energy_plus.controller")
        controller._apply_device_payload(inverter_abc, {"blob": "x" * 5000})
        assert "(truncated)" in caplog.text


class TestHousekeeping:
    """Small contracts of the controller's bookkeeping."""

    @pytest.mark.covers("FC-POLL-02")
    async def test_a_shorter_rate_limit_hint_never_shortens_a_deferral(
        self, controller: Controller
    ) -> None:
        controller._defer_polls(ElevenEnergyRateLimitedError("slow down", 200.0))
        controller._defer_polls(ElevenEnergyRateLimitedError("slow down", 10.0))
        assert controller._poll_deferral() > 190
        controller._defer_polls(ElevenEnergyRateLimitedError("no hint", None))
        assert controller._poll_deferral() > 190

    async def test_rate_limit_errors_say_what_was_limited(self, controller: Controller) -> None:
        err = controller._rate_limited("the site API", "12")
        assert str(err) == "the site API was rate limited (HTTP 429)"
        assert err.retry_after == 12.0

    @pytest.mark.covers("FC-OPT-02")
    async def test_an_unchanged_entry_update_pings_nobody(
        self, hass: HomeAssistant, entry: MockConfigEntry, controller: Controller
    ) -> None:
        pings = MagicMock()
        controller.add_option_listener(pings)
        hass.config_entries.async_update_entry(entry, options={CONF_POLL_INTERVAL: 60})
        assert controller.async_entry_updated() is False
        assert controller.async_entry_updated() is False
        assert pings.call_count == 1

    @pytest.mark.covers("FC-DISC-05")
    async def test_forgetting_a_device_drops_everything_about_it(
        self, controller: Controller
    ) -> None:
        device = HybridInverter(controller.hass, controller, "abc", "Inverter", "SN")
        controller.devices["abc"] = device
        controller._last_device_payloads["abc"] = {"pv": {}}
        controller._missing_listings["abc"] = 1
        controller.forget_device("abc")
        assert controller.devices == {}
        assert controller.last_device_payloads == {}
        assert controller._missing_listings == {}
        assert not device.is_running()

    async def test_adding_no_entities_calls_nothing(self, controller: Controller) -> None:
        callback = MagicMock()
        controller.register_platform("sensor", callback)
        controller.add_entities("sensor", [])
        callback.assert_not_called()

    async def test_forgetting_an_unknown_device_is_harmless(self, controller: Controller) -> None:
        controller.forget_device("never-seen")
        assert controller.devices == {}

    async def test_recovery_withdraws_only_reauthentication(
        self, hass: HomeAssistant, entry: MockConfigEntry, controller: Controller
    ) -> None:
        await entry.start_reconfigure_flow(hass)
        entry.async_start_reauth(hass)
        await hass.async_block_till_done()
        controller._auth_failed = True
        controller._recover_auth()
        sources = [flow["context"]["source"] for flow in hass.config_entries.flow.async_progress()]
        assert sources == ["reconfigure"]

    async def test_nothing_is_known_before_the_first_poll(self, controller: Controller) -> None:
        assert controller.last_site_payload is None
        assert controller.last_device_payloads == {}
        assert not controller.auth_failed
        assert not controller.terminated

    @pytest.mark.covers("FC-AUTH-09")
    @pytest.mark.parametrize("token", [123, "", None])
    async def test_a_credential_that_is_not_text_is_never_sent(
        self, hass: HomeAssistant, entry: MockConfigEntry, token: Any
    ) -> None:
        controller = Controller(token, hass, entry)
        with pytest.raises(ElevenEnergyAuthError):
            await controller._auth_headers()

    async def test_an_unsubscribed_listener_is_not_pinged(self, controller: Controller) -> None:
        listener = MagicMock()
        unsubscribe = controller.add_option_listener(listener)
        unsubscribe()
        controller.notify_options_changed()
        listener.assert_not_called()

    async def test_options_that_never_changed_ping_nobody(
        self, controller: Controller
    ) -> None:
        listener = MagicMock()
        controller.add_option_listener(listener)
        assert controller.async_entry_updated() is False
        listener.assert_not_called()

    async def test_every_request_has_a_timeout(
        self, controller: Controller, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from custom_components.eleven_energy_plus import controller as controller_module

        seen: dict[str, Any] = {}

        class _Response:
            status = 200
            headers: ClassVar[dict[str, str]] = {}

            async def json(self, **kwargs: Any) -> Any:
                return {"devices": []}

        class _Request:
            async def __aenter__(self) -> _Response:
                return _Response()

            async def __aexit__(self, *args: Any) -> None:
                return None

        class _Session:
            def request(self, method: str, url: str, **kwargs: Any) -> _Request:
                seen.update(kwargs, method=method, url=url)
                return _Request()

        monkeypatch.setattr(controller_module, "async_get_clientsession", lambda hass: _Session())
        assert await controller._request("GET", "site") == (200, {"devices": []}, None)
        assert seen["timeout"] is controller_module.API_TIMEOUT
        assert seen["method"] == "GET"
        assert seen["url"] == f"{BASE_URL}site"
        assert seen["headers"] == {
            "accept": "application/json",
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        }

    @pytest.mark.covers("FC-POLL-02", "FC-NOTIF-01")
    async def test_any_deferral_left_counts_as_the_api_answering(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from custom_components.eleven_energy_plus import controller as controller_module

        class _Clock:
            @staticmethod
            def monotonic() -> float:
                return 1000.0

        monkeypatch.setattr(controller_module, "time", _Clock)
        aioclient_mock.get(f"{BASE_URL}site", status=429, headers={"Retry-After": "1"})
        controller.faults.report_poll_cycle = MagicMock()  # type: ignore[method-assign]
        await controller._poll_cycle()
        assert controller._poll_deferral() == 1.0
        controller.faults.report_poll_cycle.assert_called_once_with(True)


class TestWorkModeContract:
    """The exact command each work mode sends, and how refusals are reported."""

    @pytest.mark.covers("FC-SVC-06")
    @pytest.mark.parametrize(
        ("service", "data", "body"),
        [
            ("set_work_mode_self_consumption", {"percent_to_battery": 60},
             {"workMode": "selfConsumption", "targetExcessPc": 60.0}),
            ("set_work_mode_force_charge", {"target_power": 3.5, "target_percent": 80},
             {"workMode": "forceCharge", "rate": 3.5, "targetSoc": 80.0}),
            ("set_work_mode_grid_export",
             {"target_power": 5, "target_percent": 20, "include_excess_solar": True, "overdrive": False},
             {"workMode": "gridExport", "rate": 5.0, "targetSoc": 20.0, "addAverageExcess": True, "overdrive": False}),
            ("set_work_mode_pv_export", {}, {"workMode": "pvExportPriority"}),
            ("set_work_mode_idle_battery", {"allow_charging": True, "allow_discharging": False},
             {"workMode": "idleBattery", "allowCharge": True, "allowDischarge": False}),
            ("set_work_mode_target_soc",
             {"target_soc": 90, "target_minutes": 120, "max_charge_power": 4, "max_discharge_power": 2.5},
             {"workMode": "targetSoc", "targetSoc": 90.0, "targetMinutes": 120,
              "maxChargeRate": 4.0, "maxDischargeRate": 2.5}),
            ("set_work_mode_reset", {}, {"workMode": "reset"}),
        ],
    )
    async def test_each_work_mode_sends_exactly_its_api_fields(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
        service: str,
        data: dict[str, Any],
        body: dict[str, Any],
    ) -> None:
        """A wrong key would be ignored by the inverter, so every key is pinned."""
        controller.devices["abc"] = _fake_inverter()
        aioclient_mock.post(POST_URL, status=200)
        result = await controller.set_work_mode(service, data)
        assert result["success"] is True
        assert aioclient_mock.mock_calls[-1][2] == body
        assert set(result["params"]) == set(data)

    @pytest.mark.covers("FC-SVC-08")
    @pytest.mark.parametrize(
        ("service", "data", "auth_failed", "kind"),
        [
            ("set_work_mode_nonsense", {}, False, "invalid_request"),
            ("set_work_mode_force_charge", {"target_power": 3}, False, "invalid_request"),
            ("set_work_mode_reset", {}, True, "credential_rejected"),
        ],
        ids=["unknown_service", "missing_field", "credential_rejected"],
    )
    async def test_refusals_send_nothing_and_say_why(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
        service: str,
        data: dict[str, Any],
        auth_failed: bool,
        kind: str,
    ) -> None:
        controller.devices["abc"] = _fake_inverter()
        controller._auth_failed = auth_failed
        result = await controller.set_work_mode(service, data)
        assert result["error_kind"] == kind
        assert result["attempts"] == 0
        assert result["status"] is None
        assert aioclient_mock.call_count == 0

    async def test_missing_fields_are_all_named(self, controller: Controller) -> None:
        controller.devices["abc"] = _fake_inverter()
        result = await controller.set_work_mode("set_work_mode_target_soc", {})
        assert result["error"] == "missing required field(s): target_soc, target_minutes"
        result = await controller.set_work_mode(
            "set_work_mode_target_soc", {"target_soc": 500, "target_minutes": 0}
        )
        assert result["error"] == "invalid value for field(s): target_soc, target_minutes"

    @pytest.mark.covers("FC-SVC-13", "FC-NOTIF-04")
    async def test_an_internal_error_before_the_target_is_known_is_reported_for_the_site(
        self, hass: HomeAssistant, controller: Controller, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from homeassistant.helpers import issue_registry as ir

        def _bug(data: Any) -> Any:
            raise RuntimeError("resolver bug")

        monkeypatch.setattr(controller, "_resolve_target_device", _bug)
        result = await controller.set_work_mode("set_work_mode_reset", {})
        assert result["error"] == "internal error (RuntimeError: resolver bug)"
        assert result["error_kind"] == "internal_error"
        assert result["success"] is False
        issue = ir.async_get(hass).async_get_issue(DOMAIN, "work_mode_failed_site")
        assert issue is not None
        assert issue.translation_placeholders["name"] == "Eleven Energy Plus"

    @pytest.mark.covers("FC-NOTIF-04")
    async def test_a_failed_command_names_its_inverter(
        self,
        hass: HomeAssistant,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
        instant_backoff: list[float],
    ) -> None:
        from homeassistant.helpers import issue_registry as ir

        controller.devices["abc"] = _fake_inverter()
        aioclient_mock.post(POST_URL, status=500)
        await controller.set_work_mode("set_work_mode_reset", {})
        issue = ir.async_get(hass).async_get_issue(DOMAIN, "work_mode_failed_abc")
        assert issue is not None
        assert issue.translation_placeholders["name"] == "Eleven Energy Plus abc"

    async def test_shutdown_before_the_first_attempt_sends_nothing(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        controller._shutdown.set()
        result = await controller.send_reliable_post("devices/abc/operatingMode", {})
        assert result == PostResult(
            status=None,
            body=None,
            attempts=0,
            error="aborted because Eleven Energy Plus is shutting down",
            kind="aborted",
        )

    async def test_api_failures_are_api_errors_with_the_reason(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker, instant_backoff: list[float]
    ) -> None:
        aioclient_mock.post(POST_URL, exc=aiohttp.ClientError("connection reset"))
        result = await controller.send_reliable_post("devices/abc/operatingMode", {})
        assert result.kind == "api_error"
        assert "connection reset" in str(result.error)
        aioclient_mock.clear_requests()
        aioclient_mock.post(POST_URL, status=400)
        result = await controller.send_reliable_post("devices/abc/operatingMode", {})
        assert result.kind == "api_error"
        assert result.status == 400
        assert result.body is None


class TestTargetsAndListing:
    """Corners of target resolution and of following the site listing."""

    @pytest.mark.covers("FC-POLL-02", "FC-DISC-07")
    async def test_a_deferral_of_under_a_second_still_holds_off_discovery_reads(
        self,
        controller: Controller,
        aioclient_mock: AiohttpClientMocker,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from custom_components.eleven_energy_plus import controller as controller_module

        class _Clock:
            @staticmethod
            def monotonic() -> float:
                return 1000.0

        monkeypatch.setattr(controller_module, "time", _Clock)
        controller._polls_deferred_until = 1000.5
        aioclient_mock.get(f"{BASE_URL}site", json=site_payload(site_summary("b")))
        assert await controller.poll_site()
        assert "b" in controller.devices, "built from its listing"
        assert [str(call[1]) for call in aioclient_mock.mock_calls] == [f"{BASE_URL}site"]

    async def test_an_area_holding_only_an_entity_resolves(
        self, hass: HomeAssistant, entry: MockConfigEntry, controller: Controller
    ) -> None:
        controller.devices["a"] = _fake_inverter("a")
        controller.devices["b"] = _fake_inverter("b")
        area = ar.async_get(hass).async_create("Loft")
        device = _register_device(hass, entry, "a")
        _register_device(hass, entry, "b")
        entity = er.async_get(hass).async_get_or_create(
            "sensor", DOMAIN, "a_pv_power", config_entry=entry, device_id=device.id
        )
        # Home Assistant only lets an entity with its own name have its own area.
        er.async_get(hass).async_update_entity(entity.entity_id, name="Loft PV", area_id=area.id)
        assert controller._resolve_target_device({"area_id": area.id}) == ("a", None)

    async def test_an_unknown_device_id_does_not_hide_a_known_one(
        self, hass: HomeAssistant, entry: MockConfigEntry, controller: Controller
    ) -> None:
        controller.devices["a"] = _fake_inverter("a")
        device = _register_device(hass, entry, "a")
        assert controller._resolve_target_device(
            {"device_id": ["no-such-device", device.id]}
        ) == ("a", None)

    async def test_a_device_of_ours_the_controller_no_longer_knows_is_refused(
        self, hass: HomeAssistant, entry: MockConfigEntry, controller: Controller
    ) -> None:
        controller.devices["a"] = _fake_inverter("a")
        gone = _register_device(hass, entry, "gone")
        other = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={("other_domain", "a")}
        )
        assert controller._resolve_target_device({"device_id": [gone.id]})[0] is None
        assert controller._resolve_target_device({"device_id": [other.id]})[0] is None

    @pytest.mark.covers("FC-DISC-05")
    async def test_the_missing_count_restarts_when_an_inverter_is_listed_again(
        self, hass: HomeAssistant, controller: Controller
    ) -> None:
        from homeassistant.helpers import issue_registry as ir

        device = HybridInverter(hass, controller, "a", "Inverter", "SN")
        controller.devices["a"] = device
        controller._track_missing([])
        controller._track_missing([])
        assert device.missing is True
        issue = ir.async_get(hass).async_get_issue(DOMAIN, "inverter_missing_a")
        assert issue is not None
        assert issue.translation_placeholders["name"] == "Eleven Energy Plus Inverter"
        controller._track_missing(["a"])
        assert device.missing is False
        controller._track_missing([])
        assert device.missing is False, "one listing without it is not enough again"

    async def test_a_missing_inverter_does_not_stop_the_others_being_polled(
        self, hass: HomeAssistant, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        first = HybridInverter(hass, controller, "a", "A", "SN")
        second = HybridInverter(hass, controller, "b", "B", "SN")
        controller.devices.update(a=first, b=second)
        first.set_missing(True)
        aioclient_mock.get(f"{BASE_URL}devices/b", json={"pv": {"power": 1.0}})
        assert await controller.poll_devices() is True
        assert second.sensor_entities["pv.power"].native_value == 1.0

    @pytest.mark.covers("FC-POLL-02")
    async def test_a_rate_limited_site_poll_defers_polling(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}site", status=429, headers={"Retry-After": "120"})
        assert await controller.poll_site() is False
        assert controller._poll_deferral() > 119

    async def test_a_successful_site_poll_is_kept_for_diagnostics(
        self, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        payload = site_payload(site_summary("zzz", device_type="thermostat"))
        aioclient_mock.get(f"{BASE_URL}site", json=payload)
        assert await controller.poll_site() is True
        assert controller.last_site_payload == payload

    @pytest.mark.covers("FC-DISC-02", "FC-DISC-07")
    async def test_discovery_keeps_the_serial_and_does_not_count_a_failed_read(
        self, hass: HomeAssistant, controller: Controller, aioclient_mock: AiohttpClientMocker
    ) -> None:
        aioclient_mock.get(f"{BASE_URL}devices/abc", status=500)
        await controller._discover("abc", site_summary(serial="SN-42"))
        inverter = controller.devices["abc"]
        assert device_field(inverter.device_info, "serial_number") == "SN-42"
        assert inverter.api_connected is None
        assert controller.last_device_payloads == {}

    async def test_a_processing_failure_logs_its_traceback_once_per_run(
        self,
        hass: HomeAssistant,
        controller: Controller,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        inverter = HybridInverter(hass, controller, "abc", "Inverter", "SN")
        controller.devices["abc"] = inverter
        real_update = HybridInverter.update

        def tracebacks() -> int:
            return sum(1 for record in caplog.records if record.exc_info)

        monkeypatch.setattr(HybridInverter, "update", lambda self, payload: 1 / 0)
        controller._apply_device_payload(inverter, {})
        controller._apply_device_payload(inverter, {})
        assert tracebacks() == 1
        monkeypatch.setattr(HybridInverter, "update", real_update)
        controller._apply_device_payload(inverter, {})
        monkeypatch.setattr(HybridInverter, "update", lambda self, payload: 1 / 0)
        controller._apply_device_payload(inverter, {})
        assert tracebacks() == 2

    @pytest.mark.covers("FC-AUTH-08")
    async def test_a_second_rejection_after_recovery_is_retested_too(
        self, hass: HomeAssistant, controller: Controller, aioclient_mock: AiohttpClientMocker,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("custom_components.eleven_energy_plus.controller.AUTH_RECOVERY_SECONDS", 0)
        controller._handle_auth_failure(None)
        controller._recover_auth()
        controller._handle_auth_failure(401)
        aioclient_mock.get(f"{BASE_URL}site", json=site_payload())
        assert await controller._check_auth_recovered() is True


class TestPollerLoop:
    """The background loop keeps going through timeouts and rate limits."""

    async def test_the_poller_waits_again_after_each_interval_and_during_a_deferral(
        self, controller: Controller, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from custom_components.eleven_energy_plus import controller as controller_module

        waits: list[float] = []
        cycles = 0

        async def _cycle() -> None:
            nonlocal cycles
            cycles += 1

        async def _wait_for(awaitable: Any, timeout: float | None = None) -> Any:  # noqa: ASYNC109 - stands in for asyncio.wait_for
            awaitable.close()
            waits.append(timeout or 0.0)
            if len(waits) == 2:
                controller._polls_deferred_until = controller_module.time.monotonic() + 50
                controller._poll_now.set()
                return True
            if len(waits) >= 3:
                controller._shutdown.set()
            raise TimeoutError

        async def _no_devices(**kwargs: Any) -> bool:
            return False

        controller.poll_devices = _no_devices  # type: ignore[method-assign]
        controller._poll_cycle = _cycle  # type: ignore[method-assign]
        monkeypatch.setattr(controller_module.asyncio, "wait_for", _wait_for)
        controller.start_poller()
        assert controller.poller_task is not None
        await asyncio.wait({controller.poller_task}, timeout=1)
        assert len(waits) == 3, "a timeout and a wake during a deferral both loop again"
        assert cycles == 1, "no cycle runs while polling is deferred"

    async def test_a_wake_with_one_second_of_deferral_left_still_waits(
        self, controller: Controller, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from custom_components.eleven_energy_plus import controller as controller_module

        class _Clock:
            @staticmethod
            def monotonic() -> float:
                return 1000.0

        monkeypatch.setattr(controller_module, "time", _Clock)
        controller._polls_deferred_until = 1001.0
        cycles = 0
        wakes = 0

        async def _cycle() -> None:
            nonlocal cycles
            cycles += 1

        async def _wait_for(awaitable: Any, timeout: float | None = None) -> Any:  # noqa: ASYNC109 - stands in for asyncio.wait_for
            nonlocal wakes
            awaitable.close()
            wakes += 1
            if wakes > 1:
                controller._shutdown.set()
            return True

        async def _no_devices(**kwargs: Any) -> bool:
            return False

        controller.poll_devices = _no_devices  # type: ignore[method-assign]
        controller._poll_cycle = _cycle  # type: ignore[method-assign]
        monkeypatch.setattr(controller_module.asyncio, "wait_for", _wait_for)
        controller.start_poller()
        assert controller.poller_task is not None
        await asyncio.wait({controller.poller_task}, timeout=1)
        assert cycles == 0
